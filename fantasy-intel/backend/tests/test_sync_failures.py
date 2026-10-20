"""Failure classification (spec §3.6): what a job does with each kind of error."""
import psycopg
import pytest
import requests
from espn_api.requests.espn_requests import ESPNAccessDenied, ESPNInvalidLeague, ESPNUnknownError

from app.analytics.validation import ValidationError
from app.db.r2 import R2Error
from app.sync.failures import Outcome, classify


@pytest.mark.parametrize("err, authenticated, expected", [
    (requests.ConnectionError("dns"), False, Outcome("retry", "network")),
    (requests.Timeout("slow"), True, Outcome("retry", "network")),
    (ESPNUnknownError("ESPN returned an HTTP 503"), False, Outcome("retry", "provider_5xx")),
    (RuntimeError("ESPN returned HTTP 502"), False, Outcome("retry", "provider_5xx")),
    (ESPNUnknownError("ESPN returned an HTTP 429"), False, Outcome("retry", "provider_429", pause_provider=True)),
    (R2Error(503, "SlowDown", "k"), False, Outcome("retry", "storage")),
    (R2Error(None, "ChecksumMismatch", "k"), False, Outcome("retry", "storage")),
    (psycopg.OperationalError("conn reset"), False, Outcome("retry", "control_plane")),
    # an authenticated 401/403 is ambiguous: the credential is suspect, the job retries
    (ESPNAccessDenied("League 1 cannot be accessed with the provided credentials"), True, Outcome("auth", "provider_auth")),
    (ESPNUnknownError("ESPN returned an HTTP 403"), True, Outcome("auth", "provider_auth")),
    # without cookies it is the provider saying the league is private: restrict (safe direction)
    (ESPNAccessDenied("espn_s2 and swid are required"), False, Outcome("private", "provider_private")),
    (ESPNInvalidLeague("League 1 does not exist"), True, Outcome("missing", "provider_not_found")),
    (ESPNInvalidLeague("League 1 does not exist"), False, Outcome("missing", "provider_not_found")),
    (KeyError("teams"), False, Outcome("dead", "parse_error")),
    (TypeError("NoneType"), False, Outcome("dead", "parse_error")),
    (ValidationError([]), False, Outcome("dead", "validation_error")),
    (ZeroDivisionError(), False, Outcome("retry", "unexpected")),
])
def test_every_row_of_the_failure_table(err, authenticated, expected):
    assert classify(err, authenticated=authenticated) == expected


def test_parse_errors_are_only_final_when_they_come_from_our_parsing():
    """A KeyError raised by the HTTP layer (a malformed response mid-transfer) is still a provider problem."""
    assert classify(KeyError("x"), authenticated=False, stage="fetch") == Outcome("retry", "provider_response")
    assert classify(KeyError("x"), authenticated=False, stage="build") == Outcome("dead", "parse_error")


def test_the_outcome_never_carries_the_error_message():
    out = classify(ESPNAccessDenied("League 123456 cannot be accessed"), authenticated=True)
    assert "123456" not in repr(out)
