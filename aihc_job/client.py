"""Thin transport layer over the AIHC OpenAPI (百舸 OpenAPI v2).

Every operation is a single request to ``/`` with an ``action`` query parameter;
the interesting differences between operations are only:

* HTTP verb -- resource-pool/queue reads are ``GET``, job operations ``POST``
* API version header -- job actions want ``X-API-Version: v2``, the rest ``version: v2``

:class:`AihcClient.call` handles signing, retries, and error decoding; the typed
wrappers below just name the actions and marshal parameters.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Mapping

import requests

from . import auth
from .config import Config
from .errors import ApiError

log = logging.getLogger("aihc_job")

_RETRY_STATUS = {429, 500, 502, 503, 504}


def _is_job_action(action: str) -> bool:
    """Job actions want ``X-API-Version: v2``; pool/queue actions want ``version: v2``."""
    return "Job" in action


class AihcClient:
    """Signed HTTP client for one region / credential pair."""

    def __init__(self, config: Config, session: requests.Session | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.user_agent = "aihc-job/0.1.0 (python)"

    # ---------------------------------------------------------------- core

    def build_request(
        self,
        action: str,
        *,
        method: str = "POST",
        query: Mapping[str, object] | None = None,
        body: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Return the fully signed request description (used by ``--dry-run``)."""
        self.config.require_credentials()
        params: dict[str, object] = {"action": action}
        for key, value in (query or {}).items():
            if value is not None and value != "":
                params[key] = value

        timestamp = auth.utc_timestamp()
        headers: dict[str, str] = {
            "Host": self.config.host,
            "x-bce-date": timestamp,
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        # Two spellings exist in the docs; sending both is harmless and covers
        # job actions (X-API-Version) plus pool/queue actions (version).
        if _is_job_action(action):
            headers["X-API-Version"] = "v2"
        else:
            headers["version"] = "v2"

        headers["Authorization"] = auth.sign(
            self.config.access_key,
            self.config.secret_key,
            method,
            "/",
            headers,
            params,
            timestamp=timestamp,
        )
        payload = None if body is None else json.dumps(body, ensure_ascii=False)
        # Encode the query string ourselves so the bytes on the wire are exactly
        # the ones that went into the signature.
        query_string = auth.canonical_query_string(params)
        return {
            "method": method.upper(),
            "url": f"{self.config.base_url}/?{query_string}",
            "params": params,
            "headers": headers,
            "body": payload,
        }

    def call(
        self,
        action: str,
        *,
        method: str = "POST",
        query: Mapping[str, object] | None = None,
        body: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Perform an action and return the decoded JSON response."""
        last_error: ApiError | requests.RequestException | None = None
        attempts = max(1, self.config.retries)
        for attempt in range(1, attempts + 1):
            # Re-sign each attempt: the signature is timestamped and expires.
            request = self.build_request(action, method=method, query=query, body=body)
            log.debug("%s %s params=%s", request["method"], request["url"], request["params"])
            try:
                response = self.session.request(
                    request["method"],
                    request["url"],
                    headers=request["headers"],
                    data=(request["body"] or "").encode("utf-8") if request["body"] else None,
                    timeout=self.config.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == attempts:
                    raise ApiError(0, code="NetworkError", message=str(exc), action=action) from exc
                self._sleep(attempt)
                continue

            if response.status_code < 300:
                return self._decode(response, action)

            error = self._to_api_error(response, action)
            last_error = error
            if response.status_code in _RETRY_STATUS and attempt < attempts:
                log.warning(
                    "%s: HTTP %s, retrying (%s/%s)", action, response.status_code, attempt, attempts
                )
                self._sleep(attempt)
                continue
            raise error

        assert last_error is not None  # unreachable: loop always returns or raises
        raise last_error

    def _sleep(self, attempt: int) -> None:
        time.sleep(min(8.0, 0.5 * 2 ** (attempt - 1)) * (0.8 + 0.4 * random.random()))

    @staticmethod
    def _decode(response: requests.Response, action: str) -> dict[str, Any]:
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise ApiError(
                response.status_code,
                code="InvalidResponse",
                message="response body was not JSON",
                action=action,
                raw=response.text[:2000],
            ) from exc
        return data if isinstance(data, dict) else {"result": data}

    @staticmethod
    def _to_api_error(response: requests.Response, action: str) -> ApiError:
        code = message = request_id = ""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            code = str(payload.get("code") or payload.get("Code") or "")
            message = str(payload.get("message") or payload.get("Message") or "")
            request_id = str(payload.get("requestId") or payload.get("RequestId") or "")
        return ApiError(
            response.status_code,
            code=code,
            message=message,
            request_id=request_id,
            action=action,
            raw=response.text[:2000],
        )

    # ------------------------------------------------------- job actions

    def create_job(self, pool: str, queue: str, body: Mapping[str, object]) -> dict[str, Any]:
        return self.call("CreateJob", query={"resourcePoolId": pool, "queueID": queue}, body=body)

    def describe_jobs(self, pool: str, **body: object) -> dict[str, Any]:
        return self.call("DescribeJobs", query={"resourcePoolId": pool}, body=_clean(body))

    def describe_job(
        self, pool: str, queue: str, job_id: str, need_detail: bool = False
    ) -> dict[str, Any]:
        return self.call(
            "DescribeJob",
            query={"resourcePoolId": pool, "queueID": queue},
            body={"jobId": job_id, "needDetail": need_detail},
        )

    def stop_job(self, pool: str, queue: str, job_id: str) -> dict[str, Any]:
        return self.call(
            "StopJob", query={"resourcePoolId": pool, "queueID": queue}, body={"jobId": job_id}
        )

    def delete_job(self, pool: str, queue: str, job_id: str) -> dict[str, Any]:
        return self.call(
            "DeleteJob", query={"resourcePoolId": pool, "queueID": queue}, body={"jobId": job_id}
        )

    def modify_job(self, pool: str, queue: str, job_id: str, priority: str) -> dict[str, Any]:
        return self.call(
            "ModifyJob",
            query={"resourcePoolId": pool, "queueID": queue},
            body={"jobId": job_id, "priority": priority},
        )

    def describe_job_logs(self, pool: str, queue: str, **body: object) -> dict[str, Any]:
        return self.call(
            "DescribeJobLogs", query={"resourcePoolId": pool, "queueID": queue}, body=_clean(body)
        )

    def describe_job_events(self, pool: str, queue: str, **body: object) -> dict[str, Any]:
        return self.call(
            "DescribeJobEvents", query={"resourcePoolId": pool, "queueID": queue}, body=_clean(body)
        )

    def describe_job_pod_events(self, pool: str, queue: str, **body: object) -> dict[str, Any]:
        return self.call(
            "DescribeJobPodEvents",
            query={"resourcePoolId": pool, "queueID": queue},
            body=_clean(body),
        )

    def describe_job_nodes(self, pool: str, queue: str, job_id: str) -> dict[str, Any]:
        return self.call(
            "DescribeJobNodes",
            query={"resourcePoolId": pool, "queueID": queue},
            body={"jobId": job_id},
        )

    def describe_job_metrics(self, pool: str, queue: str, **body: object) -> dict[str, Any]:
        # queueID is mandatory here: a serverless pool answers 400 "queueID must be set"
        # without it, and 403 AccessDenied when it is not the job's own queue.
        return self.call(
            "DescribeJobMetrics",
            query={"resourcePoolId": pool, "queueID": queue},
            body=_clean(body),
        )

    # --------------------------------------------- pool / queue actions

    def describe_resource_pools(self, pool_type: str = "common", **query: object) -> dict[str, Any]:
        return self.call(
            "DescribeResourcePools",
            method="GET",
            query=_clean({"resourcePoolType": pool_type, **query}),
        )

    def describe_queues(self, pool: str, **query: object) -> dict[str, Any]:
        return self.call(
            "DescribeQueues", method="GET", query=_clean({"resourcePoolId": pool, **query})
        )


def _clean(values: Mapping[str, object]) -> dict[str, Any]:
    """Drop ``None``/empty-string entries so we never send placeholder params."""
    return {k: v for k, v in values.items() if v is not None and v != ""}
