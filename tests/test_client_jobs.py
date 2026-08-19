"""Transport + job-manager behaviour, driven by a stub requests.Session."""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from aihc_job.client import AihcClient
from aihc_job.config import Config
from aihc_job.errors import ApiError, ConfigError, JobFailed, TemplateError, WaitTimeout
from aihc_job.jobs import JobManager, job_elapsed, job_started_at, summarize_job


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.content = self.text.encode()

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Returns queued responses and records the requests that produced them."""

    def __init__(self, *responses: FakeResponse):
        self.queue = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "body": json.loads(data) if data else None,
            }
        )
        if not self.queue:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        response = self.queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_client(*responses):
    config = Config(
        access_key="ak", secret_key="sk", region="cn-bj", pool="cce-1", queue="default", retries=1
    )
    session = FakeSession(*responses)
    return AihcClient(config, session=session), session


def make_manager(*responses):
    client, session = make_client(*responses)
    return JobManager(client), session


# ------------------------------------------------------------------- transport


def test_request_is_signed_and_query_encoded_once():
    client, session = make_client(FakeResponse(200, {"jobId": "job-1"}))
    client.create_job("cce-1", "default", {"name": "x"})
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == (
        "https://aihc.bj.baidubce.com/?action=CreateJob&queueID=default&resourcePoolId=cce-1"
    )
    assert call["headers"]["Authorization"].startswith("bce-auth-v1/ak/")
    assert call["headers"]["X-API-Version"] == "v2"  # job actions use this spelling
    assert call["headers"]["x-bce-date"] in call["headers"]["Authorization"]
    assert call["body"] == {"name": "x"}


def test_pool_actions_use_get_and_the_version_header():
    client, session = make_client(FakeResponse(200, {"resourcePools": []}))
    client.describe_resource_pools("common")
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["headers"]["version"] == "v2"
    assert "X-API-Version" not in call["headers"]
    assert call["body"] is None


def test_empty_params_are_dropped_from_the_request():
    client, session = make_client(FakeResponse(200, {}))
    client.describe_jobs("cce-1", queue="", status=None, pageSize=10)
    assert session.calls[0]["body"] == {"pageSize": 10}


def test_error_response_is_decoded_into_apierror():
    client, _ = make_client(
        FakeResponse(
            400,
            {
                "code": "cce.warning.GetAIJobByJobIdFailed",
                "message": "job not found",
                "requestId": "req-9",
            },
        )
    )
    with pytest.raises(ApiError) as excinfo:
        client.describe_job("cce-1", "default", "job-missing")
    error = excinfo.value
    assert error.status_code == 400
    assert error.code == "cce.warning.GetAIJobByJobIdFailed"
    assert error.request_id == "req-9"
    assert "job not found" in str(error)


def test_non_json_error_body_is_preserved():
    client, _ = make_client(FakeResponse(502, None, text="<html>bad gateway</html>"))
    with pytest.raises(ApiError, match="HTTP 502"):
        client.describe_jobs("cce-1")


def test_5xx_is_retried_then_succeeds(monkeypatch):
    client, session = make_client(FakeResponse(503, {}), FakeResponse(200, {"totalCount": 0}))
    client.config.retries = 2
    monkeypatch.setattr(client, "_sleep", lambda attempt: None)
    assert client.describe_jobs("cce-1") == {"totalCount": 0}
    assert len(session.calls) == 2


def test_4xx_is_not_retried(monkeypatch):
    client, session = make_client(FakeResponse(403, {"code": "AccessDenied"}))
    client.config.retries = 3
    monkeypatch.setattr(client, "_sleep", lambda attempt: None)
    with pytest.raises(ApiError):
        client.describe_jobs("cce-1")
    assert len(session.calls) == 1


def test_network_error_becomes_apierror(monkeypatch):
    client, _ = make_client(requests.ConnectionError("no route to host"))
    monkeypatch.setattr(client, "_sleep", lambda attempt: None)
    with pytest.raises(ApiError, match="NetworkError"):
        client.describe_jobs("cce-1")


def test_build_request_requires_credentials():
    from aihc_job.errors import ConfigError

    client = AihcClient(Config(region="cn-bj"))
    with pytest.raises(ConfigError, match="AIHC_AK"):
        client.build_request("DescribeJobs")


# ------------------------------------------------------------------ job layer


def test_submit_fills_pool_queue_and_echoes_them_back():
    manager, session = make_manager(FakeResponse(200, {"jobId": "job-1", "jobName": "demo"}))
    result = manager.submit({"name": "demo", "image": "x/y:1", "command": "sleep 1d"})
    assert result["jobId"] == "job-1"
    assert (result["pool"], result["queue"]) == ("cce-1", "default")
    assert session.calls[0]["body"]["queue"] == "default"


def test_submit_prefers_the_template_pool_over_the_config_default():
    manager, session = make_manager(FakeResponse(200, {"jobId": "job-2"}))
    manager.submit(
        {"name": "demo", "image": "x/y:1", "command": "sleep 1d", "pool": "cce-other", "queue": "q2"}
    )
    assert "resourcePoolId=cce-other" in session.calls[0]["url"]
    assert "queueID=q2" in session.calls[0]["url"]


def test_dry_run_sends_nothing():
    manager, session = make_manager()
    request = manager.dry_run({"name": "demo", "image": "x/y:1", "command": "sleep 1d"})
    assert request["action"] == "CreateJob"
    assert request["resourcePoolId"] == "cce-1"
    assert session.calls == []


def test_wait_returns_on_success():
    manager, _ = make_manager(
        FakeResponse(200, {"status": "Running"}),
        FakeResponse(200, {"status": "Succeeded"}),
    )
    seen: list[str] = []
    detail = manager.wait(
        "job-1", interval=0, on_status=lambda status, _d: seen.append(status)
    )
    assert detail["status"] == "Succeeded"
    assert seen == ["Running", "Succeeded"]


def test_wait_raises_jobfailed_with_a_reason():
    manager, _ = make_manager(
        FakeResponse(
            200,
            {
                "status": "Failed",
                "jobTimeLine": [{"conditionType": "Failed", "conditionMessage": "OOM killed"}],
            },
        )
    )
    with pytest.raises(JobFailed, match="OOM killed") as excinfo:
        manager.wait("job-1", interval=0)
    assert excinfo.value.status == "Failed"


def test_wait_until_running_stops_before_completion():
    manager, _ = make_manager(
        FakeResponse(200, {"status": "Created"}), FakeResponse(200, {"status": "Running"})
    )
    assert manager.wait("job-1", until="running", interval=0)["status"] == "Running"


def test_wait_timeout(monkeypatch):
    manager, _ = make_manager(*[FakeResponse(200, {"status": "Created"}) for _ in range(3)])
    clock = iter([0.0, 100.0, 200.0, 300.0, 400.0])
    monkeypatch.setattr("aihc_job.jobs.time.monotonic", lambda: next(clock))
    with pytest.raises(WaitTimeout, match="Created"):
        manager.wait("job-1", interval=0, timeout=5)


def test_wait_tolerates_transient_errors():
    manager, _ = make_manager(
        FakeResponse(500, {"message": "boom"}),
        FakeResponse(200, {"status": "Succeeded"}),
    )
    manager.client.config.retries = 1
    assert manager.wait("job-1", interval=0)["status"] == "Succeeded"


def test_default_pod_prefers_rank_zero():
    manager, _ = make_manager(
        FakeResponse(
            200,
            {
                "status": "Running",
                "pods": [
                    {"name": "demo-worker-1", "replicaType": "worker"},
                    {"name": "demo-master-0", "replicaType": "master"},
                ],
            },
        )
    )
    assert manager.default_pod("job-1") == "demo-master-0"


def test_default_pod_errors_when_no_pods_exist():
    manager, _ = make_manager(FakeResponse(200, {"status": "Created", "pods": []}))
    with pytest.raises(ApiError, match="no pods yet"):
        manager.default_pod("job-1")


def test_iter_logs_follows_markers_then_stops_when_terminal():
    manager, _ = make_manager(
        FakeResponse(200, {"status": "Running", "pods": [{"name": "demo-master-0"}]}),
        FakeResponse(200, {"logs": ["line 1"], "nextMarker": "m1"}),
        FakeResponse(200, {"logs": ["line 2"], "nextMarker": "m1"}),  # same marker -> no new pages
        FakeResponse(200, {"status": "Succeeded"}),
    )
    assert list(manager.iter_logs("job-1", follow=True, interval=0)) == ["line 1", "line 2"]


def test_iter_logs_without_follow_stops_at_the_last_page():
    manager, _ = make_manager(
        FakeResponse(200, {"status": "Running", "pods": [{"name": "p-master-0"}]}),
        FakeResponse(200, {"logs": ["a"], "nextMarker": ""}),
    )
    assert list(manager.iter_logs("job-1")) == ["a"]


def test_summarize_job_handles_single_and_multi_role_specs():
    single = summarize_job(
        {"jobId": "job-1", "name": "n", "status": "Running", "jobSpec": {"image": "i:1", "replicas": 4}}
    )
    assert single["replicas"] == 4
    multi = summarize_job({"jobid": "job-2", "jobSpec": {"Head": {"image": "i:1", "replicas": 1}}})
    assert (multi["jobId"], multi["replicas"]) == ("job-2", 1)


# ------------------------------------------------- queue tree / capacity matching

# Shape taken from a real DescribeQueues response: physical queues at the top level,
# the submittable Elastic queue nested under `children`, counts as strings ("62.0"),
# and `allocated` entries carrying an empty acceleratorType.
QUEUE_TREE = {
    "totalCount": 2,
    "queues": [
        {
            "queueId": "aihcq-parent-a",
            "queueName": "team-a800-1",
            "queueType": "Physical",
            "opened": True,
            "capability": {
                "cpuCores": "384",
                "memoryGi": "3072",
                "acceleratorCardList": [
                    {
                        "acceleratorCount": "24",
                        "acceleratorType": "NVIDIA A800-SXM4-80GB",
                        "acceleratorDescription": "baidu.com/a800_80g_cgpu",
                    }
                ],
            },
            "allocated": {
                "acceleratorCardList": [
                    {
                        "acceleratorCount": "8.0",
                        "acceleratorType": "",
                        "acceleratorDescription": "baidu.com/a800_80g_cgpu",
                    }
                ]
            },
            "children": [
                {
                    "queueId": "aihcq-child-a",
                    "queueName": "team-a800-1",
                    "queueType": "Elastic",
                    "opened": True,
                    "capability": {
                        "cpuCores": "384",
                        "memoryGi": "3072",
                        "acceleratorCardList": [
                            {
                                "acceleratorCount": "24",
                                "acceleratorDescription": "baidu.com/a800_80g_cgpu",
                            }
                        ],
                    },
                    "allocated": {
                        "acceleratorCardList": [
                            {
                                "acceleratorCount": "8.0",
                                "acceleratorDescription": "baidu.com/a800_80g_cgpu",
                            }
                        ]
                    },
                }
            ],
        },
        {
            "queueId": "aihcq-parent-b",
            "queueName": "team-4090",
            "queueType": "Physical",
            "opened": True,
            "capability": {
                "cpuCores": "576",
                "memoryGi": "3072",
                "acceleratorCardList": [
                    {"acceleratorCount": "24", "acceleratorDescription": "baidu.com/rtx_4090_cgpu"}
                ],
            },
            "allocated": {},
            "children": [
                {
                    "queueId": "aihcq-child-b",
                    "queueName": "team-4090",
                    "queueType": "Elastic",
                    "opened": False,  # closed: must never be offered
                    "capability": {
                        "acceleratorCardList": [
                            {
                                "acceleratorCount": "24",
                                "acceleratorDescription": "baidu.com/rtx_4090_cgpu",
                            }
                        ]
                    },
                    "allocated": {},
                }
            ],
        },
    ],
}


def test_queues_flattens_children_and_computes_free():
    manager, _ = make_manager(FakeResponse(200, QUEUE_TREE))
    rows = manager.queues("aihc-pool")
    assert [r["queueId"] for r in rows] == [
        "aihcq-parent-a",
        "aihcq-child-a",
        "aihcq-parent-b",
        "aihcq-child-b",
    ]
    child = next(r for r in rows if r["queueId"] == "aihcq-child-a")
    assert child["depth"] == 1
    assert child["parent"] == "aihcq-parent-a"
    assert child["capacity"]["baidu.com/a800_80g_cgpu"] == 24
    assert child["allocated"]["baidu.com/a800_80g_cgpu"] == 8  # parsed from "8.0"
    assert child["free"]["baidu.com/a800_80g_cgpu"] == 16
    assert (child["cpuCores"], child["memoryGi"]) == (384, 3072)


def test_find_queues_for_returns_only_open_leaves_that_fit():
    manager, _ = make_manager(FakeResponse(200, QUEUE_TREE))
    fits = manager.find_queues_for("aihc-pool", "baidu.com/a800_80g_cgpu", 8)
    # The parent is excluded (jobs go to the Elastic leaf), so only the child matches.
    assert [q["queueId"] for q in fits] == ["aihcq-child-a"]


def test_find_queues_for_excludes_too_small_and_closed_queues():
    manager, _ = make_manager(FakeResponse(200, QUEUE_TREE))
    assert manager.find_queues_for("aihc-pool", "baidu.com/a800_80g_cgpu", 17) == []

    manager, _ = make_manager(FakeResponse(200, QUEUE_TREE))
    # aihcq-child-b has 24 free 4090s but is closed.
    assert manager.find_queues_for("aihc-pool", "baidu.com/rtx_4090_cgpu", 1) == []


def test_find_queues_for_sorts_by_most_free():
    tree = json.loads(json.dumps(QUEUE_TREE))
    tree["queues"][1]["children"][0]["opened"] = True
    tree["queues"][1]["children"][0]["capability"]["acceleratorCardList"][0][
        "acceleratorDescription"
    ] = "baidu.com/a800_80g_cgpu"
    manager, _ = make_manager(FakeResponse(200, tree))
    fits = manager.find_queues_for("aihc-pool", "baidu.com/a800_80g_cgpu", 8)
    assert [q["queueId"] for q in fits] == ["aihcq-child-b", "aihcq-child-a"]  # 24 free, then 16


def test_submit_switches_queue_with_auto_queue():
    manager, session = make_manager(
        FakeResponse(200, QUEUE_TREE),  # find_queues_for
        FakeResponse(200, QUEUE_TREE),  # queues() for the report
        FakeResponse(200, {"jobId": "job-1"}),
    )
    manager.config.queue = "aihcq-child-a"
    manager.config.queue_pool = "aihc-pool"
    tree = json.loads(json.dumps(QUEUE_TREE))
    tree["queues"][1]["children"][0]["opened"] = True
    tree["queues"][1]["children"][0]["capability"]["acceleratorCardList"][0][
        "acceleratorDescription"
    ] = "baidu.com/a800_80g_cgpu"
    manager.client.session.queue[0] = FakeResponse(200, tree)
    manager.client.session.queue[1] = FakeResponse(200, tree)

    result = manager.submit(
        {"name": "demo", "image": "x/y:1", "command": "c", "gpu": "a800:20"}, auto_queue=True
    )
    # child-a has 16 free, child-b has 24 -> must move to child-b
    assert result["queue"] == "aihcq-child-b"
    assert "queueID=aihcq-child-b" in session.calls[-1]["url"]
    assert manager.last_queue_report["chosen"] == "aihcq-child-b"


def test_submit_refuses_when_capacity_is_short_and_auto_is_disabled():
    manager, session = make_manager(FakeResponse(200, QUEUE_TREE), FakeResponse(200, QUEUE_TREE))
    manager.config.queue = "aihcq-child-a"
    manager.config.queue_pool = "aihc-pool"
    with pytest.raises(ConfigError, match="needs 20 x baidu.com/a800_80g_cgpu"):
        manager.submit(
            {"name": "demo", "image": "x/y:1", "command": "c", "gpu": "a800:20"},
            auto_queue=False,
        )
    assert not any("CreateJob" in c["url"] for c in session.calls)  # nothing was created


def test_capacity_check_accounts_for_replicas():
    from aihc_job.models import build_create_job_body, gpu_requirement

    body = build_create_job_body(
        {"name": "d", "image": "x/y:1", "command": "c", "gpu": "a800:8", "replicas": 2}
    )
    assert gpu_requirement(body) == ("baidu.com/a800_80g_cgpu", 16.0)


def test_capacity_check_is_skipped_without_a_queue_pool():
    manager, session = make_manager(FakeResponse(200, {"jobId": "job-1"}))
    manager.config.queue_pool = ""
    manager.submit({"name": "demo", "image": "x/y:1", "command": "c", "gpu": "a800:8"})
    assert len(session.calls) == 1  # no DescribeQueues call
    assert manager.last_queue_report["checked"] is False


def test_capacity_check_never_blocks_on_a_queue_api_error():
    manager, session = make_manager(
        FakeResponse(500, {"message": "boom"}), FakeResponse(200, {"jobId": "job-1"})
    )
    manager.config.queue_pool = "aihc-pool"
    result = manager.submit({"name": "demo", "image": "x/y:1", "command": "c", "gpu": "a800:8"})
    assert result["jobId"] == "job-1"  # submitted anyway
    assert manager.last_queue_report["checked"] is False


def test_no_accelerator_request_skips_the_check():
    manager, session = make_manager(FakeResponse(200, {"jobId": "job-1"}))
    manager.config.queue_pool = "aihc-pool"
    manager.submit({"name": "demo", "image": "x/y:1", "command": "c", "gpu": "cpu=8,memory=32"})
    assert len(session.calls) == 1


def test_capacity_error_lists_alternatives_when_some_queue_fits():
    tree = json.loads(json.dumps(QUEUE_TREE))
    tree["queues"][1]["children"][0]["opened"] = True
    tree["queues"][1]["children"][0]["capability"]["acceleratorCardList"][0][
        "acceleratorDescription"
    ] = "baidu.com/a800_80g_cgpu"
    manager, _ = make_manager(FakeResponse(200, tree), FakeResponse(200, tree))
    manager.config.queue = "aihcq-child-a"
    manager.config.queue_pool = "aihc-pool"
    with pytest.raises(ConfigError) as excinfo:
        manager.submit(
            {"name": "d", "image": "x/y:1", "command": "c", "gpu": "a800:20"}, auto_queue=False
        )
    message = str(excinfo.value)
    assert "Queues that fit:" in message
    assert "aihcq-child-b" in message
    assert "24/24 free" in message


# ------------------------------------------------------------------ monitoring


def _job_detail(status: str = "Running", queue: str = "aihcq-real", **extra):
    detail = {
        "jobId": "job-1",
        "name": "demo",
        "status": status,
        "queue": queue,
        "createdAt": "2026-08-19T05:07:05Z",
        "finishedAt": "",
        "gpuCount": 8,
        "nodeCount": 1,
        "gpuUtilizationPercent": 51.25,
        "gpuMemoryUtilizationPercent": 40.15,
        "runTimeNanoseconds": 2040000000000,
        "jobTimeLine": [
            {"conditionType": "Created", "time": "2026-08-19T05:07:05Z"},
            {"conditionType": "Running", "time": "2026-08-19T05:07:14Z"},
        ],
        "pods": [
            {
                "name": "job-1-master-0",
                "replicaType": "master",
                "status": "Running",
                "nodeName": "192.168.80.201",
                "restartCount": 0,
            }
        ],
    }
    detail.update(extra)
    return detail


def _metric_page(*values: float, pod: str = "job-1-master-0"):
    return {
        "jobId": "job-1",
        "metrics": [
            {
                "podName": pod,
                # The API returns times and values as strings.
                "metrics": [
                    {"time": 1787117100 + 30 * i, "value": str(v)} for i, v in enumerate(values)
                ],
            }
        ],
    }


def test_metrics_uses_the_jobs_own_queue_not_the_configured_one():
    """DescribeJobMetrics 403s on any queue but the job's; auto-queue can move a job."""
    manager, session = make_manager(
        FakeResponse(200, _job_detail(queue="aihcq-real")),
        FakeResponse(200, _metric_page(70.0, 80.0)),
    )
    manager.metrics("job-1", "GpuUsage", start_time=1787117100, end_time=1787117130, step=30)
    assert "queueID=default" in session.calls[0]["url"]  # DescribeJob tolerates it
    assert "queueID=aihcq-real" in session.calls[1]["url"]
    assert "action=DescribeJobMetrics" in session.calls[1]["url"]


