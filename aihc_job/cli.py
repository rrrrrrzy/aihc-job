"""``aihc-job`` command line interface.

Designed to be driven by agents as much as by humans:

* every command accepts ``--json`` and then writes a single JSON document to
  stdout, with all logging on stderr
* exit codes are stable (see ``EXIT_*``) so callers can branch without parsing
* ``submit --dry-run`` prints the exact signed request body, so a job can be
  reviewed before any resource is spent
* ``raw`` reaches any OpenAPI action that has no dedicated subcommand yet
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from .client import AihcClient
from .config import (
    KNOWN_VARIABLES,
    REGIONS,
    SERVERLESS_POOL_ID,
    find_env_file,
    load_config,
    load_env_file,
    template_variables,
    write_config,
)
from .errors import AihcError, ApiError, ConfigError, JobFailed, TemplateError, WaitTimeout
from .jobs import (
    DEFAULT_METRICS,
    METRIC_TYPES,
    PERCENT_METRICS,
    RDMA_METRICS,
    SUCCESS_STATES,
    TERMINAL_STATES,
    TREND_METRIC,
    JobManager,
    summarize_job,
)
from .models import (
    build_create_job_body,
    expand_variables,
    load_template,
    merge_templates,
    summarize_body,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_JOB_FAILED = 3
EXIT_CONFIG = 4
EXIT_TIMEOUT = 5
EXIT_API = 6

log = logging.getLogger("aihc_job")


# --------------------------------------------------------------- output helpers


def _emit(data: Any, *, as_json: bool, text: str | None = None) -> None:
    if as_json:
        json.dump(data, sys.stdout, indent=2, ensure_ascii=False, default=str)
        sys.stdout.write("\n")
    elif text is not None:
        print(text)
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    if not rows:
        return "(no results)"
    widths = {c: len(c) for c in columns}
    cells = []
    for row in rows:
        cell = {c: ("" if row.get(c) is None else str(row.get(c))) for c in columns}
        for c in columns:
            widths[c] = max(widths[c], len(cell[c]))
        cells.append(cell)
    lines = ["  ".join(c.upper().ljust(widths[c]) for c in columns).rstrip()]
    lines += ["  ".join(cell[c].ljust(widths[c]) for c in columns).rstrip() for cell in cells]
    return "\n".join(lines)


# Log lines come back with the container runtime's prefix, e.g.
# "2026-08-19T12:58:56.511554557+08:00 stdout F <line>". Stripped in text output;
# `--json` and the library keep the API response verbatim.
_CRI_PREFIX = re.compile(r"^\S+T\S+ std(?:out|err) [FP] ")


def _clean_log_line(line: str, raw: bool = False) -> str:
    return line if raw else _CRI_PREFIX.sub("", line)


def _kv_pairs(values: Sequence[str] | None, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise TemplateError(f"--{what} expects KEY=value, got {item!r}")
        out[key.strip()] = value
    return out


def _mask_secret(name: str, value: str) -> str:
    """Never print an AK/SK, even when it came from .env rather than the config file."""
    if any(part in name.upper() for part in ("SECRET", "_SK", "ACCESS_KEY", "_AK", "PASSWORD")):
        return f"{value[:6]}{'*' * 8}" if len(value) > 8 else "*" * len(value)
    return value


_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _duration(text: str) -> float:
    """``'90'``, ``'15m'``, ``'2h'`` -> seconds."""
    value = str(text).strip().lower()
    unit = _DURATION_UNITS.get(value[-1:], 1.0)
    number = value[:-1] if value[-1:] in _DURATION_UNITS else value
    try:
        return float(number) * unit
    except ValueError:
        raise TemplateError(f"expected a duration like 30s/15m/2h, got {text!r}") from None


def _hms(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = int(seconds)
    return f"{total // 3600}:{total // 60 % 60:02d}:{total % 60:02d}"


def _clock(epoch: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch)) if epoch else ""


_SPARK = "▁▂▃▄▅▆▇█"


def _sparkline(values: Sequence[float], width: int = 12, ceiling: float | None = None) -> str:
    """Bar chart of the last ``width`` samples.

    Percentages pass ``ceiling=100`` -- autoscaling would draw an idle job as a busy one.
    """
    points = [float(v) for v in values if v is not None][-width:]
    if not points:
        return ""
    top = ceiling or max(points) or 1.0
    return "".join(
        _SPARK[min(len(_SPARK) - 1, max(0, int(value / top * len(_SPARK))))] for value in points
    )


def _si_bytes(value: float) -> str:
    for suffix in ("B", "K", "M", "G"):
        if abs(value) < 1024:
            return f"{value:.0f}{suffix}" if suffix == "B" else f"{value:.1f}{suffix}"
        value /= 1024
    return f"{value:.1f}T"


def _format_metric(name: str, value: float | None) -> str:
    if value is None:
        return "-"
    unit = METRIC_TYPES.get(name, ("", ""))[1]
    if unit == "%":
        return f"{value:.1f}"
    if unit == "B/s":
        return f"{_si_bytes(value)}/s"
    if unit == "B":
        return _si_bytes(value)
    if unit == "W":
        return f"{value:.0f}W"
    if unit == "C":
        return f"{value:.0f}C"
    return f"{value:g}"


def _metric_types(args: argparse.Namespace) -> list[str]:
    """Resolve ``--metric``/``--rdma`` into the metric types to sample.

    Names are already constrained by argparse ``choices``; ``jobs.metrics`` re-checks for
    callers coming in through the Python API.
    """
    types = list(dict.fromkeys(args.metric)) if args.metric else list(DEFAULT_METRICS)
    if getattr(args, "rdma", False):
        types += [name for name in RDMA_METRICS if name not in types]
    return types


def _metric_columns(types: Sequence[str]) -> list[str]:
    return [METRIC_TYPES[name][0] for name in types]


def _short_pod(pod: str, job_id: str) -> str:
    """``job-xxxx-master-0`` -> ``master-0``: the prefix is the same on every row."""
    prefix = f"{job_id}-"
    return pod[len(prefix) :] if pod.startswith(prefix) else pod


def _render_watch(snapshot: Mapping[str, Any], *, types: Sequence[str], trend: int = 12) -> str:
    job_id = str(snapshot.get("jobId") or "")
    status = snapshot.get("status", "")
    head = [f"{job_id}  {snapshot.get('name', '')}  {status}"]

    facts = [f"queue {snapshot.get('queue', '')}"]
    if snapshot.get("gpuCount"):
        facts.append(f"{snapshot['gpuCount']} gpu / {snapshot.get('nodeCount')} node")
    elapsed = _hms(snapshot.get("elapsedSeconds"))
    if elapsed:
        facts.append(f"elapsed {elapsed}")
    if snapshot.get("gpuUtil") is not None:
        facts.append(
            f"job avg gpu {float(snapshot['gpuUtil']):.1f}% "
            f"mem {float(snapshot.get('gpuMemUtil') or 0):.1f}%"
        )
    facts.append(f"at {_clock(snapshot.get('sampledAt'))}")
    head.append("   ".join(facts))
    if snapshot.get("reason"):
        head.append(f"reason: {snapshot['reason']}")

    metrics = snapshot.get("metrics") or {}
    series = metrics.get("series") or {}
    labels = _metric_columns(types)
    rows = []
    for pod in snapshot.get("pods") or []:
        latest = pod.get("metrics") or {}
        row = {
            "pod": _short_pod(str(pod.get("name") or ""), job_id),
            "status": pod.get("status", ""),
            "node": pod.get("nodeName", ""),
            "restarts": pod.get("restarts"),
        }
        for name, label in zip(types, labels):
            row[label] = _format_metric(name, latest.get(name))
        trend_samples = (series.get(TREND_METRIC) or {}).get(pod.get("name"), [])
        row[f"{METRIC_TYPES[TREND_METRIC][0]} trend"] = _sparkline(
            [s["value"] for s in trend_samples],
            width=trend,
            ceiling=100.0 if TREND_METRIC in PERCENT_METRICS else None,
        )
        rows.append(row)

    columns = ["pod", "status", "node", "restarts", *labels]
    if TREND_METRIC in types:
        columns.append(f"{METRIC_TYPES[TREND_METRIC][0]} trend")
    body = [_table(rows, columns)]
    if not metrics:
        body.append(f"# no load samples: the job is {status}, not running")
    elif metrics.get("step"):
        body.append(f"# per-pod averages over all its GPUs, {metrics['step']}s samples")
    for name, error in (metrics.get("errors") or {}).items():
        body.append(f"# {name} unavailable: {error}")
    return "\n".join(head + [""] + body)


def _render_metric_history(snapshot: Mapping[str, Any], types: Sequence[str], job_id: str) -> str:
    """One table per pod: a row per sample time, a column per metric.

    Every type was queried over the same window and step, so the time grids line up.
    """
    series = snapshot.get("series") or {}
    labels = _metric_columns(types)
    pods = sorted({pod for by_pod in series.values() for pod in by_pod})
    blocks = []
    for pod in pods:
        by_time: dict[float, dict[str, Any]] = {}
        for name, label in zip(types, labels):
            for sample in (series.get(name) or {}).get(pod) or []:
                by_time.setdefault(sample["time"], {})[label] = _format_metric(
                    name, sample["value"]
                )
        rows = [
            {"time": _clock(stamp), **{label: values.get(label, "-") for label in labels}}
            for stamp, values in sorted(by_time.items())
        ]
        blocks.append(f"{_short_pod(pod, job_id)}\n" + _table(rows, ["time", *labels]))
    return "\n\n".join(blocks) if blocks else "(no samples)"


# ------------------------------------------------------------------ arg parsing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aihc-job",
        description="Submit and manage Baidu AIHC (百舸) training jobs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes: 0 ok, 1 error, 2 usage, 3 job failed, 4 config, 5 timeout, 6 api.\n"
            "Credentials come from --access-key/--secret-key, AIHC_AK/AIHC_SK, or ~/.aihc/config*."
        ),
    )
    parser.add_argument("--version", action="version", version=f"aihc-job {__version__}")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="repeat for more logging")

    common = parser.add_argument_group("connection")
    common.add_argument("--access-key", help="BCE Access Key ID")
    common.add_argument("--secret-key", help="BCE Secret Access Key")
    common.add_argument("--region", choices=sorted(REGIONS), help="region short name (default cn-bj)")
    common.add_argument("--endpoint", help="override the OpenAPI endpoint host")
    common.add_argument("-p", "--pool", help=f"resource pool ID, or {SERVERLESS_POOL_ID!r}")
    common.add_argument("-q", "--queue", help="queue name (self-managed pool) or queue ID (managed)")
    common.add_argument(
        "--queue-pool",
        help="real pool ID used for queue/capacity lookups (job actions use --pool)",
    )
    common.add_argument("-C", "--config", dest="config_path", help="path to a config file")
    common.add_argument(
        "--env-file",
        help="path to the .env holding per-user settings (default: repo root, $AIHC_ENV_FILE)",
    )
    common.add_argument("--timeout", type=float, help="per-request HTTP timeout in seconds")

    sub = parser.add_subparsers(dest="subcommand", metavar="<command>")

    # ---- config
    p_config = sub.add_parser("config", help="inspect or write the tool's config file")
    config_sub = p_config.add_subparsers(dest="config_command", metavar="<action>")
    p_init = config_sub.add_parser("init", help="write credentials/defaults to a config file")
    p_init.add_argument("--path", help="config file to write (default ~/.aihc/config.json)")
    config_sub.add_parser("show", help="show the resolved config (secrets masked)")

    # ---- pools / queues
    p_pools = sub.add_parser("pools", help="list resource pools")
    p_pools.add_argument(
        "--type",
        default="common",
        choices=["common", "dedicatedV2"],
        help="common = self-managed, dedicatedV2 = fully managed",
    )
    p_pools.add_argument("--keyword", default="", help="filter by name substring")
    p_pools.add_argument("--page-size", type=int, default=50)

    p_queues = sub.add_parser("queues", help="list queues in a resource pool")
    p_queues.add_argument("--keyword", default="")
    p_queues.add_argument("--page-size", type=int, default=50)

    # ---- submit
    p_submit = sub.add_parser(
        "submit",
        help="create a training job from a template and/or flags",
        description=(
            "Templates are JSON or YAML; any flag below overrides the matching template key. "
            "Multiple -f files are merged left to right."
        ),
    )
    p_submit.add_argument("-f", "--file", action="append", default=[], help="job template (repeatable)")
    p_submit.add_argument("--name", help="job name (lowercase, '-' separated)")
    p_submit.add_argument("--image", help="container image, tag required")
    p_submit.add_argument("--command", help="launch command")
    p_submit.add_argument("--command-file", help="read the launch command from a file")
    p_submit.add_argument(
        "--framework",
        help="PyTorchJob (default) | TFJob | MPIJob | RayJob; aliases like 'pytorch' work",
    )
    p_submit.add_argument("--replicas", type=int, help="worker replicas (nodes)")
    p_submit.add_argument("--gpu", help="e.g. 'a800:8' or 'a800:8,cpu=64,memory=512'")
    p_submit.add_argument("--env", action="append", default=[], help="KEY=value (repeatable)")
    p_submit.add_argument("--label", action="append", default=[], help="key=value (repeatable)")
    p_submit.add_argument(
        "--datasource",
        action="append",
        default=[],
        help="type=pfs,name=pfs-xxx,mountPath=/mnt/cluster (repeatable)",
    )
    p_submit.add_argument("--priority", choices=["low", "normal", "high"])
    p_submit.add_argument("--rdma", dest="rdma", action="store_true", default=None, help="enable RDMA")
    p_submit.add_argument("--no-rdma", dest="rdma", action="store_false", help="disable RDMA")
    p_submit.add_argument("--bccl", action="store_true", default=None, help="enable BCCL acceleration")
    p_submit.add_argument("--host-network", action="store_true", default=None)
    p_submit.add_argument("--fault-tolerance", action="store_true", default=None)
    p_submit.add_argument("--fault-tolerance-args", help="e.g. '--enable-replace=true ...'")
    p_submit.add_argument("--retention-period", help="keep the finished job for e.g. 1d")
    p_submit.add_argument(
        "--auto-queue",
        dest="auto_queue",
        action="store_true",
        default=True,
        help="on by default: if the target queue lacks room, switch to the emptiest that fits",
    )
    p_submit.add_argument(
        "--no-auto-queue",
        dest="auto_queue",
        action="store_false",
        help="pin the job to the configured queue and fail if it does not fit",
    )
    p_submit.add_argument(
        "--no-check-capacity",
        dest="check_capacity",
        action="store_false",
        help="skip the pre-submit queue capacity check",
    )
    p_submit.add_argument("--dry-run", action="store_true", help="print the request without sending")
    p_submit.add_argument("--wait", action="store_true", help="block until the job is terminal")
    p_submit.add_argument("--wait-running", action="store_true", help="block until the job starts")
    p_submit.add_argument("--follow", action="store_true", help="stream rank-0 logs after start")
    p_submit.add_argument("--wait-timeout", type=float, default=0.0, help="0 = no timeout")
    p_submit.add_argument("--poll-interval", type=float, default=15.0)

    # ---- list / get
    p_list = sub.add_parser("list", aliases=["ls"], help="list jobs")
    p_list.add_argument("--status", default="", help="filter by status, e.g. Running")
    p_list.add_argument("--name", default="", help="filter by name substring")
    p_list.add_argument("--all-queues", action="store_true", help="ignore the default queue filter")
    p_list.add_argument("--page", type=int, default=1)
    p_list.add_argument("-n", "--limit", type=int, default=20, help="page size")

    p_get = sub.add_parser("get", help="show one job")
    p_get.add_argument("job_id")
    p_get.add_argument("--pods", action="store_true", help="include the pod list")

    p_pods = sub.add_parser("pods", help="list a job's pods")
    p_pods.add_argument("job_id")

    p_events = sub.add_parser("events", help="show job events")
    p_events.add_argument("job_id")

    p_nodes = sub.add_parser("nodes", help="show the nodes a job is running on")
    p_nodes.add_argument("job_id")

    # ---- logs / wait / control
    p_logs = sub.add_parser("logs", help="fetch pod logs")
    p_logs.add_argument("job_id")
    p_logs.add_argument("--pod", default="", help="pod name (default: rank 0)")
    p_logs.add_argument("--follow", action="store_true", help="stream until the job is terminal")
    p_logs.add_argument("--max-lines", type=int, help="cap the lines returned per page")
    p_logs.add_argument("--keywords", default="", help="only lines containing this text")
    p_logs.add_argument("--interval", type=float, default=5.0, help="poll interval when following")
    p_logs.add_argument(
        "--raw", action="store_true", help="keep the container-runtime timestamp prefix"
    )

    # ---- monitoring
    p_watch = sub.add_parser(
        "watch",
        help="live status + per-pod GPU load, redrawn in place until the job ends",
        description=(
            "Refreshes a status and load dashboard until the job reaches a terminal state "
            "(exit 3 if that state is not Succeeded). Load figures are per pod, averaged "
            "over that pod's GPUs -- the API exposes no per-device breakdown. Costs one "
            "DescribeJob plus one call per metric type per refresh."
        ),
    )
    p_watch.add_argument("job_id")
    p_watch.add_argument("--interval", type=float, default=10.0, help="seconds between refreshes")
    p_watch.add_argument("--step", type=float, default=30.0, help="metric sampling interval (s)")
    p_watch.add_argument("--window", type=float, default=300.0, help="history kept for the trend (s)")
    p_watch.add_argument("--trend", type=int, default=12, help="width of the trend sparkline")
    p_watch.add_argument(
        "--metric",
        action="append",
        default=[],
        choices=sorted(METRIC_TYPES),
        metavar="TYPE",
        help=f"metric to show, repeatable (default: {' '.join(DEFAULT_METRICS)})",
    )
    p_watch.add_argument("--rdma", action="store_true", help="add RDMA send/recv rate columns")
    p_watch.add_argument("--once", action="store_true", help="render one snapshot and exit 0")
    # dest must not be `timeout`: that is the global per-request HTTP timeout, and a
    # colliding dest silently overwrites it with 0.
    p_watch.add_argument(
        "--timeout",
        dest="watch_timeout",
        type=float,
        default=0.0,
        help="give up after this many seconds (0 = until the job ends)",
    )
    p_watch.add_argument(
        "--no-clear", action="store_true", help="append each refresh instead of redrawing"
    )

    p_metrics = sub.add_parser(
        "metrics",
        help="one-shot GPU/CPU load for a job's pods",
        description=(
            "Prints the latest value per pod, or every sample with --history. Values are "
            "per pod, averaged over its GPUs."
        ),
    )
    p_metrics.add_argument("job_id")
    p_metrics.add_argument(
        "--metric",
        action="append",
        default=[],
        choices=sorted(METRIC_TYPES),
        metavar="TYPE",
        help=f"metric to query, repeatable (default: {' '.join(DEFAULT_METRICS)})",
    )
    p_metrics.add_argument("--rdma", action="store_true", help="add RDMA send/recv rates")
    p_metrics.add_argument("--since", default="10m", help="window to query, e.g. 300s/10m/2h")
    p_metrics.add_argument("--step", type=float, default=30.0, help="sampling interval (s)")
    p_metrics.add_argument(
        "--history", action="store_true", help="print every sample instead of the latest"
    )

    p_wait = sub.add_parser("wait", help="block until a job reaches a state")
    p_wait.add_argument("job_id")
    p_wait.add_argument("--until", choices=["terminal", "running"], default="terminal")
    # Same reason as watch: `timeout` is taken by the global HTTP timeout.
    p_wait.add_argument(
        "--timeout", dest="wait_timeout", type=float, default=0.0, help="0 = no timeout"
    )
    p_wait.add_argument("--interval", type=float, default=15.0)

    p_stop = sub.add_parser("stop", help="stop a running job")
    p_stop.add_argument("job_id", nargs="+")

    p_delete = sub.add_parser("delete", aliases=["rm"], help="delete a job")
    p_delete.add_argument("job_id", nargs="+")
    p_delete.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    p_priority = sub.add_parser("priority", help="change a queued job's priority")
    p_priority.add_argument("job_id")
    p_priority.add_argument("value", choices=["low", "normal", "high"])

    # ---- escape hatches
    p_render = sub.add_parser("render", help="validate a template and print the request body")
    p_render.add_argument("-f", "--file", action="append", default=[], required=True)

    p_raw = sub.add_parser("raw", help="call any OpenAPI action directly")
    p_raw.add_argument("action", help="e.g. DescribeJobMetrics")
    p_raw.add_argument("--method", default="POST", choices=["GET", "POST"])
    p_raw.add_argument("--query", action="append", default=[], help="k=v (repeatable)")
    p_raw.add_argument("--body", help="inline JSON or @file.json")
    p_raw.add_argument("--no-pool", action="store_true", help="omit the implicit resourcePoolId")

    return parser


# --------------------------------------------------------------------- commands


def _manager(args: argparse.Namespace) -> JobManager:
    config = load_config(
        access_key=args.access_key,
        secret_key=args.secret_key,
        region=args.region,
        endpoint=args.endpoint,
        pool=args.pool,
        queue=args.queue,
        queue_pool=args.queue_pool,
        config_path=args.config_path,
        env_file=args.env_file,
        # `or None` keeps a 0 out of the HTTP layer (requests rejects it) whatever a
        # subcommand flag may have left here.
        timeout=args.timeout or None,
    )
    return JobManager(AihcClient(config), config)


def _template_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Merge template files, then apply explicitly-passed flags on top."""
    files = [load_template(path) for path in args.file]
    overrides: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is not None:
            overrides[key] = value

    put("name", args.name)
    put("image", args.image)
    put("framework", args.framework)
    put("replicas", args.replicas)
    put("gpu", args.gpu)
    put("priority", args.priority)
    put("enableRDMA", args.rdma)
    put("hostNetwork", args.host_network)
    put("enableBccl", args.bccl)
    put("faultTolerance", args.fault_tolerance)
    put("faultToleranceArgs", args.fault_tolerance_args)
    put("retentionPeriod", args.retention_period)
    put("pool", args.pool)
    put("queue", args.queue)

    if args.command_file:
        overrides["command"] = Path(args.command_file).expanduser().read_text(encoding="utf-8").strip()
    if args.command:
        overrides["command"] = args.command
    if args.env:
        merged_env = {}
        for template in files:
            existing = template.get("envs") or template.get("env")
            if isinstance(existing, Mapping):
                merged_env.update({str(k): str(v) for k, v in existing.items()})
        merged_env.update(_kv_pairs(args.env, "env"))
        overrides["envs"] = merged_env
    if args.label:
        overrides["labels"] = _kv_pairs(args.label, "label")
    if args.datasource:
        overrides["datasources"] = list(args.datasource)

    # Expand here rather than leaving it to build_create_job_body so that flags may carry
    # placeholders too ('--command "cd {{AIHC_WORKDIR}}"'). --env-file is honoured because
    # main() exports it, which also lets the error message name the file to edit.
    return expand_variables(merge_templates(*files, overrides))


