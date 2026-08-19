"""High-level job operations: submit, poll, tail logs, stop, delete.

This is the layer an agent (or another Python program) should import; it owns the
pool/queue defaulting, the status polling loop, and log pagination, so callers
never deal with markers or terminal-state bookkeeping.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable, Iterator, Mapping, Sequence

from .client import AihcClient
from .config import Config
from .errors import ApiError, ConfigError, JobFailed, TemplateError, WaitTimeout
from .models import build_create_job_body, gpu_requirement

log = logging.getLogger("aihc_job")

# From the JobItem doc: statuses a job can never leave.
TERMINAL_STATES = {"Succeeded", "Failed", "Stopped", "ManualTermination"}
SUCCESS_STATES = {"Succeeded"}
# Present before any container runs; `wait --until running` treats these as "not yet".
# "Scheduled" is undocumented but real: the API reports it between Created and Running
# (pods placed on a node, still pulling the image).
PENDING_STATES = {"Created", "Scheduled", "Starting", "Restarting"}
# Statuses where pods exist, so asking for load figures is worth the API calls.
ACTIVE_STATES = {"Running", "Abnormal", "Restarting"}

# DescribeJobMetrics metric types -> (short column label, unit). A closed set on
# purpose: an unrecognised type comes back as an empty series, which reads as "the job
# is idle" rather than as the typo it is.
METRIC_TYPES: dict[str, tuple[str, str]] = {
    "GpuUsage": ("gpu%", "%"),
    "GpuMemoryUsage": ("gpumem%", "%"),
    "GpuPowerUsage": ("power", "W"),
    "GpuTemperature": ("temp", "C"),
    "GpuPipeTensorUsage": ("tensor%", "%"),
    "CpuUsage": ("cpu%", "%"),
    "MemoryUsage": ("mem%", "%"),
    "CpuTime": ("cputime", "s"),
    "MemoryAllocation": ("membytes", "B"),
    "DiskReadRate": ("diskrd", "B/s"),
    "DiskWriteRate": ("diskwr", "B/s"),
    "RDMASendDataRate": ("rdmatx", "B/s"),
    "RDMARecvDataRate": ("rdmarx", "B/s"),
    "RDMASendPacketsRate": ("rdmatxpkt", "/s"),
    "RDMARecvPacketsRate": ("rdmarxpkt", "/s"),
    "RDMASendErrorRate": ("rdmatxerr", "/s"),
    "RDMARecvErrorRate": ("rdmarxerr", "/s"),
    "RDMAHealth": ("rdma", ""),
    "PCIESendDataRate": ("pcietx", "B/s"),
    "PCIERecvDataRate": ("pcierx", "B/s"),
    "NVLinkSendDataRate": ("nvltx", "B/s"),
    "NVLinkRecvDataRate": ("nvlrx", "B/s"),
}
PERCENT_METRICS = {name for name, (_, unit) in METRIC_TYPES.items() if unit == "%"}

# What `watch` samples unless told otherwise: one API call each, so keep it short.
DEFAULT_METRICS: tuple[str, ...] = (
    "GpuUsage",
    "GpuMemoryUsage",
    "GpuPowerUsage",
    "GpuTemperature",
    "CpuUsage",
    "MemoryUsage",
)
RDMA_METRICS: tuple[str, ...] = ("RDMASendDataRate", "RDMARecvDataRate")
# Metric the trend line is drawn from.
TREND_METRIC = "GpuUsage"


class JobManager:
    """Job operations bound to one client plus a default pool/queue."""

    def __init__(self, client: AihcClient, config: Config | None = None) -> None:
        self.client = client
        self.config = config or client.config
        # Populated by submit() when a capacity check ran; advisory only.
        self.last_queue_report: dict[str, Any] = {}
        # jobId -> the queue the job really lives in (see job_queue).
        self._queue_of: dict[str, str] = {}

    # ------------------------------------------------------------ helpers

    def _pool(self, pool: str | None = None) -> str:
        return pool or self.config.require_pool()

    def _queue(self, queue: str | None = None) -> str:
        return queue or self.config.require_queue()

    # ------------------------------------------------------------- submit

    def submit(
        self,
        template: Mapping[str, Any],
        *,
        pool: str | None = None,
        queue: str | None = None,
        check_capacity: bool = True,
        auto_queue: bool = True,
    ) -> dict[str, Any]:
        """Build the request from ``template`` and create the job.

        With ``check_capacity`` the target queue is verified to have room for the
        requested accelerators first (requires ``config.queue_pool``). ``auto_queue``
        (on by default) keeps that queue when it fits and otherwise moves the job to the
        emptiest queue that does; pass ``auto_queue=False`` to fail instead of moving.
        """
        body = build_create_job_body(template)
        target_pool = self._pool(pool or template.get("pool") or template.get("resourcePoolId"))
        target_queue = self._queue(queue or template.get("queue"))
        if check_capacity or auto_queue:
            target_queue, report = self.resolve_queue(
                body,
                queue=target_queue,
                queue_pool=self.config.queue_pool,
                auto=auto_queue,
            )
            self.last_queue_report = report
        # The API takes the queue in the query string; some pool types also read
        # it from the body, and sending both matches the console's behaviour.
        body.setdefault("queue", target_queue)
        result = self.client.create_job(target_pool, target_queue, body)
        result.setdefault("pool", target_pool)
        result.setdefault("queue", target_queue)
        return result

    def dry_run(
        self,
        template: Mapping[str, Any],
        *,
        pool: str | None = None,
        queue: str | None = None,
    ) -> dict[str, Any]:
        """Return exactly what ``submit`` would send, without sending it."""
        body = build_create_job_body(template)
        target_pool = pool or template.get("pool") or template.get("resourcePoolId") or self.config.pool
        target_queue = queue or template.get("queue") or self.config.queue
        body.setdefault("queue", target_queue or "")
        return {
            "action": "CreateJob",
            "resourcePoolId": target_pool or "<missing: --pool>",
            "queueID": target_queue or "<missing: --queue>",
            "body": body,
        }

    # -------------------------------------------------------------- reads

    def list(
        self,
        *,
        pool: str | None = None,
        queue: str | None = None,
        status: str = "",
        keyword: str = "",
        keyword_type: str = "name",
        order_by: str = "createdAt",
        order: str = "desc",
        page_number: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        return self.client.describe_jobs(
            self._pool(pool),
            queue=queue if queue is not None else self.config.queue,
            status=status,
            keyword=keyword,
            keywordType=keyword_type if keyword else "",
            orderBy=order_by,
            order=order,
            pageNumber=page_number,
            pageSize=page_size,
        )

    def get(
        self,
        job_id: str,
        *,
        pool: str | None = None,
        queue: str | None = None,
        detail: bool = False,
    ) -> dict[str, Any]:
        return self.client.describe_job(self._pool(pool), self._queue(queue), job_id, detail)

    def status(self, job_id: str, *, pool: str | None = None, queue: str | None = None) -> str:
        return str(self.get(job_id, pool=pool, queue=queue).get("status") or "Unknown")

    def job_queue(self, job_id: str, *, pool: str | None = None, queue: str | None = None) -> str:
        """The queue a job actually landed in, looked up once and cached.

        ``DescribeJob`` answers even when handed the wrong queue and reports the real one,
        but ``DescribeJobMetrics`` returns ``403 AccessDenied`` for anything but the job's
        own queue -- and with auto-queue on, the configured queue is not always where the
        job went.
        """
        if queue:
            return queue
        cached = self._queue_of.get(job_id)
        if cached:
            return cached
        detail = self.get(job_id, pool=pool)
        resolved = str(detail.get("queue") or detail.get("queueId") or self._queue())
        self._queue_of[job_id] = resolved
        return resolved

    def queues(self, pool: str, **query: object) -> list[dict[str, Any]]:
        """Flatten the queue tree, annotating each entry with free capacity.

        ``DescribeQueues`` returns physical queues at the top level and nests the
        Elastic queues that jobs are actually submitted to under ``children``, so a
        caller that only reads the top level misses every submittable queue.
        """
        response = self.client.describe_queues(pool, **query)
        rows: list[dict[str, Any]] = []

        def walk(queues: Any, depth: int, parent: str) -> None:
            for queue in queues or []:
                if not isinstance(queue, Mapping):
                    continue
                capacity = _accelerators(queue.get("capability"))
                allocated = _accelerators(queue.get("allocated"))
                rows.append(
                    {
                        "queueId": queue.get("queueId") or queue.get("resourceQueueId", ""),
                        "queueName": queue.get("queueName") or queue.get("resourceQueueName", ""),
                        "queueType": queue.get("queueType") or queue.get("resourceQueueType", ""),
                        "depth": depth,
                        "parent": parent,
                        "opened": bool(queue.get("opened", True)),
                        "capacity": capacity,
                        "allocated": allocated,
                        "free": {k: capacity[k] - allocated.get(k, 0.0) for k in capacity},
                        "cpuCores": _number((queue.get("capability") or {}).get("cpuCores")),
                        "memoryGi": _number((queue.get("capability") or {}).get("memoryGi")),
                    }
                )
                walk(queue.get("children"), depth + 1, str(queue.get("queueId") or ""))

        walk(response.get("queues"), 0, "")
        return rows

    def find_queues_for(
        self, pool: str, gpu_descriptor: str, count: float, **query: object
    ) -> list[dict[str, Any]]:
        """Submittable queues that can hold ``count`` cards of ``gpu_descriptor``.

        "Submittable" means open, a leaf of the tree (a physical queue's Elastic child
        is where jobs go), and offering that accelerator type. Sorted by free capacity,
        most free first.
        """
        rows = self.queues(pool, **query)
        parents = {row["parent"] for row in rows if row["parent"]}
        candidates = [
            row
            for row in rows
            if row["opened"]
            and row["queueId"] not in parents  # leaf queue
            and row["free"].get(gpu_descriptor, 0.0) >= count
        ]
        return sorted(candidates, key=lambda r: -r["free"].get(gpu_descriptor, 0.0))

    def resolve_queue(
        self,
        body: Mapping[str, Any],
        *,
        queue: str,
        queue_pool: str = "",
        auto: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """Check that ``queue`` can hold the job, optionally picking a better one.

        ``queue_pool`` is the *real* resource pool ID -- ``DescribeQueues`` rejects the
        ``aihc-serverless`` sentinel that job actions require, so capacity checking is
        only possible when it is configured. Returns ``(queue_id, report)``; the report
        is advisory and always safe to ignore.

        Raises :class:`ConfigError` when the request does not fit and ``auto`` is off,
        listing the queues that would fit.
        """
        descriptor, needed = gpu_requirement(body)
        report: dict[str, Any] = {
            "checked": False,
            "descriptor": descriptor,
            "needed": needed,
            "queue": queue,
            "fits": None,
            "alternatives": [],
        }
        if not descriptor or needed <= 0 or not queue_pool:
            # No accelerator requested, or no pool to inspect: nothing to verify.
            report["reason"] = "no accelerator requested" if not descriptor else "no queuepool configured"
            return queue, report

        try:
            candidates = self.find_queues_for(queue_pool, descriptor, needed)
            everything = self.queues(queue_pool)
        except ApiError as exc:
            # Capacity checking is a convenience; never block a submit on it.
            log.warning("could not check queue capacity (%s); submitting to %s as-is", exc, queue)
            report["reason"] = f"capacity check failed: {exc}"
            return queue, report

        report["checked"] = True
        current = next((row for row in everything if row["queueId"] == queue), None)
        report["queueFound"] = current is not None
        report["free"] = (current or {}).get("free", {}).get(descriptor)
        report["fits"] = any(row["queueId"] == queue for row in candidates)
        report["alternatives"] = [
            {
                "queueId": row["queueId"],
                "queueName": row["queueName"],
                "free": row["free"].get(descriptor, 0.0),
                "capacity": row["capacity"].get(descriptor, 0.0),
            }
            for row in candidates
            if row["queueId"] != queue
        ]

        if report["fits"]:
            return queue, report
        if auto and candidates:
            chosen = candidates[0]
            report["chosen"] = chosen["queueId"]
            log.info(
                "auto-queue: %s -> %s (%g free)",
                queue,
                chosen["queueId"],
                chosen["free"].get(descriptor, 0.0),
            )
            return chosen["queueId"], report

        have = report["free"]
        if have is not None:
            detail = f"queue {queue} has {have:g} free"
        elif report["queueFound"]:
            # The queue exists but is a different chip: queues here are split by hardware,
            # so an A800 default queue offers no B200 at all.
            detail = f"queue {queue} has no {descriptor} at all"
        else:
            detail = f"queue {queue} was not found"
        if report["alternatives"]:
            options = "\n".join(
                f"    {alt['queueId']}  {alt['queueName']}  {alt['free']:g}/{alt['capacity']:g} free"
                for alt in report["alternatives"]
            )
            advice = (
                f"  Queues that fit:\n{options}\n"
                "  Pass -q <queueId>, or --auto-queue to pick automatically."
            )
        else:
            advice = (
                "  No open queue currently has room for that request.\n"
                "  Use --no-check-capacity to submit anyway and let it wait in the queue."
            )
        raise ConfigError(f"job needs {needed:g} x {descriptor} but {detail}.\n{advice}")

    def events(
        self, job_id: str, *, pool: str | None = None, queue: str | None = None
    ) -> dict[str, Any]:
        return self.client.describe_job_events(self._pool(pool), self._queue(queue), jobId=job_id)

    def nodes(
        self, job_id: str, *, pool: str | None = None, queue: str | None = None
    ) -> dict[str, Any]:
        return self.client.describe_job_nodes(self._pool(pool), self._queue(queue), job_id)

    def pods(
        self, job_id: str, *, pool: str | None = None, queue: str | None = None
    ) -> list[dict[str, Any]]:
        detail = self.get(job_id, pool=pool, queue=queue, detail=True)
        pods = detail.get("pods") or []
        return [p for p in pods if isinstance(p, Mapping)]

    def default_pod(
        self, job_id: str, *, pool: str | None = None, queue: str | None = None
    ) -> str:
        """Pick the rank-0 pod, which is where a training job's logs live."""
        pods = self.pods(job_id, pool=pool, queue=queue)
        if not pods:
            raise ApiError(
                0,
                code="NoPods",
                message=f"job {job_id} has no pods yet; wait for it to start or pass --pod",
                action="DescribeJob",
            )
        for preferred in ("master-0", "chief-0", "head", "worker-0"):
            for pod in pods:
                if str(pod.get("name", "")).endswith(preferred):
                    return str(pod["name"])
        return str(pods[0].get("name") or "")

    # ------------------------------------------------------------ control

    def stop(self, job_id: str, *, pool: str | None = None, queue: str | None = None) -> dict[str, Any]:
        return self.client.stop_job(self._pool(pool), self._queue(queue), job_id)

    def delete(self, job_id: str, *, pool: str | None = None, queue: str | None = None) -> dict[str, Any]:
        return self.client.delete_job(self._pool(pool), self._queue(queue), job_id)

    def set_priority(
        self, job_id: str, priority: str, *, pool: str | None = None, queue: str | None = None
    ) -> dict[str, Any]:
        return self.client.modify_job(self._pool(pool), self._queue(queue), job_id, priority)

    # --------------------------------------------------------------- wait

    def wait(
        self,
        job_id: str,
        *,
        pool: str | None = None,
        queue: str | None = None,
        until: str = "terminal",
        interval: float = 15.0,
        timeout: float = 0.0,
        on_status: Callable[[str, dict[str, Any]], None] | None = None,
        raise_on_failure: bool = True,
    ) -> dict[str, Any]:
        """Poll until the job reaches ``until`` (``terminal`` or ``running``).

        ``timeout`` of 0 means wait indefinitely. Transient API errors are
        tolerated (they are common while a job is being scheduled) but a run of
        five consecutive failures is re-raised.
        """
        started = time.monotonic()
        last_status = ""
        consecutive_errors = 0
        while True:
            try:
                detail = self.get(job_id, pool=pool, queue=queue)
                consecutive_errors = 0
            except ApiError as exc:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    raise
                log.warning("polling %s failed (%s), retrying", job_id, exc)
                time.sleep(interval)
                continue

            status = str(detail.get("status") or "Unknown")
            if status != last_status:
                last_status = status
                if on_status:
                    on_status(status, detail)

            if status in TERMINAL_STATES:
                if raise_on_failure and status not in SUCCESS_STATES:
                    raise JobFailed(job_id, status, _failure_reason(detail))
                return detail
            if until == "running" and status not in PENDING_STATES:
                return detail

            if timeout and time.monotonic() - started > timeout:
                raise WaitTimeout(job_id, status, timeout)
            time.sleep(interval)

    # --------------------------------------------------------------- logs

    def logs(
        self,
        job_id: str,
        *,
        pod: str = "",
        pool: str | None = None,
        queue: str | None = None,
        keywords: str = "",
        start_time: int | None = None,
        end_time: int | None = None,
        max_lines: int | None = None,
        marker: str = "",
    ) -> dict[str, Any]:
        """One page of logs for a pod (``nextMarker`` drives further pages)."""
        target_pool, target_queue = self._pool(pool), self._queue(queue)
        pod_name = pod or self.default_pod(job_id, pool=target_pool, queue=target_queue)
        return self.client.describe_job_logs(
            target_pool,
            target_queue,
            jobId=job_id,
            podName=pod_name,
            keywords=keywords,
            startTime=start_time,
            endTime=end_time,
            maxLines=max_lines,
            marker=marker,
        )

    def iter_logs(
        self,
        job_id: str,
        *,
        pod: str = "",
        pool: str | None = None,
        queue: str | None = None,
        keywords: str = "",
        max_lines: int | None = None,
        follow: bool = False,
        interval: float = 5.0,
        timeout: float = 0.0,
    ) -> Iterator[str]:
        """Yield log lines, optionally following until the job is terminal.

        Paging uses ``nextMarker``; when following, an empty marker just means
        "no new lines yet", so we keep the last non-empty marker and re-poll.
        """
        target_pool, target_queue = self._pool(pool), self._queue(queue)
        pod_name = pod or self.default_pod(job_id, pool=target_pool, queue=target_queue)
        marker = ""
        started = time.monotonic()
        while True:
            page = self.client.describe_job_logs(
                target_pool,
                target_queue,
                jobId=job_id,
                podName=pod_name,
                keywords=keywords,
                maxLines=max_lines,
                marker=marker,
            )
            lines = page.get("logs") or []
            for line in lines:
                yield str(line)
            next_marker = str(page.get("nextMarker") or "")
            if next_marker and next_marker != marker:
                marker = next_marker
                continue  # more pages available right now
            if not follow:
                return
            status = self.status(job_id, pool=target_pool, queue=target_queue)
            if status in TERMINAL_STATES:
                return
            if timeout and time.monotonic() - started > timeout:
                raise WaitTimeout(job_id, status, timeout)
            time.sleep(interval)

    # ---------------------------------------------------------- monitoring

    def metrics(
        self,
        job_id: str,
        metric_type: str,
        *,
        pool: str | None = None,
        queue: str | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        step: float | None = None,
    ) -> dict[str, list[dict[str, float]]]:
        """One metric as ``{podName: [{"time": epoch, "value": float}, ...]}``.

        Values are per *pod*, already averaged over that pod's devices -- the API exposes
        no per-GPU breakdown. ``step`` is the sampling interval in seconds; leaving it
        unset means the API's 5-minute default, which is far too coarse to watch.
        """
        if metric_type not in METRIC_TYPES:
            raise TemplateError(
                f"unknown metric type {metric_type!r}; expected one of "
                + ", ".join(sorted(METRIC_TYPES))
            )
        target_pool = self._pool(pool)
        response = self.client.describe_job_metrics(
            target_pool,
            self.job_queue(job_id, pool=target_pool, queue=queue),
            jobId=job_id,
            metricType=metric_type,
            # Strings, not numbers: the server unmarshals these into Go string fields and
            # answers 400 MalformedJSON for an integer.
            startTime=str(int(start_time)) if start_time else None,
            endTime=str(int(end_time)) if end_time else None,
            timeStep=str(int(step)) if step else None,
        )
        series: dict[str, list[dict[str, float]]] = {}
        for entry in response.get("metrics") or []:
            if not isinstance(entry, Mapping):
                continue
            pod = str(entry.get("podName") or "")
            series[pod] = [
                {"time": _number(sample.get("time")), "value": _number(sample.get("value"))}
                for sample in entry.get("metrics") or []
                if isinstance(sample, Mapping)
            ]
        return series

    def metrics_snapshot(
        self,
        job_id: str,
        *,
        types: Sequence[str] = DEFAULT_METRICS,
        pool: str | None = None,
        queue: str | None = None,
        window: float = 300.0,
        step: float = 30.0,
        end_time: float | None = None,
    ) -> dict[str, Any]:
        """Latest value per pod for each metric type, plus the series behind it.

        Costs one API call per metric type. A type that fails is recorded under
        ``errors`` and the rest are still returned: monitoring must never be the thing
        that breaks, and RDMA metrics in particular are missing on non-RDMA nodes.
        """
        end = int(end_time if end_time is not None else time.time())
        start = int(end - max(window, step))
        snapshot: dict[str, Any] = {
            "jobId": job_id,
            "startTime": start,
            "endTime": end,
            "step": int(step),
            "types": list(types),
            "series": {},
            "latest": {},
            "errors": {},
        }
        for metric_type in types:
            try:
                series = self.metrics(
                    job_id,
                    metric_type,
                    pool=pool,
                    queue=queue,
                    start_time=start,
                    end_time=end,
                    step=step,
                )
            except ApiError as exc:
                log.warning("metric %s unavailable (%s)", metric_type, exc)
                snapshot["errors"][metric_type] = str(exc)
                continue
            snapshot["series"][metric_type] = series
            for pod, samples in series.items():
                latest = snapshot["latest"].setdefault(pod, {})
                latest[metric_type] = samples[-1]["value"] if samples else None
        return snapshot

    def watch(
        self,
        job_id: str,
        *,
        pool: str | None = None,
        queue: str | None = None,
        types: Sequence[str] = DEFAULT_METRICS,
        interval: float = 10.0,
        step: float = 30.0,
        window: float = 300.0,
        timeout: float = 0.0,
        once: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Yield a status-plus-load snapshot every ``interval`` seconds.

        Stops after the first snapshot when ``once``, otherwise once the job is terminal
        (so the last snapshot always carries the final status). Metrics are only sampled
        while the job is active; a queued job costs one ``DescribeJob`` per round.
        """
        target_pool = self._pool(pool)
        started = time.monotonic()
        while True:
            detail = self.get(job_id, pool=target_pool, queue=queue, detail=True)
            status = str(detail.get("status") or "Unknown")
            target_queue = str(detail.get("queue") or "") or self.job_queue(
                job_id, pool=target_pool, queue=queue
            )
            self._queue_of.setdefault(job_id, target_queue)
            snapshot: dict[str, Any] = {
                "jobId": job_id,
                "name": str(detail.get("name") or ""),
                "status": status,
                "queue": target_queue,
                "priority": detail.get("priority", ""),
                "createdAt": detail.get("createdAt", ""),
                "finishedAt": detail.get("finishedAt", ""),
                "startedAt": job_started_at(detail),
                "elapsedSeconds": job_elapsed(detail),
                "gpuCount": detail.get("gpuCount"),
                "nodeCount": detail.get("nodeCount"),
                # Job-wide averages DescribeJob hands over for free; the per-pod numbers
                # below come from DescribeJobMetrics.
                "gpuUtil": detail.get("gpuUtilizationPercent"),
                "gpuMemUtil": detail.get("gpuMemoryUtilizationPercent"),
                "sampledAt": int(time.time()),
                "pods": _watch_pods(detail),
                "metrics": {},
                # Only for a bad ending: on success the timeline message is just "任务成功".
                "reason": (
                    _failure_reason(detail)
                    if status in TERMINAL_STATES and status not in SUCCESS_STATES
                    else ""
                ),
            }
            if status in ACTIVE_STATES:
                snapshot["metrics"] = self.metrics_snapshot(
                    job_id,
                    types=types,
                    pool=target_pool,
                    queue=target_queue,
                    window=window,
                    step=step,
                )
                _attach_pod_metrics(snapshot)
            yield snapshot

            if once or status in TERMINAL_STATES:
                return
            if timeout and time.monotonic() - started > timeout:
                raise WaitTimeout(job_id, status, timeout)
            time.sleep(interval)


def _watch_pods(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(pod.get("name") or ""),
            "replicaType": pod.get("replicaType", ""),
            "status": pod.get("status") or pod.get("podPhase", ""),
            "nodeName": pod.get("nodeName", ""),
            "restarts": pod.get("restartCount"),
            "metrics": {},
        }
        for pod in detail.get("pods") or []
        if isinstance(pod, Mapping)
    ]


def _attach_pod_metrics(snapshot: dict[str, Any]) -> None:
    """Fold the per-pod latest values into the pod rows, keeping metric-only pods."""
    latest: dict[str, dict[str, Any]] = snapshot["metrics"].get("latest") or {}
    known = set()
    for pod in snapshot["pods"]:
        pod["metrics"] = latest.get(pod["name"], {})
        known.add(pod["name"])
    # A pod that reports metrics but is missing from DescribeJob (a replaced worker, say)
    # is still worth showing rather than silently dropping.
    for name in latest:
        if name not in known:
            snapshot["pods"].append(
                {
                    "name": name,
                    "replicaType": "",
                    "status": "",
                    "nodeName": "",
                    "restarts": None,
                    "metrics": latest[name],
                }
            )


def _parse_time(value: Any) -> float | None:
    """AIHC timestamps are ISO-8601 with a ``Z``, which 3.10's fromisoformat rejects."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def job_started_at(detail: Mapping[str, Any]) -> str:
    """When the job actually started running, from the timeline (``''`` if not yet)."""
    for entry in detail.get("jobTimeLine") or []:
        if isinstance(entry, Mapping) and entry.get("conditionType") == "Running":
            return str(entry.get("time") or "")
    return ""


def job_elapsed(detail: Mapping[str, Any], now: float | None = None) -> float | None:
    """Seconds the job has been running.

    ``runTimeNanoseconds`` is the API's own counter and needs no clock agreement between
    this machine and the cluster, so it wins; the ``Running`` timeline entry (falling back
    to ``createdAt``) covers responses that omit it. The two matched to within a second on
    a live job, so either is fine.
    """
    api_runtime = _number(detail.get("runTimeNanoseconds"))
    if api_runtime > 0:
        return api_runtime / 1e9
    started = _parse_time(job_started_at(detail) or detail.get("createdAt"))
    if started is None:
        return None
    finished = _parse_time(detail.get("finishedAt"))
    end = finished if finished is not None else (now if now is not None else time.time())
    return max(0.0, end - started)


def _number(value: Any) -> float:
    """Queue quantities arrive as strings, and sometimes as '62.0' rather than '62'."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _accelerators(resource: Any) -> dict[str, float]:
    """`{descriptor: card count}` from a ResourceAmount's acceleratorCardList."""
    out: dict[str, float] = {}
    if not isinstance(resource, Mapping):
        return out
    for card in resource.get("acceleratorCardList") or []:
        if not isinstance(card, Mapping):
            continue
        # `allocated` entries carry an empty acceleratorType, so key on the descriptor
        # (which is also what jobSpec.resources uses).
        key = str(card.get("acceleratorDescription") or card.get("acceleratorType") or "")
        if key:
            out[key] = out.get(key, 0.0) + _number(card.get("acceleratorCount"))
    return out


def _failure_reason(detail: Mapping[str, Any]) -> str:
    """Best-effort explanation pulled from the timeline or pod list."""
    for entry in reversed(list(detail.get("jobTimeLine") or [])):
        if isinstance(entry, Mapping) and entry.get("conditionMessage"):
            return str(entry["conditionMessage"])
    for pod in detail.get("pods") or []:
        if isinstance(pod, Mapping) and pod.get("reason"):
            return f"{pod.get('name')}: {pod['reason']}"
    return str(detail.get("enableBcclErrorReason") or "")


def summarize_job(job: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a JobItem into the handful of fields worth printing."""
    spec = job.get("jobSpec") or {}
    if not isinstance(spec, Mapping) or "image" not in spec:
        # Multi-role job: report the first role's spec.
        roles = [v for v in (spec.values() if isinstance(spec, Mapping) else []) if isinstance(v, Mapping)]
        spec = roles[0] if roles else {}
    return {
        "jobId": job.get("jobId") or job.get("jobid") or "",
        "name": job.get("name", ""),
        "status": job.get("status", ""),
        "jobType": job.get("jobType", ""),
        "queue": job.get("queue") or job.get("queueId") or "",
        "replicas": spec.get("replicas"),
        "gpuCount": job.get("gpuCount"),
        "nodeCount": job.get("nodeCount"),
        # DescribeJobs already carries the job-wide averages, so listing shows load
        # without a metrics call per job.
        "gpuUtil": job.get("gpuUtilizationPercent"),
        "gpuMemUtil": job.get("gpuMemoryUtilizationPercent"),
        "createdAt": job.get("createdAt", ""),
        "finishedAt": job.get("finishedAt", ""),
    }