def test_metrics_sends_the_time_window_as_strings():
    """Integers come back as 400 MalformedJSON: the server unmarshals strings."""
    manager, session = make_manager(
        FakeResponse(200, _job_detail()), FakeResponse(200, _metric_page(1.0))
    )
    manager.metrics("job-1", "GpuUsage", start_time=1787117100.4, end_time=1787117130, step=30.0)
    body = session.calls[1]["body"]
    assert body == {
        "jobId": "job-1",
        "metricType": "GpuUsage",
        "startTime": "1787117100",
        "endTime": "1787117130",
        "timeStep": "30",
    }


def test_metrics_parses_a_series_per_pod():
    manager, _ = make_manager(
        FakeResponse(200, _job_detail()), FakeResponse(200, _metric_page(70.5, 80.25))
    )
    series = manager.metrics("job-1", "GpuUsage", start_time=1, end_time=2, step=30)
    assert series["job-1-master-0"] == [
        {"time": 1787117100.0, "value": 70.5},
        {"time": 1787117130.0, "value": 80.25},
    ]


def test_unknown_metric_type_is_rejected_before_any_call():
    manager, session = make_manager()
    with pytest.raises(TemplateError) as excinfo:
        manager.metrics("job-1", "GpuUsagePercent")
    assert "unknown metric type" in str(excinfo.value)
    assert session.calls == []


