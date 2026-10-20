"""Envelope encryption for user-supplied ESPN cookies (spec §2 "Credential security").

  plaintext  = canonical JSON {"espn_s2": ..., "swid": ...}
  data key   = 32 random bytes per credential;  nonce = 12 random bytes
  ciphertext = AES-256-GCM(data key, nonce, plaintext, AAD)
  AAD        = b"fi-cred-v1" | 0x1f | league_id | 0x1f | credential_id | 0x1f | owner_user_id   (never the key version)
  wrapped    = RSA-OAEP-3072/SHA-256(public key CREDENTIALS_PUBKEY_V<n>, data key);  wrap_key_version = "v<n>"

The API process holds a PublicKeyring only: it can encrypt but not decrypt. GitHub Actions holds the PrivateKeyring.
Rotation (`rewrap`) unwraps with the version the row names and rewraps under the new one; ciphertext, nonce and
AAD stay byte-identical. Nothing in this module logs; reprs show lengths and versions only."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD_FORMAT = "fi-cred-v1"
RSA_BITS = 3072
_VERSION = re.compile(r"v[0-9]{1,4}")
_FIELDS = ("espn_s2", "swid")
_OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


class CredentialCryptoError(Exception):
    """Any failure: wrong key, tampered row, bad input. The message never includes key or cookie material."""


def build_aad(league_id: str, credential_id: str, owner_user_id: str) -> bytes:
    parts = (AAD_FORMAT, league_id, credential_id, owner_user_id)
    if not all(isinstance(p, str) and p and "\x1f" not in p for p in parts):
        raise CredentialCryptoError("bad AAD component")
    return "\x1f".join(parts).encode("utf-8")


@dataclass(frozen=True)
class EncryptedCredential:
    ciphertext: bytes
    nonce: bytes
    aad_format: str
    wrapped_data_key: bytes
    wrap_key_version: str

    def __repr__(self) -> str:
        return (f"EncryptedCredential(ciphertext=<{len(self.ciphertext)} bytes>, nonce=<{len(self.nonce)} bytes>, "
                f"aad_format={self.aad_format!r}, wrapped_data_key=<{len(self.wrapped_data_key)} bytes>, "
                f"wrap_key_version={self.wrap_key_version!r})")

    __str__ = __repr__


def _check_version(version: str) -> str:
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise CredentialCryptoError("bad key version")
    return version


def _version_number(version: str) -> int:
    return int(version[1:])


def _check_payload(payload: object) -> bytes:
    if (not isinstance(payload, dict) or set(payload) != set(_FIELDS)
            or not all(isinstance(payload[f], str) and payload[f] for f in _FIELDS)):
        raise CredentialCryptoError("payload must be {'espn_s2': str, 'swid': str}")
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


class PublicKeyring:
    """Versioned RSA public keys. The API's side: encrypt only."""
    __slots__ = ("_keys",)

    def __init__(self, keys: dict[str, str]):
        loaded = {}
        for version, pem in (keys or {}).items():
            _check_version(version)
            if not isinstance(pem, str) or "PRIVATE KEY" in pem:
                raise CredentialCryptoError("a private key was offered to the public keyring")
            try:
                key = serialization.load_pem_public_key(pem.encode("utf-8"))
            except (ValueError, TypeError):
                raise CredentialCryptoError("bad public key PEM") from None
            if not isinstance(key, rsa.RSAPublicKey) or key.key_size < RSA_BITS:
                raise CredentialCryptoError("public key must be RSA-3072 or larger")
            loaded[version] = key
        if not loaded:
            raise CredentialCryptoError("no credential public keys configured")
        object.__setattr__(self, "_keys", loaded)

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys, key=_version_number))

    @property
    def current_version(self) -> str:
        return self.versions[-1]

    def wrap(self, data_key: bytes, version: str) -> bytes:
        try:
            return self._keys[version].encrypt(data_key, _OAEP)
        except KeyError:
            raise CredentialCryptoError("unknown key version") from None

    def encrypt(self, payload: dict, *, league_id: str, credential_id: str, owner_user_id: str,
                version: str | None = None) -> EncryptedCredential:
        version = _check_version(version or self.current_version)
        plaintext = _check_payload(payload)
        data_key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
        ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, build_aad(league_id, credential_id, owner_user_id))
        return EncryptedCredential(ciphertext, nonce, AAD_FORMAT, self.wrap(data_key, version), version)

    def __repr__(self) -> str:
        return f"PublicKeyring(versions={self.versions})"

    def __reduce__(self):
        raise TypeError("keyrings cannot be serialized")


class PrivateKeyring:
    """Versioned RSA private keys. Sync workers only (GitHub Actions secrets)."""
    __slots__ = ("_keys",)

    def __init__(self, keys: dict[str, str]):
        loaded = {}
        for version, pem in (keys or {}).items():
            _check_version(version)
            try:
                key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
            except (ValueError, TypeError):
                raise CredentialCryptoError("bad private key PEM") from None
            if not isinstance(key, rsa.RSAPrivateKey):
                raise CredentialCryptoError("private key must be RSA")
            loaded[version] = key
        if not loaded:
            raise CredentialCryptoError("no credential private keys configured")
        object.__setattr__(self, "_keys", loaded)

    def _unwrap(self, enc: EncryptedCredential) -> bytes:
        if enc.aad_format != AAD_FORMAT:
            raise CredentialCryptoError("unknown AAD format")
        key = self._keys.get(_check_version(enc.wrap_key_version))    # exactly the version the row names
        if key is None:
            raise CredentialCryptoError("no private key for this row's version")
        try:
            data_key = key.decrypt(enc.wrapped_data_key, _OAEP)
        except ValueError:
            raise CredentialCryptoError("cannot unwrap data key") from None
        if len(data_key) != 32:
            raise CredentialCryptoError("bad data key")
        return data_key

    def decrypt(self, enc: EncryptedCredential, *, league_id: str, credential_id: str, owner_user_id: str) -> dict:
        data_key = self._unwrap(enc)
        try:
            plaintext = AESGCM(data_key).decrypt(enc.nonce, enc.ciphertext,
                                                 build_aad(league_id, credential_id, owner_user_id))
            payload = json.loads(plaintext)
        except (InvalidTag, ValueError):
            raise CredentialCryptoError("credential does not authenticate") from None
        _check_payload(payload)
        return payload

    def rewrap(self, enc: EncryptedCredential, *, to_version: str, public: PublicKeyring) -> EncryptedCredential:
        data_key = self._unwrap(enc)
        return EncryptedCredential(enc.ciphertext, enc.nonce, enc.aad_format,
                                   public.wrap(data_key, _check_version(to_version)), to_version)

    def __repr__(self) -> str:
        return f"PrivateKeyring(versions={tuple(sorted(self._keys, key=_version_number))})"

    def __reduce__(self):
        raise TypeError("keyrings cannot be serialized")


def generate_keypair() -> tuple[str, str]:
    """(private PEM, public PEM). RSA-3072, PKCS8 / SubjectPublicKeyInfo, unencrypted (the private PEM lives only in
    a GitHub Actions secret)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_BITS)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private.decode("ascii"), public.decode("ascii")
