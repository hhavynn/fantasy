"""app/db/r2.py: SigV4 signing against AWS's published example, and the object operations."""

import hashlib
from datetime import datetime, timezone

import pytest

from app.db import r2
from fake_r2 import FakeR2


@pytest.fixture
def fake():
    f = FakeR2()
    yield f
    f.close()


def test_sigv4_matches_aws_published_get_object_example():
    """AWS S3 docs, 'Signature Calculations for the Authorization Header', Example: GET Object."""
    headers = r2.sign("GET", "examplebucket.s3.amazonaws.com", "/test.txt", {}, {"Range": "bytes=0-9"},
                      r2.EMPTY_SHA256, "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                      "us-east-1", datetime(2013, 5, 24, tzinfo=timezone.utc))
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_config_repr_hides_the_secret():
    cfg = r2.R2Config("acct", "AKID", "super-secret")
    assert "super-secret" not in repr(cfg)


def test_put_get_list_delete_round_trip(fake, tmp_path):
    src = tmp_path / "a.duckdb"
    src.write_bytes(b"x" * 3_000_000)
    digest = r2.put_file(fake.config, "snapshots/a.duckdb", src)
    assert digest == hashlib.sha256(src.read_bytes()).hexdigest()
    r2.put_bytes(fake.config, "snapshots/current.json", b'{"k": 1}')
    assert r2.get_bytes(fake.config, "snapshots/current.json") == b'{"k": 1}'
    assert r2.list_keys(fake.config, "snapshots/") == ["snapshots/a.duckdb", "snapshots/current.json"]
    out = r2.download(fake.config, "snapshots/a.duckdb", tmp_path / "b.duckdb", expected_sha256=digest)
    assert out.read_bytes() == src.read_bytes()
    r2.delete(fake.config, "snapshots/a.duckdb")
    assert r2.list_keys(fake.config, "snapshots/") == ["snapshots/current.json"]
    assert all(auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDTEST/") for _, _, auth in fake.requests)


def test_download_rejects_a_checksum_mismatch_and_leaves_nothing_behind(fake, tmp_path):
    fake.objects["snapshots/a.duckdb"] = b"payload"
    with pytest.raises(r2.R2Error) as err:
        r2.download(fake.config, "snapshots/a.duckdb", tmp_path / "a.duckdb", expected_sha256="0" * 64)
    assert err.value.code == "ChecksumMismatch"
    assert list(tmp_path.iterdir()) == []


def test_errors_carry_status_and_code_but_no_secret(fake):
    with pytest.raises(r2.R2Error) as err:
        r2.get_bytes(fake.config, "snapshots/missing.json")
    assert (err.value.status, err.value.code) == (404, "NoSuchKey")
    assert "secret-test-key" not in str(err.value)


def test_parallel_ranged_download_reassembles_the_object(fake, tmp_path):
    body = bytes(range(256)) * 4000 + b"tail"                    # 1,024,004 bytes: uneven last range
    fake.objects["snapshots/big.duckdb"] = body
    out = r2.download(fake.config, "snapshots/big.duckdb", tmp_path / "big.duckdb",
                      expected_sha256=hashlib.sha256(body).hexdigest(), size=len(body), parts=8, min_parallel_bytes=1)
    assert out.read_bytes() == body
    assert len(fake.ranges) == 8 and sorted(fake.ranges)[0][0] == 0 and max(e for _, e in fake.ranges) == len(body) - 1


def test_a_server_ignoring_range_is_an_error_not_a_corrupt_file(fake, tmp_path):
    body = b"x" * 100_000
    fake.objects["k"] = body
    fake.ignore_range = True
    with pytest.raises(r2.R2Error) as err:
        r2.download(fake.config, "k", tmp_path / "k", size=len(body), parts=4, min_parallel_bytes=1)
    assert err.value.code == "RangeNotHonoured" and list(tmp_path.iterdir()) == []


def test_small_objects_use_one_stream(fake, tmp_path):
    fake.objects["k"] = b"small"
    r2.download(fake.config, "k", tmp_path / "k", size=5)
    assert fake.ranges == []