def cmd_config(args: argparse.Namespace) -> int:
    if args.config_command == "init":
        path = write_config(
            args.path or args.config_path,
            access_key=args.access_key or "",
            secret_key=args.secret_key or "",
            region=args.region or "",
            pool=args.pool or "",
            queue=args.queue or "",
            queue_pool=args.queue_pool or "",
        )
        _emit({"written": str(path)}, as_json=args.json, text=f"wrote {path}")
        return EXIT_OK

    config = load_config(
        access_key=args.access_key,
        secret_key=args.secret_key,
        region=args.region,
        endpoint=args.endpoint,
        pool=args.pool,
        queue=args.queue,
        queue_pool=args.queue_pool,
        config_path=args.config_path,
        env_file=args.env_file,
        timeout=args.timeout,
    )
    info = config.redacted()
    env_path = find_env_file(args.env_file)
    info["env_file"] = str(env_path) if env_path else ""
    # What a template can substitute, so "did my .env get picked up?" is answerable.
    # Limited to the documented names plus whatever this .env defines: the platform
    # exports unrelated AIHC_* variables on these machines and they are only noise here.
    dotenv, _ = load_env_file(args.env_file)
    shown = set(KNOWN_VARIABLES) | set(dotenv)
    resolved = template_variables(args.env_file)
    info["variables"] = {
        name: _mask_secret(name, resolved[name]) for name in sorted(shown) if resolved.get(name)
    }
    lines = [f"{k:<12} {v}" for k, v in info.items() if k != "variables"]
    lines += ["variables"] + [f"  {k:<18} {v}" for k, v in info["variables"].items()]
    _emit(info, as_json=args.json, text="\n".join(lines))
    return EXIT_OK


