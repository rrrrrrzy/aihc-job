"""CLI wiring: argument -> template overrides, output modes, exit codes."""

from __future__ import annotations

import json

import pytest

from aihc_job import cli
from aihc_job.client import AihcClient as RealAihcClient
from tests.test_client_jobs import FakeResponse, FakeSession


@pytest.fixture(autouse=True)
def stub_credentials(monkeypatch, tmp_path):
    # Isolate HOME and cwd so a real ~/.aihc/config* cannot supply the very
    # fields a test is trying to remove.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AIHC_CONFIG", raising=False)
    monkeypatch.setenv("AIHC_AK", "ak")
    monkeypatch.setenv("AIHC_SK", "sk")
    monkeypatch.setenv("AIHC_POOL", "cce-1")
    monkeypatch.setenv("AIHC_QUEUE", "default")


def run(argv, monkeypatch, *responses):
    """Run the CLI with a stubbed HTTP session; returns the exit code."""
    session = FakeSession(*responses)

    # Bind the real class, not cli.AihcClient -- a second run() in one test would
    # otherwise wrap the previous factory.
    def factory(config, session_=None):
        return RealAihcClient(config, session=session)

    monkeypatch.setattr(cli, "AihcClient", factory)
    code = cli.main(argv)
    return code, session


def test_no_command_prints_help(capsys):
    assert cli.main([]) == cli.EXIT_USAGE
    assert "submit" in capsys.readouterr().out


def test_submit_dry_run_makes_no_request(monkeypatch, capsys):
    code, session = run(
        [
            "--json",
            "submit",
            "--name",
            "demo",
            "--image",
            "registry/x:1",
            "--command",
            "sleep 1d",
            "--gpu",
            "a800:8",
            "--dry-run",
        ],
        monkeypatch,
    )
    assert code == cli.EXIT_OK
    assert session.calls == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "CreateJob"
    assert payload["resourcePoolId"] == "cce-1"
    assert payload["body"]["jobSpec"]["resources"] == [
        {"name": "baidu.com/a800_80g_cgpu", "quantity": 8}
    ]


def test_submit_flags_override_template_file(monkeypatch, tmp_path, capsys):
    template = tmp_path / "job.json"
    template.write_text(
        json.dumps(
            {
                "name": "from-file",
                "image": "registry/x:1",
                "command": "sleep 1d",
                "replicas": 1,
                "envs": {"KEEP": "1", "OVERRIDE": "old"},
            }
        ),
        encoding="utf-8",
    )
    code, _ = run(
        [
            "--json",
            "submit",
            "-f",
            str(template),
            "--name",
            "from-flag",
            "--replicas",
            "4",
            "--env",
            "OVERRIDE=new",
            "--dry-run",
        ],
        monkeypatch,
    )
    assert code == cli.EXIT_OK
    body = json.loads(capsys.readouterr().out)["body"]
    assert body["name"] == "from-flag"
    assert body["jobSpec"]["replicas"] == 4
    envs = {e["name"]: e["value"] for e in body["jobSpec"]["envs"]}
    assert envs == {"KEEP": "1", "OVERRIDE": "new"}  # --env merges, does not wipe


def test_submit_reports_job_id(monkeypatch, capsys):
    code, _ = run(
        ["--json", "submit", "--name", "demo", "--image", "r/x:1", "--command", "sleep 1d"],
        monkeypatch,
        FakeResponse(200, {"jobId": "job-abc", "jobName": "demo"}),
    )
    assert code == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["jobId"] == "job-abc"


def test_submit_without_anything_is_a_usage_error(monkeypatch, capsys):
    code, _ = run(["submit"], monkeypatch)
    assert code == cli.EXIT_USAGE
    assert "nothing to submit" in capsys.readouterr().err


def test_bad_template_key_is_a_usage_error(monkeypatch, capsys):
    code, _ = run(
        ["submit", "--name", "demo", "--image", "r/x:1", "--command", "c", "--gpu", "mi300x:8"],
        monkeypatch,
    )
    assert code == cli.EXIT_USAGE
    assert "unknown resource/GPU type" in capsys.readouterr().err


def test_missing_credentials_is_a_config_error(monkeypatch, capsys):
    for key in ("AIHC_AK", "AIHC_SK"):
        monkeypatch.delenv(key, raising=False)
    code, _ = run(["list"], monkeypatch)
    assert code == cli.EXIT_CONFIG
    assert "missing access key" in capsys.readouterr().err


def test_missing_pool_is_a_config_error(monkeypatch, capsys):
    monkeypatch.delenv("AIHC_POOL", raising=False)
    code, _ = run(["list"], monkeypatch)
    assert code == cli.EXIT_CONFIG
    assert "no resource pool" in capsys.readouterr().err


def test_api_error_exit_code(monkeypatch, capsys):
    code, _ = run(
        ["get", "job-missing"], monkeypatch, FakeResponse(404, {"code": "NotFound", "message": "gone"})
    )
    assert code == cli.EXIT_API
    assert "api error" in capsys.readouterr().err


def test_wait_on_failed_job_exits_three(monkeypatch, capsys):
    code, _ = run(
        ["wait", "job-1", "--interval", "0"], monkeypatch, FakeResponse(200, {"status": "Failed"})
    )
    assert code == cli.EXIT_JOB_FAILED
    assert "ended in state Failed" in capsys.readouterr().err


def test_list_renders_a_table(monkeypatch, capsys):
    code, _ = run(
        ["list"],
        monkeypatch,
        FakeResponse(
            200,
            {
                "totalCount": 1,
                "jobs": [
                    {
                        "jobId": "job-1",
                        "name": "demo",
                        "status": "Running",
                        "jobType": "pytorch",
                        "queue": "default",
                        "gpuCount": 32,
                        "nodeCount": 4,
                        "createdAt": "2026-08-19T00:00:00Z",
                    }
                ],
            },
        ),
    )
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "JOBID" in out and "job-1" in out and "Running" in out


def test_json_mode_returns_the_raw_envelope(monkeypatch, capsys):
    code, _ = run(
        ["--json", "list"], monkeypatch, FakeResponse(200, {"totalCount": 0, "jobs": []})
    )
    assert code == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == {"totalCount": 0, "jobs": []}


def test_logs_prints_lines(monkeypatch, capsys):
    code, _ = run(
        ["logs", "job-1", "--pod", "demo-master-0"],
        monkeypatch,
        FakeResponse(200, {"logs": ["hello", "world"], "nextMarker": ""}),
    )
    assert code == cli.EXIT_OK
    assert capsys.readouterr().out.splitlines() == ["hello", "world"]


