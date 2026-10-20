"""A software passkey for tests: real WebAuthn answers (P-256, "none" attestation), as Windows Hello makes.

It signs exactly what a browser and authenticator would send, so the server's
checks run for real. Knobs let a test break one thing at a time: the origin,
the site name, user verification, the counter, the challenge, the signature.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

UP, UV, AT = 0x01, 0x04, 0x40


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class SoftAuthenticator:
    origin: str = "http://localhost:8765"
    rp_id: str = "localhost"
    counter: int = 0
    key: ec.EllipticCurvePrivateKey = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))
    credential_id: bytes = field(default_factory=lambda: os.urandom(32))

    def _cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        )

    def _client_data(self, kind: str, challenge: str, origin: str | None) -> bytes:
        data = {"type": kind, "challenge": challenge, "origin": origin or self.origin, "crossOrigin": False}
        return json.dumps(data, separators=(",", ":")).encode()

    def _auth_data(self, flags: int, rp_id: str | None, extra: bytes = b"") -> bytes:
        rp_hash = hashlib.sha256((rp_id or self.rp_id).encode()).digest()
        return rp_hash + bytes([flags]) + self.counter.to_bytes(4, "big") + extra

    def register(
        self, options: dict[str, object], *, origin: str | None = None, verified: bool = True
    ) -> dict[str, object]:
        """The browser's answer to ``navigator.credentials.create`` (as ``credential.toJSON()`` gives it)."""
        challenge = str(options["challenge"])
        attested = (
            bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + self._cose_key()
        )
        flags = UP | AT | (UV if verified else 0)
        auth_data = self._auth_data(flags, None, attested)
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(self._client_data("webauthn.create", challenge, origin)),
                "attestationObject": b64url(attestation),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }

    def sign(  # each knob breaks one thing on purpose
        self,
        options: dict[str, object],
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        verified: bool = True,
        challenge: str | None = None,
        tamper: bool = False,
    ) -> dict[str, object]:
        """The browser's answer to ``navigator.credentials.get``; the counter goes up by one first."""
        self.counter += 1
        client = self._client_data("webauthn.get", challenge or str(options["challenge"]), origin)
        auth_data = self._auth_data(UP | (UV if verified else 0), rp_id)
        signature = self.key.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        if tamper:
            signature = signature[:-1] + bytes([signature[-1] ^ 1])
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }
