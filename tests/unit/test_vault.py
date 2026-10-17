"""The vault: envelope encryption, slot binding, key files, rotation (``vault/ciphertext_only``)."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

from yaqzan.core.vault import (
    CredentialSlot,
    Keyring,
    MasterKey,
    SealedSecret,
    VaultError,
    VaultErrorCode,
    load_master_key,
    open_secret,
    rewrap,
    seal,
)
from yaqzan.core.vault.envelope import MAX_SECRET_BYTES
from yaqzan.core.vault.keys import check_private_mode, parse_key_text

SECRET = "read-only-api-value-0123456789"


def slot(**overrides: object) -> CredentialSlot:
    fields: dict[str, object] = {
        "tenant_id": uuid4(),
        "project_id": uuid4(),
        "connector": "stripe",
        "name": "api_key",
        "environment": "production",
    }
    fields.update(overrides)
    return CredentialSlot.model_validate(fields)


@pytest.fixture
def ring() -> Keyring:
    return Keyring([MasterKey.generate("k1")], current_id="k1")


def test_round_trip(ring: Keyring) -> None:
    where = slot()
    sealed = seal(SecretStr(SECRET), where, ring)
    assert open_secret(sealed, where, ring).get_secret_value() == SECRET


def test_database_row_holds_ciphertext_only(ring: Keyring) -> None:
    sealed = seal(SecretStr(SECRET), slot(), ring)
    stored = sealed.model_dump_json().encode()
    assert SECRET.encode() not in stored
    assert base64.b64encode(SECRET.encode()) not in stored
    assert SECRET not in repr(sealed)
    assert sealed.master_key_id == "k1"


def test_same_secret_encrypts_differently_each_time(ring: Keyring) -> None:
    where = slot()
    first = seal(SecretStr(SECRET), where, ring)
    second = seal(SecretStr(SECRET), where, ring)
    assert first.ciphertext != second.ciphertext
    assert first.wrapped_dek != second.wrapped_dek


def test_ciphertext_copied_to_another_project_will_not_open(ring: Keyring) -> None:
    original = slot()
    sealed = seal(SecretStr(SECRET), original, ring)
    elsewhere = original.model_copy(update={"project_id": uuid4()})
    with pytest.raises(VaultError) as info:
        open_secret(sealed, elsewhere, ring)
    assert info.value.code is VaultErrorCode.TAMPERED


@pytest.mark.parametrize("field", ["connector", "name", "environment"])
def test_any_slot_change_blocks_decryption(ring: Keyring, field: str) -> None:
    original = slot()
    sealed = seal(SecretStr(SECRET), original, ring)
    changed = {"connector": "paddle", "name": "webhook_secret", "environment": "staging"}[field]
    with pytest.raises(VaultError):
        open_secret(sealed, original.model_copy(update={field: changed}), ring)


@pytest.mark.parametrize("part", ["ciphertext", "wrapped_dek"])
def test_flipping_one_bit_is_detected(ring: Keyring, part: str) -> None:
    where = slot()
    sealed = seal(SecretStr(SECRET), where, ring)
    blob = bytearray(getattr(sealed, part))
    blob[-1] ^= 0x01
    tampered = sealed.model_copy(update={part: bytes(blob)})
    with pytest.raises(VaultError) as info:
        open_secret(tampered, where, ring)
    assert info.value.code is VaultErrorCode.TAMPERED


def test_wrong_master_key_fails_closed(ring: Keyring) -> None:
    where = slot()
    sealed = seal(SecretStr(SECRET), where, ring)
    impostor = Keyring([MasterKey.generate("k1")], current_id="k1")
    with pytest.raises(VaultError):
        open_secret(sealed, where, impostor)


def test_unknown_master_key_id(ring: Keyring) -> None:
    where = slot()
    sealed = seal(SecretStr(SECRET), where, ring).model_copy(update={"master_key_id": "gone"})
    with pytest.raises(VaultError) as info:
        open_secret(sealed, where, ring)
    assert info.value.code is VaultErrorCode.UNKNOWN_KEY


def test_empty_and_oversized_secrets_are_refused(ring: Keyring) -> None:
    with pytest.raises(VaultError) as empty:
        seal(SecretStr(""), slot(), ring)
    assert empty.value.code is VaultErrorCode.EMPTY
    with pytest.raises(VaultError) as big:
        seal(SecretStr("x" * (MAX_SECRET_BYTES + 1)), slot(), ring)
    assert big.value.code is VaultErrorCode.TOO_LARGE


def test_non_utf8_plaintext_is_reported_not_crashed() -> None:
    # Build a sealed value whose plaintext is invalid UTF-8, using the same construction.
    from yaqzan.core.vault import envelope  # noqa: PLC0415  # private helpers, this test only

    key = MasterKey.generate("k1")
    keys = Keyring([key], current_id="k1")
    where = slot()
    dek = bytes(32)
    sealed = SealedSecret(
        master_key_id="k1",
        wrapped_dek=envelope._wrap_dek(dek, where, key),
        ciphertext=envelope._encrypt(dek, b"\xff\xfe", envelope._DATA_DOMAIN + where.associated_data()),
    )
    with pytest.raises(VaultError) as info:
        open_secret(sealed, where, keys)
    assert info.value.code is VaultErrorCode.UNSUPPORTED


def test_rotation_rewraps_without_touching_the_secret() -> None:
    old = MasterKey.generate("k1")
    new = MasterKey.generate("k2")
    where = slot()
    sealed = seal(SecretStr(SECRET), where, Keyring([old], current_id="k1"))
    both = Keyring([old, new], current_id="k2")
    moved = rewrap(sealed, where, both)
    assert moved.master_key_id == "k2"
    assert moved.ciphertext == sealed.ciphertext
    only_new = Keyring([new], current_id="k2")
    assert open_secret(moved, where, only_new).get_secret_value() == SECRET
    assert rewrap(moved, where, both) is moved


def test_keyring_rules() -> None:
    key = MasterKey.generate("k1")
    with pytest.raises(VaultError):
        Keyring([key], current_id="k2")
    with pytest.raises(VaultError):
        Keyring([key, MasterKey.generate("k1")], current_id="k1")
    assert "k1" in repr(Keyring([key], current_id="k1"))


def test_master_key_never_shows_its_bytes() -> None:
    key = MasterKey("k1", b"\x07" * 32)
    for text in (repr(key), str(key)):
        assert "k1" in text
        assert "\\x07" not in text
        assert "BwcH" not in text  # base64 of the key bytes


@pytest.mark.parametrize(("key_id", "material"), [("Bad Id", b"\x00" * 32), ("k1", b"\x00" * 31)])
def test_master_key_validation(key_id: str, material: bytes) -> None:
    with pytest.raises(VaultError):
        MasterKey(key_id, material)


def test_key_file_round_trip(tmp_path: Path) -> None:
    key = MasterKey.generate("prod-2026-10")
    path = tmp_path / "master.key"
    path.write_text(key.to_file_text(), encoding="ascii")
    path.chmod(0o600)
    loaded = load_master_key(path)
    assert loaded.key_id == "prod-2026-10"
    assert loaded.material() == key.material()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "yaqzan-master-v1 k1",
        "other-header k1 AAAA",
        "yaqzan-master-v1 k1 not*base64!",
        "yaqzan-master-v1 k1 é",
    ],
)
def test_bad_key_text_is_refused_without_echoing_it(text: str) -> None:
    with pytest.raises(VaultError) as info:
        parse_key_text(text)
    assert info.value.code is VaultErrorCode.BAD_KEY_FILE
    if text:
        assert text.rsplit(" ", maxsplit=1)[-1] not in str(info.value)


def test_missing_and_binary_key_files(tmp_path: Path) -> None:
    with pytest.raises(VaultError):
        load_master_key(tmp_path / "absent.key")
    binary = tmp_path / "binary.key"
    binary.write_bytes(b"\xff\xfe\x00")
    binary.chmod(0o600)
    with pytest.raises(VaultError) as info:
        load_master_key(binary)
    assert info.value.code is VaultErrorCode.BAD_KEY_FILE


def test_key_file_readable_by_others_is_refused_on_posix() -> None:
    with pytest.raises(VaultError) as info:
        check_private_mode(stat.S_IFREG | 0o644, posix=True)
    assert info.value.code is VaultErrorCode.KEY_FILE_TOO_OPEN
    check_private_mode(stat.S_IFREG | 0o600, posix=True)
    check_private_mode(stat.S_IFREG | 0o644, posix=False)


def test_key_file_owned_by_another_user_is_refused_on_posix() -> None:
    with pytest.raises(VaultError) as info:
        check_private_mode(stat.S_IFREG | 0o600, posix=True, owner_uid=1000, my_uid=0)
    assert info.value.code is VaultErrorCode.KEY_FILE_TOO_OPEN


@pytest.mark.parametrize("kind", [stat.S_IFDIR, stat.S_IFIFO, stat.S_IFLNK])
def test_key_file_must_be_a_plain_file_on_posix(kind: int) -> None:
    with pytest.raises(VaultError) as info:
        check_private_mode(kind | 0o600, posix=True)
    assert info.value.code is VaultErrorCode.BAD_KEY_FILE


def test_symlinked_key_file_is_refused(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW exists only on POSIX; the server is Linux")
    real = tmp_path / "real.key"
    real.write_text(MasterKey.generate("k1").to_file_text(), encoding="ascii")
    real.chmod(0o600)
    link = tmp_path / "link.key"
    link.symlink_to(real)
    assert load_master_key(real).key_id == "k1"
    with pytest.raises(VaultError):
        load_master_key(link)


@settings(max_examples=25, deadline=None)
@given(st.text(min_size=1, max_size=300))
def test_any_text_round_trips(value: str) -> None:
    keys = Keyring([MasterKey.generate("k1")], current_id="k1")
    where = slot()
    assert open_secret(seal(SecretStr(value), where, keys), where, keys).get_secret_value() == value


def test_sealed_secret_survives_a_json_round_trip(ring: Keyring) -> None:
    where = slot()
    sealed = seal(SecretStr(SECRET), where, ring)
    restored = SealedSecret.model_validate_json(sealed.model_dump_json())
    assert restored == sealed
    assert open_secret(restored, where, ring).get_secret_value() == SECRET