def test_snapshot_survives_one_metric_type_failing():
    """RDMA metrics are missing on non-RDMA nodes; that must not lose the GPU numbers."""
    manager, _ = make_manager(
        FakeResponse(200, _job_detail()),
        FakeResponse(200, _metric_page(70.0, 90.0)),
        FakeResponse(400, {"code": "InvalidParam", "message": "no such metric"}),
    )
    snapshot = manager.metrics_snapshot(
        "job-1", types=("GpuUsage", "RDMASendDataRate"), window=60, step=30
    )
    assert snapshot["latest"]["job-1-master-0"]["GpuUsage"] == 90.0
    assert "RDMASendDataRate" in snapshot["errors"]
    assert snapshot["endTime"] - snapshot["startTime"] == 60


def test_watch_yields_status_plus_per_pod_load_and_stops_when_terminal():
    manager, session = make_manager(
        FakeResponse(200, _job_detail("Running")),
        FakeResponse(200, _metric_page(60.0, 75.5)),
        FakeResponse(200, _job_detail("Succeeded", finishedAt="2026-08-19T05:41:14Z")),
        FakeResponse(200, _metric_page(0.0)),
    )
    rounds = list(manager.watch("job-1", types=("GpuUsage",), interval=0))
    assert [r["status"] for r in rounds] == ["Running", "Succeeded"]
    first = rounds[0]
    assert first["queue"] == "aihcq-real"
    assert first["gpuUtil"] == 51.25  # free of charge from DescribeJob
    assert first["pods"][0]["metrics"]["GpuUsage"] == 75.5
    assert first["elapsedSeconds"] == 2040.0
    # A Succeeded job is not sampled: the metrics response above is left unused.
    assert len(session.calls) == 3