def cmd_pools(args: argparse.Namespace, manager: JobManager) -> int:
    response = manager.client.describe_resource_pools(
        args.type, keyword=args.keyword, pageSize=args.page_size
    )
    pools = response.get("resourcePools") or []
    rows = [
        {
            "resourcePoolId": p.get("resourcePoolId", ""),
            "name": p.get("name", ""),
            "type": p.get("type", ""),
            "phase": p.get("phase", ""),
            "nodeNum": p.get("nodeNum", ""),
            "region": p.get("region", ""),
        }
        for p in pools
        if isinstance(p, Mapping)
    ]
    _emit(
        response if args.json else rows,
        as_json=args.json,
        text=_table(rows, ["resourcePoolId", "name", "type", "phase", "nodeNum", "region"]),
    )
    return EXIT_OK


def cmd_queues(args: argparse.Namespace, manager: JobManager) -> int:
    pool = manager.config.require_pool()
    try:
        queues = manager.queues(pool, keyword=args.keyword, pageSize=args.page_size)
    except ApiError as exc:
        if pool == SERVERLESS_POOL_ID and exc.status_code == 404:
            # DescribeQueues wants the real pool ID even where job actions want the
            # aihc-serverless sentinel.
            raise ConfigError(
                f"DescribeQueues does not accept {SERVERLESS_POOL_ID!r}; pass the real pool ID "
                "(e.g. `aihc-job -p aihc-xxxxxxxx queues`). Job commands keep using "
                f"{SERVERLESS_POOL_ID!r}."
            ) from exc
        raise
    leaves = {q["parent"] for q in queues if q["parent"]}
    rows = []
    for queue in queues:
        gpu = " ".join(
            f"{name.replace('baidu.com/', '')} {free:g}/{queue['capacity'][name]:g}"
            for name, free in sorted(queue["free"].items())
        )
        rows.append(
            {
                # Indent to show the tree: jobs are submitted to the nested Elastic queue.
                "queueId": "  " * queue["depth"] + queue["queueId"],
                "queueName": queue["queueName"],
                "type": queue["queueType"],
                "submit": "yes" if queue["opened"] and queue["queueId"] not in leaves else "",
                "gpuFree/total": gpu,
                "cpu": f"{queue['cpuCores']:g}",
                "memGi": f"{queue['memoryGi']:g}",
            }
        )
    _emit(
        {"queues": queues} if args.json else rows,
        as_json=args.json,
        text=_table(
            rows, ["queueId", "queueName", "type", "submit", "gpuFree/total", "cpu", "memGi"]
        ),
    )
    return EXIT_OK


