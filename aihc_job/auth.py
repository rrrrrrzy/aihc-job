"""BCE (Baidu Cloud) request signing -- ``bce-auth-v1``.

Implemented from the algorithm documented at
https://cloud.baidu.com/doc/AIHC/s/4maz04s1c so the package stays free of the
``bce-python-sdk`` dependency (only ``requests`` is required at runtime).

Authorization header layout::

    bce-auth-v1/{accessKeyId}/{timestamp}/{expireSeconds}/{signedHeaders}/{signature}

The AIHC docs sign only ``host`` and ``x-bce-date``; ``sign()`` defaults to that
pair but accepts an explicit ``headers_to_sign`` list for other services.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Iterable, Mapping
from urllib.parse import quote

_AUTH_VERSION = "bce-auth-v1"
_DEFAULT_EXPIRE_SECONDS = 1800
# Per BCE spec: only these characters survive percent-encoding un-escaped.
_SAFE = "-_.~"
_SAFE_WITH_SLASH = _SAFE + "/"


def _encode(value: str, keep_slash: bool = False) -> str:
    return quote(str(value), safe=_SAFE_WITH_SLASH if keep_slash else _SAFE)


def _hmac_sha256_hex(key: str | bytes, message: str) -> str:
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    return hmac.new(key_bytes, message.encode("utf-8"), hashlib.sha256).hexdigest()


def utc_timestamp(epoch_seconds: float | None = None) -> str:
    """Return an ISO-8601 UTC timestamp of the shape BCE expects (``...Z``)."""
    t = time.gmtime(time.time() if epoch_seconds is None else epoch_seconds)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)


def canonical_uri(path: str) -> str:
    """Percent-encode the request path, leaving ``/`` separators intact."""
    return _encode(path or "/", keep_slash=True)


def canonical_query_string(params: Mapping[str, object] | None) -> str:
    """Sorted ``k=v`` pairs, both sides percent-encoded; ``authorization`` skipped."""
    if not params:
        return ""
    pairs = [
        f"{_encode(k)}={_encode('' if v is None else v)}"
        for k, v in params.items()
        if k.lower() != "authorization"
    ]
    pairs.sort()
    return "&".join(pairs)


def canonical_headers(
    headers: Mapping[str, object], headers_to_sign: Iterable[str]
) -> tuple[str, list[str]]:
    """Build the canonical header block plus the ``signedHeaders`` list.

    Header names are lowercased and values trimmed before encoding; anything
    named ``x-bce-*`` is always signed, per the BCE spec.
    """
    wanted = {h.strip().lower() for h in headers_to_sign}
    encoded: list[str] = []
    for name, value in headers.items():
        lowered = str(name).strip().lower()
        if lowered in wanted or lowered.startswith("x-bce-"):
            encoded.append(f"{_encode(lowered)}:{_encode(str(value).strip())}")
    encoded.sort()
    return "\n".join(encoded), [item.split(":", 1)[0] for item in encoded]


def sign(
    access_key: str,
    secret_key: str,
    method: str,
    path: str,
    headers: Mapping[str, object],
    params: Mapping[str, object] | None = None,
    timestamp: str | None = None,
    expire_seconds: int = _DEFAULT_EXPIRE_SECONDS,
    headers_to_sign: Iterable[str] | None = None,
) -> str:
    """Return the value for the ``Authorization`` header.

    ``timestamp`` must be the same value sent as ``x-bce-date``; pass it
    explicitly (tests do) or let the caller's header dict supply it.
    """
    if timestamp is None:
        timestamp = str(headers.get("x-bce-date") or headers.get("X-Bce-Date") or utc_timestamp())
    if headers_to_sign is None:
        headers_to_sign = ("host", "x-bce-date")

    auth_prefix = f"{_AUTH_VERSION}/{access_key}/{timestamp}/{int(expire_seconds)}"
    signing_key = _hmac_sha256_hex(secret_key, auth_prefix)

    header_block, signed_names = canonical_headers(headers, headers_to_sign)
    string_to_sign = "\n".join(
        [
            method.upper(),
            canonical_uri(path),
            canonical_query_string(params),
            header_block,
        ]
    )
    signature = _hmac_sha256_hex(signing_key, string_to_sign)
    return f"{auth_prefix}/{';'.join(signed_names)}/{signature}"
