"""Signing tests.

There is no published AIHC signature fixture, so these lock down the algorithm's
observable properties (structure, determinism, and each canonicalization step)
against the documented spec at https://cloud.baidu.com/doc/AIHC/s/4maz04s1c.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from aihc_job import auth

AK = "ALTAKxxxxxxxxxxxxxxx"
SK = "b903d4xxxxxxxxxxxxxxxxxxxxxxxxxx"
TS = "2026-08-19T03:04:05Z"


def _expected(string_to_sign: str, prefix: str) -> str:
    key = hmac.new(SK.encode(), prefix.encode(), hashlib.sha256).hexdigest()
    return hmac.new(key.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()


def test_authorization_header_layout():
    header = auth.sign(
        AK,
        SK,
        "POST",
        "/",
        {"Host": "aihc.bj.baidubce.com", "x-bce-date": TS},
        {"action": "CreateJob"},
        timestamp=TS,
    )
    parts = header.split("/")
    assert parts[0] == "bce-auth-v1"
    assert parts[1] == AK
    assert parts[2] == TS
    assert parts[3] == "1800"
    assert parts[4] == "host;x-bce-date"
    assert re.fullmatch(r"[0-9a-f]{64}", parts[5])


def test_signature_matches_manual_derivation():
    headers = {"Host": "aihc.bj.baidubce.com", "x-bce-date": TS, "Content-Type": "application/json"}
    params = {"action": "CreateJob", "resourcePoolId": "cce-1uji3ib5", "queueID": "default"}
    prefix = f"bce-auth-v1/{AK}/{TS}/1800"
    string_to_sign = "\n".join(
        [
            "POST",
            "/",
            "action=CreateJob&queueID=default&resourcePoolId=cce-1uji3ib5",
            f"host:aihc.bj.baidubce.com\nx-bce-date:{TS.replace(':', '%3A')}",
        ]
    )
    header = auth.sign(AK, SK, "post", "/", headers, params, timestamp=TS)
    assert header.endswith(_expected(string_to_sign, prefix))


def test_content_type_is_not_signed_but_x_bce_headers_are():
    headers = {"Host": "h", "x-bce-date": TS, "Content-Type": "application/json"}
    block, names = auth.canonical_headers(headers, ("host", "x-bce-date"))
    assert names == ["host", "x-bce-date"]
    assert "content-type" not in block

    block, names = auth.canonical_headers({"Host": "h", "x-bce-request-id": "r"}, ("host",))
    assert names == ["host", "x-bce-request-id"]


def test_query_string_is_sorted_and_encoded():
    assert auth.canonical_query_string({"b": "2", "a": "1"}) == "a=1&b=2"
    assert auth.canonical_query_string({"action": "X", "authorization": "secret"}) == "action=X"
    assert auth.canonical_query_string({"k": "a/b c"}) == "k=a%2Fb%20c"
    assert auth.canonical_query_string({"k": None}) == "k="
    assert auth.canonical_query_string(None) == ""


def test_canonical_uri_keeps_slashes():
    assert auth.canonical_uri("/") == "/"
    assert auth.canonical_uri("/v1/jobs/a b") == "/v1/jobs/a%20b"


def test_header_values_are_trimmed_and_lowercased():
    block, _ = auth.canonical_headers({"  HOST  ": "  example.com  "}, ("host",))
    assert block == "host:example.com"


def test_timestamp_format():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", auth.utc_timestamp())
    assert auth.utc_timestamp(0) == "1970-01-01T00:00:00Z"


def test_signature_is_deterministic_for_a_fixed_timestamp():
    args = ("POST", "/", {"Host": "h", "x-bce-date": TS}, {"action": "A"})
    assert auth.sign(AK, SK, *args, timestamp=TS) == auth.sign(AK, SK, *args, timestamp=TS)
    assert auth.sign(AK, SK, *args, timestamp=TS) != auth.sign(
        AK, SK, "GET", "/", {"Host": "h", "x-bce-date": TS}, {"action": "A"}, timestamp=TS
    )