def cmd_submit(args: argparse.Namespace, manager: JobManager) -> int:
    template = _template_from_args(args)
    if not template:
        raise TemplateError("nothing to submit: pass -f <template> and/or --name/--image/--command")

    if args.dry_run:
        request = manager.dry_run(template)
        if args.json:
            _emit(request, as_json=True)
        else:
            summary = summarize_body(request["body"])
            print(f"# CreateJob -> pool={request['resourcePoolId']} queue={request['queueID']}")
            if args.auto_queue:
                print(
                    "# --auto-queue is on: if that queue lacks room at submit time, an "
                    "emptier one is chosen instead (--no-auto-queue to pin it)."
                )
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            print("\n# request body")
            print(json.dumps(request["body"], indent=2, ensure_ascii=False))
        return EXIT_OK

    result = manager.submit(
        template, check_capacity=args.check_capacity, auto_queue=args.auto_queue
    )
    job_id = str(result.get("jobId") or "")
    report = manager.last_queue_report
    if not args.json:
        if report.get("checked"):
            card = str(report["descriptor"]).replace("baidu.com/", "")
            free = report.get("free")
            where = f"queue {result.get('queue')}"
            if report.get("chosen"):
                where = f"auto-queue -> {report['chosen']}"
            free_text = f", {free:g} free before this job" if free is not None else ""
            print(f"{where}: needs {report['needed']:g} x {card}{free_text}", file=sys.stderr)
        print(f"submitted {result.get('jobName') or template.get('name')} -> {job_id}")

    if not (args.wait or args.wait_running or args.follow):
        _emit(result, as_json=args.json)
        return EXIT_OK

    def report(status: str, _detail: Mapping[str, Any]) -> None:
        if not args.json:
            print(f"[{job_id}] {status}", file=sys.stderr)

    if args.follow:
        manager.wait(
            job_id,
            until="running",
            interval=args.poll_interval,
            timeout=args.wait_timeout,
            on_status=report,
            raise_on_failure=False,
        )
        for line in manager.iter_logs(job_id, follow=True, timeout=args.wait_timeout):
            # With --json this command still owes stdout exactly one document at the end,
            # so the streamed lines go to stderr instead of interleaving with it.
            print(_clean_log_line(line), file=sys.stderr if args.json else sys.stdout, flush=True)
    detail = manager.wait(
        job_id,
        until="running" if (args.wait_running and not args.wait) else "terminal",
        interval=args.poll_interval,
        timeout=args.wait_timeout,
        on_status=report,
    )
    _emit(
        detail if args.json else {"jobId": job_id, "status": detail.get("status")},
        as_json=args.json,
        text=f"[{job_id}] {detail.get('status')}",
    )
    return EXIT_OK


def cmd_list(args: argparse.Namespace, manager: JobManager) -> int:
    response = manager.list(
        queue="" if args.all_queues else None,
        status=args.status,
        keyword=args.name,
        page_number=args.page,
        page_size=args.limit,
    )
    jobs = [summarize_job(j) for j in response.get("jobs") or [] if isinstance(j, Mapping)]
    _emit(
        response if args.json else jobs,
        as_json=args.json,
        text=_table(
            jobs,
            [
                "jobId",
                "name",
                "status",
                "queue",
                "gpuCount",
                "nodeCount",
                # DescribeJobs carries these, so listing shows load with no extra call.
                "gpuUtil",
                "gpuMemUtil",
                "createdAt",
            ],
        ),
    )
    return EXIT_OK


def cmd_get(args: argparse.Namespace, manager: JobManager) -> int:
    detail = manager.get(args.job_id, detail=args.pods)
    if args.json:
        _emit(detail, as_json=True)
        return EXIT_OK
    summary = summarize_job(detail)
    lines = [f"{k:<12} {v}" for k, v in summary.items() if v not in (None, "")]
    spec = detail.get("jobSpec") or {}
    if isinstance(spec, Mapping) and spec.get("image"):
        lines.append(f"{'image':<12} {spec.get('image')}")
    lines.append(f"{'command':<12} {detail.get('command', '')}")
    if args.pods:
        pods = [
            {
                "name": p.get("name", ""),
                "replicaType": p.get("replicaType", ""),
                "status": p.get("status") or p.get("podPhase", ""),
                "nodeName": p.get("nodeName", ""),
                "restarts": p.get("restartCount", ""),
            }
            for p in detail.get("pods") or []
            if isinstance(p, Mapping)
        ]
        lines += ["", _table(pods, ["name", "replicaType", "status", "nodeName", "restarts"])]
    print("\n".join(lines))
    return EXIT_OK


def cmd_pods(args: argparse.Namespace, manager: JobManager) -> int:
    pods = manager.pods(args.job_id)
    rows = [
        {
            "name": p.get("name", ""),
            "replicaType": p.get("replicaType", ""),
            "status": p.get("status") or p.get("podPhase", ""),
            "nodeName": p.get("nodeName", ""),
            "podIP": p.get("PodIP") or p.get("podIP", ""),
        }
        for p in pods
    ]
    _emit(
        {"pods": pods} if args.json else rows,
        as_json=args.json,
        text=_table(rows, ["name", "replicaType", "status", "nodeName", "podIP"]),
    )
    return EXIT_OK


def cmd_events(args: argparse.Namespace, manager: JobManager) -> int:
    response = manager.events(args.job_id)
    rows = [
        {
            "lastTimestamp": e.get("lastTimestamp", ""),
            "reason": e.get("reason", ""),
            "count": e.get("count", ""),
            "message": str(e.get("message", ""))[:120],
        }
        for e in (response.get("events") or [])
        if isinstance(e, Mapping)
    ]
    _emit(
        response if args.json else rows,
        as_json=args.json,
        text=_table(rows, ["lastTimestamp", "reason", "count", "message"]),
    )
    return EXIT_OK


def cmd_nodes(args: argparse.Namespace, manager: JobManager) -> int:
    _emit(manager.nodes(args.job_id), as_json=True)
    return EXIT_OK


def cmd_logs(args: argparse.Namespace, manager: JobManager) -> int:
    if args.follow:
        for line in manager.iter_logs(
            args.job_id,
            pod=args.pod,
            keywords=args.keywords,
            max_lines=args.max_lines,
            follow=True,
            interval=args.interval,
        ):
            print(_clean_log_line(line, args.raw), flush=True)
        return EXIT_OK

    page = manager.logs(
        args.job_id,
        pod=args.pod,
        keywords=args.keywords,
        max_lines=args.max_lines,
    )
    if args.json:
        _emit(page, as_json=True)
    else:
        for line in page.get("logs") or []:
            print(_clean_log_line(line, args.raw))
    return EXIT_OK


def cmd_watch(args: argparse.Namespace, manager: JobManager) -> int:
    types = _metric_types(args)
    # Redrawing in place only makes sense on a terminal; piped or appended output keeps
    # every refresh so a log stays readable.
    redraw = sys.stdout.isatty() and not args.no_clear and not args.once and not args.json
    status = ""
    for snapshot in manager.watch(
        args.job_id,
        types=types,
        interval=args.interval,
        step=args.step,
        window=args.window,
        timeout=args.watch_timeout,
        once=args.once,
    ):
        status = str(snapshot.get("status") or "")
        if args.json:
            # A stream cannot be a single document: one JSON object per refresh (JSONL).
            json.dump(snapshot, sys.stdout, ensure_ascii=False, default=str)
            sys.stdout.write("\n")
            sys.stdout.flush()
            continue
        if redraw:
            sys.stdout.write("\033[H\033[2J")  # cursor home, clear screen
        print(_render_watch(snapshot, types=types, trend=args.trend), flush=True)

    if not args.once and status in TERMINAL_STATES and status not in SUCCESS_STATES:
        print(f"[{args.job_id}] {status}", file=sys.stderr)
        return EXIT_JOB_FAILED
    return EXIT_OK


