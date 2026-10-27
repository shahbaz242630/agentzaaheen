"""Certificate reads: one TLS handshake, a real local server with throwaway certificates (F11 slice 5b)."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from agentzaaheen.core.net import CertProbe, NetError, NetErrorCode
from agentzaaheen.core.sdk import CertAnswer

NOW = datetime.now(UTC)
HOST = "localhost"


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


class Authority:
    """A throwaway certificate authority that signs one server certificate per test."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(_name("Agent Zaaheen test CA"))
            .issuer_name(_name("Agent Zaaheen test CA"))
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - timedelta(days=2))
            .not_valid_after(NOW + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False)
            .sign(self.key, hashes.SHA256())
        )

    def trusting(self) -> ssl.SSLContext:
        return ssl.create_default_context(cadata=self.cert.public_bytes(serialization.Encoding.PEM).decode())

    def server(self, folder: Path, *, name: str = HOST, ends: datetime) -> ssl.SSLContext:
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name(name))
            .issuer_name(self.cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(min(NOW - timedelta(days=1), ends - timedelta(days=2)))
            .not_valid_after(ends)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False
            )
            .sign(self.key, hashes.SHA256())
        )
        cert_file, key_file = folder / f"{name}.crt", folder / f"{name}.key"
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file)
        return context


@asynccontextmanager
async def serving(context: ssl.SSLContext) -> AsyncIterator[int]:
    async def answer(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0, ssl=context)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def read(server: ssl.SSLContext, client: ssl.SSLContext) -> CertAnswer:
    async def body() -> CertAnswer:
        async with serving(server) as port:
            probe = CertProbe(frozenset({HOST}), context=client, port=port, timeout_s=5)
            return await probe.certificate_of(HOST)

    return asyncio.run(body())


@pytest.fixture
def authority() -> Authority:
    return Authority()


def test_a_valid_certificate_says_ok_and_when_it_ends(authority: Authority, tmp_path: Path) -> None:
    ends = (NOW + timedelta(days=40)).replace(microsecond=0)
    answer = read(authority.server(tmp_path, ends=ends), authority.trusting())
    assert answer == CertAnswer(reason="cert.ok", not_after=ends)


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda a, d: a.server(d, ends=NOW - timedelta(days=1)), "cert.expired"),
        (
            lambda a, d: a.server(d, name="other.example.com", ends=NOW + timedelta(days=40)),
            "cert.wrong_name",
        ),
    ],
)
def test_a_certificate_that_is_not_valid_is_an_answer_not_an_error(
    authority: Authority, tmp_path: Path, make: Callable[[Authority, Path], ssl.SSLContext], reason: str
) -> None:
    assert read(make(authority, tmp_path), authority.trusting()) == CertAnswer(reason=reason)


def test_a_certificate_from_an_issuer_nobody_trusts_is_not_valid(
    authority: Authority, tmp_path: Path
) -> None:
    stranger = Authority().trusting()  # trusts another authority only
    answer = read(authority.server(tmp_path, ends=NOW + timedelta(days=40)), stranger)
    assert answer == CertAnswer(reason="cert.invalid")


@pytest.mark.parametrize(
    ("opened", "code"),
    [
        (TimeoutError(), NetErrorCode.TIMEOUT),
        (ConnectionRefusedError(), NetErrorCode.UNAVAILABLE),  # nothing listening
        (ssl.SSLError("handshake failure"), NetErrorCode.UNAVAILABLE),  # it does not speak TLS
        ({}, NetErrorCode.BAD_RESPONSE),
        ({"notAfter": "not a date"}, NetErrorCode.BAD_RESPONSE),
    ],
)
def test_no_answer_in_time_or_no_end_date_is_an_error(opened: object, code: NetErrorCode) -> None:
    async def opener(*_: object) -> dict[str, object]:
        if isinstance(opened, BaseException):
            raise opened
        assert isinstance(opened, dict)
        return opened

    with pytest.raises(NetError) as caught:
        asyncio.run(CertProbe(frozenset({HOST}), opener=opener).certificate_of(HOST))
    assert caught.value.net_code is code


@pytest.mark.parametrize("host", ["example.org", "127.0.0.1", "LOCALHOST.evil"])
def test_only_allowlisted_names_are_ever_reached(host: str) -> None:
    async def opener(*_: object) -> dict[str, object]:
        raise AssertionError("never reached")

    with pytest.raises(NetError) as caught:
        asyncio.run(CertProbe(frozenset({HOST}), opener=opener).certificate_of(host))
    assert caught.value.net_code is NetErrorCode.BLOCKED


def test_by_default_the_real_opener_checks_certificates_as_a_browser_would() -> None:
    from agentzaaheen.core.net import cert  # noqa: PLC0415  # the module's defaults, checked once

    probe = CertProbe(frozenset({HOST}))
    assert probe._opener is cert._open
    context = probe._context
    assert context.verify_mode is ssl.CERT_REQUIRED  # a trusted issuer
    assert context.check_hostname  # for this very name