def test_delete_requires_yes_when_non_interactive(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code, session = run(["delete", "job-1"], monkeypatch)
    assert code == cli.EXIT_CONFIG
    assert session.calls == []
    assert "--yes" in capsys.readouterr().err


def test_raw_passes_through_action_and_body(monkeypatch, capsys):
    code, session = run(
        ["raw", "DescribeJobMetrics", "--body", '{"jobId": "job-1", "metricType": "GpuUsage"}'],
        monkeypatch,
        FakeResponse(200, {"result": []}),
    )
    assert code == cli.EXIT_OK
    call = session.calls[0]
    assert "action=DescribeJobMetrics" in call["url"]
    assert "resourcePoolId=cce-1" in call["url"]  # implicit default pool
    assert call["body"] == {"jobId": "job-1", "metricType": "GpuUsage"}


def test_render_validates_offline(monkeypatch, tmp_path, capsys):
    template = tmp_path / "job.json"
    template.write_text(
        json.dumps({"name": "demo", "image": "r/x:1", "command": "sleep 1d"}), encoding="utf-8"
    )
    assert cli.main(["render", "-f", str(template)]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["jobType"] == "PyTorchJob"


def test_config_show_masks_secrets(capsys):
    assert cli.main(["--json", "config", "show"]) == cli.EXIT_OK
    info = json.loads(capsys.readouterr().out)
    assert info["secret_key"] != "sk"
    assert info["pool"] == "cce-1"


def test_config_init_writes_a_file(tmp_path, capsys):
    target = tmp_path / "config.json"
    code = cli.main(
        [
            "--json",
            "--access-key",
            "AK1",
            "--secret-key",
            "SK1",
            "--region",
            "cn-gz",
            "config",
            "init",
            "--path",
            str(target),
        ]
    )
    assert code == cli.EXIT_OK
    data = json.loads(target.read_text())
    assert data["credentials"]["accesskey"] == "AK1"
    assert data["region"] == "cn-gz"
    assert json.loads(capsys.readouterr().out)["written"] == str(target)


def test_log_prefix_is_stripped_in_text_mode(monkeypatch, capsys):
    """Container-runtime prefixes are noise when tailing training output."""
    raw = "2026-08-19T12:58:56.511554557+08:00 stdout F step 1 loss 0.5"
    code, _ = run(
        ["logs", "job-1", "--pod", "p-master-0"],
        monkeypatch,
        FakeResponse(200, {"logs": [raw], "nextMarker": ""}),
    )
    assert code == cli.EXIT_OK
    assert capsys.readouterr().out.strip() == "step 1 loss 0.5"


def test_log_raw_flag_and_json_keep_the_prefix(monkeypatch, capsys):
    raw = "2026-08-19T12:58:56.511554557+08:00 stderr P partial line"
    code, _ = run(
        ["logs", "job-1", "--pod", "p-master-0", "--raw"],
        monkeypatch,
        FakeResponse(200, {"logs": [raw], "nextMarker": ""}),
    )
    assert capsys.readouterr().out.strip() == raw

    code, _ = run(
        ["--json", "logs", "job-1", "--pod", "p-master-0"],
        monkeypatch,
        FakeResponse(200, {"logs": [raw], "nextMarker": ""}),
    )
    assert json.loads(capsys.readouterr().out)["logs"] == [raw]
    assert code == cli.EXIT_OK


def test_non_prefixed_lines_are_untouched(monkeypatch, capsys):
    code, _ = run(
        ["logs", "job-1", "--pod", "p-master-0"],
        monkeypatch,
        FakeResponse(200, {"logs": ["plain line", "2.10.0+cu128 1"], "nextMarker": ""}),
    )
    assert capsys.readouterr().out.splitlines() == ["plain line", "2.10.0+cu128 1"]


def test_auto_queue_is_on_by_default(monkeypatch, capsys):
    """`submit` moves the job to a queue that fits without being asked."""
    from tests.test_client_jobs import QUEUE_TREE

    tree = json.loads(json.dumps(QUEUE_TREE))
    tree["queues"][1]["children"][0]["opened"] = True
    tree["queues"][1]["children"][0]["capability"]["acceleratorCardList"][0][
        "acceleratorDescription"
    ] = "baidu.com/a800_80g_cgpu"
    monkeypatch.setenv("AIHC_QUEUE", "aihcq-child-a")  # only 16 free
    monkeypatch.setenv("AIHC_QUEUE_POOL", "aihc-pool")

    code, session = run(
        ["submit", "--name", "demo", "--image", "r/x:1", "--command", "c", "--gpu", "a800:20"],
        monkeypatch,
        FakeResponse(200, tree),
        FakeResponse(200, tree),
        FakeResponse(200, {"jobId": "job-1", "jobName": "demo"}),
    )
    assert code == cli.EXIT_OK
    assert "queueID=aihcq-child-b" in session.calls[-1]["url"]
    assert "auto-queue -> aihcq-child-b" in capsys.readouterr().err


def test_no_auto_queue_pins_the_configured_queue(monkeypatch, capsys):
    from tests.test_client_jobs import QUEUE_TREE

    monkeypatch.setenv("AIHC_QUEUE", "aihcq-child-a")
    monkeypatch.setenv("AIHC_QUEUE_POOL", "aihc-pool")
    code, session = run(
        [
            "submit", "--name", "demo", "--image", "r/x:1", "--command", "c",
            "--gpu", "a800:20", "--no-auto-queue",
        ],
        monkeypatch,
        FakeResponse(200, QUEUE_TREE),
        FakeResponse(200, QUEUE_TREE),
    )
    assert code == cli.EXIT_CONFIG
    assert not any("CreateJob" in c["url"] for c in session.calls)
    # Only child-a is open in the base tree, and it is too small -> no alternatives.
    err = capsys.readouterr().err
    assert "needs 20 x baidu.com/a800_80g_cgpu" in err
    assert "No open queue currently has room" in err


def test_dry_run_warns_that_auto_queue_may_move_the_job(monkeypatch, capsys):
    code, session = run(
        ["submit", "--name", "demo", "--image", "r/x:1", "--command", "c", "--dry-run"],
        monkeypatch,
    )
    assert code == cli.EXIT_OK
    assert session.calls == []  # dry-run stays offline even with auto-queue on
    assert "--auto-queue is on" in capsys.readouterr().out


# ---------------------------------------------------------------- monitoring


def test_watch_once_renders_status_and_per_pod_load(monkeypatch, capsys):
    from tests.test_client_jobs import _job_detail, _metric_page

    code, session = run(
        ["watch", "job-1", "--once", "--metric", "GpuUsage", "--metric", "GpuTemperature"],
        monkeypatch,
        FakeResponse(200, _job_detail("Running")),
        FakeResponse(200, _metric_page(60.0, 75.5)),
        FakeResponse(200, _metric_page(50.0, 52.0)),
    )
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "job-1  demo  Running" in out
    assert "queue aihcq-real" in out and "elapsed 0:34:00" in out
    assert "job avg gpu 51.2%" in out
    # pod row: prefix stripped, latest values, trend drawn from the GpuUsage series
    assert "master-0" in out and "75.5" in out and "52C" in out
    assert "GPU% TREND" in out
    assert any("action=DescribeJobMetrics" in call["url"] for call in session.calls)


def test_watch_json_emits_one_document_per_refresh(monkeypatch, capsys):
    """A stream cannot be a single document, so watch --json is JSON Lines."""
    from tests.test_client_jobs import _job_detail, _metric_page

    code, _ = run(
        ["--json", "watch", "job-1", "--metric", "GpuUsage", "--interval", "0"],
        monkeypatch,
        FakeResponse(200, _job_detail("Running")),
        FakeResponse(200, _metric_page(60.0)),
        FakeResponse(200, _job_detail("Succeeded", finishedAt="2026-08-19T05:41:14Z")),
    )
    assert code == cli.EXIT_OK
    documents = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert [d["status"] for d in documents] == ["Running", "Succeeded"]
    assert documents[0]["pods"][0]["metrics"]["GpuUsage"] == 60.0


def test_watch_exits_three_when_the_job_ends_badly(monkeypatch, capsys):
    from tests.test_client_jobs import _job_detail

    code, _ = run(
        ["watch", "job-1", "--interval", "0"],
        monkeypatch,
        FakeResponse(200, _job_detail("Failed", pods=[])),
    )
    assert code == cli.EXIT_JOB_FAILED
    assert "[job-1] Failed" in capsys.readouterr().err


def test_watch_once_on_a_failed_job_still_exits_zero(monkeypatch):
    """--once is an observation, not a wait, so it does not carry the job's verdict."""
    from tests.test_client_jobs import _job_detail

    code, _ = run(
        ["watch", "job-1", "--once"], monkeypatch, FakeResponse(200, _job_detail("Failed", pods=[]))
    )
    assert code == cli.EXIT_OK


def test_metrics_command_prints_the_latest_value(monkeypatch, capsys):
    from tests.test_client_jobs import _job_detail, _metric_page

    code, session = run(
        ["metrics", "job-1", "--metric", "GpuUsage", "--since", "2m", "--step", "30"],
        monkeypatch,
        FakeResponse(200, _job_detail("Running")),
        FakeResponse(200, _metric_page(60.0, 75.5)),
    )
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "master-0" in out and "75.5" in out
    assert session.calls[1]["body"]["timeStep"] == "30"
    window = int(session.calls[1]["body"]["endTime"]) - int(session.calls[1]["body"]["startTime"])
    assert window == 120  # --since 2m


def test_metrics_history_prints_every_sample(monkeypatch, capsys):
    from tests.test_client_jobs import _job_detail, _metric_page

    code, _ = run(
        ["metrics", "job-1", "--metric", "GpuUsage", "--history"],
        monkeypatch,
        FakeResponse(200, _job_detail("Running")),
        FakeResponse(200, _metric_page(60.0, 75.5)),
    )
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "60.0" in out and "75.5" in out
    assert "TIME" in out


def test_bad_duration_is_a_usage_error(monkeypatch, capsys):
    code, session = run(["metrics", "job-1", "--since", "soon"], monkeypatch)
    assert code == cli.EXIT_USAGE
    assert "expected a duration" in capsys.readouterr().err
    assert session.calls == []


def test_rdma_flag_adds_the_link_metrics(monkeypatch):
    args = cli.build_parser().parse_args(["watch", "job-1", "--rdma"])
    types = cli._metric_types(args)
    assert types[: len(cli.DEFAULT_METRICS)] == list(cli.DEFAULT_METRICS)
    assert types[-2:] == ["RDMASendDataRate", "RDMARecvDataRate"]


def test_subcommand_timeout_flags_do_not_clobber_the_http_timeout():
    """A subparser dest of `timeout` would silently overwrite the global one with 0."""
    parser = cli.build_parser()
    for command, dest in (("wait", "wait_timeout"), ("watch", "watch_timeout")):
        args = parser.parse_args([command, "job-1", "--timeout", "5"])
        assert getattr(args, dest) == 5.0
        assert args.timeout is None  # the connection timeout is untouched


def test_sparkline_scales_percentages_against_100_not_the_maximum():
    """Autoscaling would draw an idle job as a busy one."""
    assert cli._sparkline([1.0, 2.0, 3.0], ceiling=100.0) == "▁▁▁"
    assert cli._sparkline([0.0, 50.0, 100.0], ceiling=100.0) == "▁▅█"
    assert cli._sparkline([]) == ""