def test_watch_does_not_sample_a_queued_job():
    manager, session = make_manager(
        FakeResponse(200, _job_detail("Created", pods=[])),
    )
    rounds = list(manager.watch("job-1", once=True))
    assert rounds[0]["metrics"] == {}
    assert rounds[0]["pods"] == []
    assert len(session.calls) == 1


def test_watch_keeps_a_pod_that_only_metrics_knows_about():
    manager, _ = make_manager(
        FakeResponse(200, _job_detail("Running")),
        FakeResponse(200, _metric_page(42.0, pod="job-1-worker-9")),
    )
    rounds = list(manager.watch("job-1", types=("GpuUsage",), once=True))
    names = [pod["name"] for pod in rounds[0]["pods"]]
    assert names == ["job-1-master-0", "job-1-worker-9"]


def test_watch_times_out():
    manager, _ = make_manager(
        FakeResponse(200, _job_detail("Created", pods=[])),
        FakeResponse(200, _job_detail("Created", pods=[])),
    )
    with pytest.raises(WaitTimeout):
        list(manager.watch("job-1", interval=0, timeout=-1))


def test_elapsed_falls_back_to_the_timeline_without_the_api_counter():
    detail = _job_detail(runTimeNanoseconds=0, finishedAt="2026-08-19T05:41:14Z")
    assert job_elapsed(detail) == 2040.0  # 05:07:14Z -> 05:41:14Z
    assert job_started_at(detail) == "2026-08-19T05:07:14Z"
    assert job_elapsed({"runTimeNanoseconds": 0}) is None


def test_wrong_hardware_queue_is_reported_as_such_not_as_missing():
    """Queues are split by chip: an A800 queue offers no 4090, which is not 'not found'."""
    manager, _ = make_manager(FakeResponse(200, QUEUE_TREE), FakeResponse(200, QUEUE_TREE))
    manager.config.queue = "aihcq-child-a"  # A800 only
    manager.config.queue_pool = "aihc-pool"
    with pytest.raises(ConfigError) as excinfo:
        manager.submit(
            {"name": "d", "image": "x/y:1", "command": "c", "gpu": "4090:2"}, auto_queue=False
        )
    assert "has no baidu.com/rtx_4090_cgpu at all" in str(excinfo.value)
