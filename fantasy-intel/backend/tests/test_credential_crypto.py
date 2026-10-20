"""Envelope encryption for ESPN cookies: AES-256-GCM under a per-credential data key, the data key wrapped with
RSA-OAEP under a versioned public key the API holds; only GitHub Actions holds private keys."""
import logging
import pickle
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from app.control_plane.credential_crypto import (
    AAD_FORMAT, CredentialCryptoError, EncryptedCredential, PrivateKeyring, PublicKeyring, build_aad, generate_keypair,
)

BACKEND = Path(__file__).resolve().parents[1]
LEAGUE, CRED, OWNER = "lg_" + "a" * 24, "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f", "11111111-1111-4111-8111-111111111111"
COOKIES = {"espn_s2": "AEBtestcookie" + "x" * 60, "swid": "{11111111-2222-4333-8444-555555555555}"}


@pytest.fixture(scope="module")
def pairs():
    return {"v1": generate_keypair(), "v2": generate_keypair()}


@pytest.fixture
def pub(pairs):
    return PublicKeyring({"v1": pairs["v1"][1]})


@pytest.fixture
def priv(pairs):
    return PrivateKeyring({"v1": pairs["v1"][0], "v2": pairs["v2"][0]})


def test_round_trip_and_shape(pub, priv):
    enc = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    assert (enc.aad_format, enc.wrap_key_version, len(enc.nonce), len(enc.wrapped_data_key)) == (AAD_FORMAT, "v1", 12, 384)
    assert priv.decrypt(enc, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER) == COOKIES
    again = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    assert again.ciphertext != enc.ciphertext and again.nonce != enc.nonce      # fresh key + nonce every time


def test_aad_binds_the_row_and_excludes_the_key_version(pub, priv):
    enc = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    for swap in (dict(league_id="lg_" + "b" * 24), dict(credential_id="1f1f1f1f-1f1f-4f1f-8f1f-1f1f1f1f1f1f"),
                 dict(owner_user_id="22222222-2222-4222-8222-222222222222")):
        kw = {"league_id": LEAGUE, "credential_id": CRED, "owner_user_id": OWNER, **swap}
        with pytest.raises(CredentialCryptoError):
            priv.decrypt(enc, **kw)
    # The AAD is exactly format + row identity; the wrapping-key version is deliberately not part of it, so a
    # rewrap under a new key leaves it valid.
    assert build_aad(LEAGUE, CRED, OWNER) == "\x1f".join((AAD_FORMAT, LEAGUE, CRED, OWNER)).encode()


def test_the_api_holds_only_public_keys_and_cannot_decrypt(pub, pairs):
    pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    assert not hasattr(pub, "decrypt")
    with pytest.raises(CredentialCryptoError):
        PrivateKeyring({"v1": pairs["v1"][1]})          # a public PEM is not a private key
    with pytest.raises(CredentialCryptoError):
        PublicKeyring({"v1": pairs["v1"][0]})           # and the API refuses to be handed a private key


def test_rewrap_changes_only_the_wrapped_key_and_version(pub, priv, pairs):
    enc = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    pub2 = PublicKeyring({"v1": pairs["v1"][1], "v2": pairs["v2"][1]})
    assert pub2.current_version == "v2"
    enc2 = priv.rewrap(enc, to_version="v2", public=pub2)
    assert (enc2.ciphertext, enc2.nonce, enc2.aad_format) == (enc.ciphertext, enc.nonce, enc.aad_format)
    assert enc2.wrap_key_version == "v2" and enc2.wrapped_data_key != enc.wrapped_data_key
    assert priv.decrypt(enc2, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER) == COOKIES
    only_v2 = PrivateKeyring({"v2": pairs["v2"][0]})     # v1's private key deleted after rotation
    assert only_v2.decrypt(enc2, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER) == COOKIES
    with pytest.raises(CredentialCryptoError):
        only_v2.decrypt(enc, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)


def test_a_row_is_only_ever_unwrapped_with_the_key_its_version_names(pub, priv):
    enc = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    mislabeled = EncryptedCredential(enc.ciphertext, enc.nonce, enc.aad_format, enc.wrapped_data_key, "v2")
    with pytest.raises(CredentialCryptoError):
        priv.decrypt(mislabeled, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    wrong_format = EncryptedCredential(enc.ciphertext, enc.nonce, "fi-cred-v0", enc.wrapped_data_key, "v1")
    with pytest.raises(CredentialCryptoError):
        priv.decrypt(wrong_format, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)


def test_nothing_secret_appears_in_reprs_logs_or_pickles(pub, priv, caplog, pairs):
    enc = pub.encrypt(COOKIES, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)
    text = repr(enc) + str(enc) + f"{enc}" + repr(pub) + repr(priv)
    for secret in (COOKIES["espn_s2"], COOKIES["swid"], enc.ciphertext.hex(), pairs["v1"][0][:40]):
        assert secret not in text
    assert "PRIVATE KEY" not in text
    with pytest.raises(TypeError):
        pickle.dumps(priv)
    with pytest.raises(TypeError):
        pickle.dumps(pub)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(CredentialCryptoError):
            priv.decrypt(enc, league_id=LEAGUE, credential_id=CRED, owner_user_id="22222222-2222-4222-8222-222222222222")
    assert COOKIES["espn_s2"] not in caplog.text and "PRIVATE KEY" not in caplog.text


def test_payload_must_be_the_two_cookie_fields(pub):
    for bad in ({}, {"espn_s2": "x"}, {"espn_s2": "x", "swid": "y", "extra": 1}, {"espn_s2": 1, "swid": "y"}, "x"):
        with pytest.raises(CredentialCryptoError):
            pub.encrypt(bad, league_id=LEAGUE, credential_id=CRED, owner_user_id=OWNER)


def test_settings_collect_versioned_public_keys(monkeypatch, pairs):
    from app.config import get_settings
    monkeypatch.setenv("CREDENTIALS_PUBKEY_V1", pairs["v1"][1])
    monkeypatch.setenv("CREDENTIALS_PUBKEY_V3", pairs["v2"][1])
    monkeypatch.delenv("CREDENTIALS_WRAP_KEY_VERSION", raising=False)
    s = get_settings()
    assert set(s.credential_public_keys) == {"v1", "v3"} and s.credential_wrap_key_version == "v3"
    monkeypatch.setenv("CREDENTIALS_WRAP_KEY_VERSION", "v1")
    assert get_settings().credential_wrap_key_version == "v1"
    assert "BEGIN PUBLIC KEY" not in repr(get_settings())
    assert PublicKeyring(s.credential_public_keys).current_version == "v3"


def test_keypair_generator_writes_private_key_to_a_file_only(tmp_path):
    out = tmp_path / "k.pem"
    r = subprocess.run([sys.executable, "scripts/gen_credential_keypair.py", "--private-key-file", str(out)],
                       capture_output=True, text=True, cwd=BACKEND)
    assert r.returncode == 0, r.stderr
    assert "BEGIN PUBLIC KEY" in r.stdout and "PRIVATE KEY" not in r.stdout
    assert "BEGIN PRIVATE KEY" in out.read_text() and (out.stat().st_mode & 0o777) == 0o600
    serialization.load_pem_private_key(out.read_bytes(), password=None)
    again = subprocess.run([sys.executable, "scripts/gen_credential_keypair.py", "--private-key-file", str(out)],
                           capture_output=True, text=True, cwd=BACKEND)
    assert again.returncode == 2 and again.stdout == ""          # never overwrites a key