def cmd_metrics(args: argparse.Namespace, manager: JobManager) -> int:
    types = _metric_types(args)
    snapshot = manager.metrics_snapshot(
        args.job_id, types=types, window=_duration(args.since), step=args.step
    )
    if args.json:
        _emit(snapshot, as_json=True)
        return EXIT_OK

    if args.history:
        print(_render_metric_history(snapshot, types, args.job_id))
        return EXIT_OK

    labels = _metric_columns(types)
    rows = []
    for pod, latest in sorted((snapshot.get("latest") or {}).items()):
        row: dict[str, Any] = {"pod": _short_pod(pod, args.job_id)}
        for name, label in zip(types, labels):
            row[label] = _format_metric(name, latest.get(name))
        rows.append(row)
    print(_table(rows, ["pod", *labels]))
    window = f"{_clock(snapshot.get('startTime'))}-{_clock(snapshot.get('endTime'))}"
    print(f"# latest of {window}, {snapshot.get('step')}s samples, per-pod GPU averages")
    for name, error in (snapshot.get("errors") or {}).items():
        print(f"# {name} unavailable: {error}", file=sys.stderr)
    return EXIT_OK


def cmd_wait(args: argparse.Namespace, manager: JobManager) -> int:
    detail = manager.wait(
        args.job_id,
        until=args.until,
        interval=args.interval,
        timeout=args.wait_timeout,
        on_status=lambda status, _d: print(f"[{args.job_id}] {status}", file=sys.stderr),
    )
    _emit(
        detail if args.json else {"jobId": args.job_id, "status": detail.get("status")},
        as_json=args.json,
        text=f"[{args.job_id}] {detail.get('status')}",
    )
    return EXIT_OK


def cmd_stop(args: argparse.Namespace, manager: JobManager) -> int:
    results = [manager.stop(job_id) for job_id in args.job_id]
    _emit(
        results,
        as_json=args.json,
        text="\n".join(f"stopped {r.get('jobId')}" for r in results),
    )
    return EXIT_OK


def cmd_delete(args: argparse.Namespace, manager: JobManager) -> int:
    if not args.yes and sys.stdin.isatty():
        answer = input(f"delete {len(args.job_id)} job(s): {', '.join(args.job_id)}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted", file=sys.stderr)
            return EXIT_ERROR
    elif not args.yes:
        raise ConfigError("refusing to delete non-interactively without --yes")
    results = [manager.delete(job_id) for job_id in args.job_id]
    _emit(
        results,
        as_json=args.json,
        text="\n".join(f"deleted {r.get('jobId')}" for r in results),
    )
    return EXIT_OK


def cmd_priority(args: argparse.Namespace, manager: JobManager) -> int:
    result = manager.set_priority(args.job_id, args.value)
    _emit(result, as_json=args.json, text=f"{args.job_id} priority -> {args.value}")
    return EXIT_OK


def cmd_render(args: argparse.Namespace) -> int:
    template = merge_templates(*[load_template(path) for path in args.file])
    body = build_create_job_body(template)
    if args.json:
        _emit({"body": body, "summary": summarize_body(body)}, as_json=True)
    else:
        print(json.dumps(body, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_raw(args: argparse.Namespace, manager: JobManager) -> int:
    query = _kv_pairs(args.query, "query")
    if not args.no_pool and "resourcePoolId" not in query and manager.config.pool:
        query["resourcePoolId"] = manager.config.pool
    body: dict[str, Any] | None = None
    if args.body:
        text = args.body
        if text.startswith("@"):
            text = Path(text[1:]).expanduser().read_text(encoding="utf-8")
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise TemplateError("--body must be a JSON object")
        body = parsed
    response = manager.client.call(args.action, method=args.method, query=query, body=body)
    _emit(response, as_json=True)
    return EXIT_OK


# ------------------------------------------------------------------------ main

_NEEDS_CLIENT = {
    "pools": cmd_pools,
    "queues": cmd_queues,
    "submit": cmd_submit,
    "list": cmd_list,
    "ls": cmd_list,
    "get": cmd_get,
    "pods": cmd_pods,
    "events": cmd_events,
    "nodes": cmd_nodes,
    "logs": cmd_logs,
    "watch": cmd_watch,
    "metrics": cmd_metrics,
    "wait": cmd_wait,
    "stop": cmd_stop,
    "delete": cmd_delete,
    "rm": cmd_delete,
    "priority": cmd_priority,
    "raw": cmd_raw,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.env_file:
        # One source of truth for the rest of the process, so template expansion and its
        # error messages point at the same file config resolution used.
        os.environ["AIHC_ENV_FILE"] = args.env_file

    level = logging.WARNING - 10 * min(args.verbose, 2)
    logging.basicConfig(
        level=level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )

    if not args.subcommand:
        parser.print_help()
        return EXIT_USAGE

    try:
        if args.subcommand == "config":
            if not args.config_command:
                args.config_command = "show"
            return cmd_config(args)
        if args.subcommand == "render":
            return cmd_render(args)
        handler = _NEEDS_CLIENT[args.subcommand]
        return handler(args, _manager(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except TemplateError as exc:
        print(f"template error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except JobFailed as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_JOB_FAILED
    except WaitTimeout as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_TIMEOUT
    except ApiError as exc:
        print(f"api error: {exc}", file=sys.stderr)
        return EXIT_API
    except AihcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:  # `| head`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
