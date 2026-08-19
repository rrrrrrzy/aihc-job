# README.agent.md — driving `aihc-job` programmatically

For an agent or script that submits and supervises Baidu AIHC jobs through this tool.
Humans want [README.md](README.md); someone changing this repo's code wants
[CLAUDE.md](CLAUDE.md).

Everything here is a stable contract: exit codes, `--json` shapes and the offline
guarantees are treated as API and will not be renumbered or reshaped silently.

## Three rules that cause most failures

1. **Global flags go before the subcommand.** `aihc-job --json list` works;
   `aihc-job list --json` exits `2` with `unrecognized arguments: --json`. The same
   applies to `-p/--pool`, `-q/--queue`, `--env-file`, `-C/--config`, `-v`.
2. **Check with `--dry-run` before spending GPUs.** `submit --dry-run` and `render`
   never touch the network, so a plan can be reviewed for free.
3. **Branch on the exit code, not on stderr text.** Messages are for humans and may be
   reworded; the codes are fixed.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| `0` | success | continue |
| `1` | unexpected error | report; do not retry blindly |
| `2` | usage or template error | fix the arguments/template — retrying identical input cannot help |
| `3` | job reached a **non-success** terminal state (`Failed`, `Stopped`, `ManualTermination`) | the submit worked, the *run* failed: read logs and events |
| `4` | config/credentials | missing AK/SK, pool, queue, or an `--env-file` that does not exist |
| `5` | wait timed out | the job is still running; poll again or extend `--timeout` |
| `6` | API error (non-2xx from AIHC) | inspect `code`/`requestId` in the message; 4xx will not fix itself |

`130` is a keyboard interrupt.

## stdout / stderr / `--json`

- `--json` prints **exactly one JSON document to stdout**; progress, warnings and log
  lines go to stderr. Parse stdout only.
- Two streaming exceptions: `logs --follow` prints raw lines, and `watch --json` prints
  **one JSON document per refresh** (JSON Lines) — read it line by line.
- In text mode `logs` strips the container-runtime prefix (`<ts> stdout F `); `--raw` and
  `--json` keep the API response verbatim.

## Preflight

```bash
aihc-job --json config show
```

```jsonc
{
  "region": "cn-bj", "endpoint": "aihc.bj.baidubce.com",
  "access_key": "ALTAK…****", "secret_key": "…****",   // always masked
  "pool": "aihc-serverless",          // "" means submits will fail with exit 4
  "queue": "aihcq-xxxxxxxxxxxx",
  "queue_pool": "aihc-xxxxxxxxxxxx",  // "" disables capacity checks + auto-queue silently
  "sources": ["/path/to/.env"],       // which files contributed
  "env_file": "/path/to/.env",
  "variables": { "AIHC_IMAGE": "…", "AIHC_WORKDIR": "…", "AIHC_OWNER": "…" }
}
```

A missing `queue_pool` is the quiet failure mode: capacity checking and auto-queue turn
themselves off rather than erroring, so a job can sit pending behind a full queue.

## Recipe: submit and supervise

```bash
# 1. validate offline (no network, no cost)
aihc-job --json submit -f job.json --name run-42 --dry-run   # {action,resourcePoolId,queueID,body}

# 2. submit
aihc-job --json submit -f job.json --name run-42             # {requestId,jobId,jobName,pool,queue}

# 3. supervise: whichever fits the caller
aihc-job --json wait job-xxxx --until terminal                # exit 3 if it failed
aihc-job --json watch job-xxxx --interval 30                  # JSONL, status + GPU load
aihc-job logs job-xxxx --follow                               # rank-0 lines to stdout
```

`submit --wait` / `--wait-running` / `--follow` fold steps 2 and 3 together; with `--json`
the final document is the full job detail rather than the create response.

Read `jobId` from step 2 and keep it: every later command needs it, and the job's own
queue may differ from the configured one (see below).

## Recipe: diagnose a failure (exit 3)

```bash
aihc-job --json get job-xxxx            # status, jobTimeLine[].conditionMessage, pods[]
aihc-job logs job-xxxx --pod <name>     # rank-0 by default; --pod for a specific worker
aihc-job --json events job-xxxx         # scheduling/image-pull problems live here, not in logs
aihc-job --json metrics job-xxxx --since 30m --history   # still queryable after the job ends
```

Order matters: a job that never started has no logs but does have events. `get` also
carries `gpuUtilizationPercent` / `gpuMemoryUtilizationPercent` / `runTimeNanoseconds`.

## Command → JSON shape

| Command | stdout with `--json` |
|---|---|
| `submit` | `{requestId, jobId, jobName, pool, queue}` — or the job detail with `--wait` |
| `submit --dry-run` | `{action, resourcePoolId, queueID, body}` |
| `render -f …` | `{body, summary}` |
| `list` | the `DescribeJobs` envelope: `{totalCount, jobs:[…]}` |
| `get` | the `DescribeJob` detail (`--pods` adds `pods`/`historyPods`) |
| `pods` | `{pods:[{name, replicaType, status, nodeName, PodIP, …}]}` |
| `events` | `{events:[{lastTimestamp, reason, count, message}]}` |
| `nodes` | the `DescribeJobNodes` response (always JSON) |
| `logs` | `{logs:[line, …], nextMarker}` |
| `wait` | the final job detail |
| `watch` | **JSONL**, one snapshot per refresh (shape below) |
| `metrics` | `{jobId, startTime, endTime, step, types, series, latest, errors}` |
| `queues` | `{queues:[{queueId, queueName, queueType, depth, parent, opened, capacity, allocated, free, cpuCores, memoryGi}]}` — flattened tree; submittable queues are the leaves |
| `pools` | the `DescribeResourcePools` envelope |
| `stop` / `delete` | a list, one result per job ID |
| `priority` | the `ModifyJob` result |
| `config show` | see Preflight |
| `raw <Action>` | the raw API response (always JSON) |

### `watch` snapshot

```jsonc
{
  "jobId": "job-xxxx", "name": "run-42", "status": "Running",
  "queue": "aihcq-xxxxxxxxxxxx",           // the queue the job really lives in
  "createdAt": "…", "startedAt": "…", "finishedAt": "",
  "elapsedSeconds": 2040.0,
  "gpuCount": 8, "nodeCount": 1,
  "gpuUtil": 51.25, "gpuMemUtil": 40.15,   // job-wide averages, free from DescribeJob
  "sampledAt": 1787117343,
  "pods": [{ "name": "job-xxxx-master-0", "replicaType": "master", "status": "Running",
             "nodeName": "…", "restarts": 0,
             "metrics": { "GpuUsage": 78.6, "GpuMemoryUsage": 48.6, "GpuPowerUsage": 268.8,
                          "GpuTemperature": 52.0, "CpuUsage": 12.1, "MemoryUsage": 3.5 } }],
  "metrics": { "step": 30, "series": { "GpuUsage": { "job-xxxx-master-0": [{"time":…,"value":…}] } },
               "latest": {…}, "errors": {} },
  "reason": ""                             // set only on a non-success terminal state
}
```

The stream ends when the job is terminal, so the last line always carries the final
status. `watch` exits `3` on a non-success end (like `wait`); `--once` emits one snapshot
and always exits `0`.

## Templates

JSON always, YAML if PyYAML is installed. Flattened view of the `CreateJob` body.
Minimum viable job:

```json
{ "name": "run-42", "image": "registry/img:tag", "command": "bash /share/you/run.sh",
  "replicas": 1, "gpu": "a800:8" }
```

- **The key set is closed.** An unrecognised key is an error, not a silent default —
  a typo'd `faultTolerence` would otherwise cost a multi-node run. Accepted keys:
  `advancedSettings alertConfig command datasources enableBccl enableRDMA envs
  faultTolerance faultToleranceArgs framework gpu hostNetwork image imageConfig jobType
  labels name pool preInitCommand priority queue replicas resources retentionPeriod roles
  tensorboardConfig visibleScope` (plus the aliases `dataSources`, `env`, `resourcePoolId`,
  `tensorboard`). Keys starting with `_` are comments.
- `gpu` grammar: `"<alias>:<count>[,cpu=N][,memory=GiB][,sharedMemory=GiB]"`, e.g.
  `a800:8,cpu=122,memory=1964`. Aliases resolve to Baidu descriptors
  (`a800` → `baidu.com/a800_80g_cgpu`); a full descriptor also works. `replicas` counts
  **nodes**, so total cards = `replicas × count`.
- `image` **must** carry a tag; validated locally.
- `priority` defaults to `high` in this repo (the API's own default is `normal`).
- `{{VAR}}` placeholders are substituted from `.env` plus the environment *before*
  validation. `{{VAR:-fallback}}` is optional; an unset variable is an error, never an
  empty string. Never `$VAR` — that belongs to the remote shell (`$RANK`,
  `${MASTER_ADDR}` must survive into the container).
- Multiple `-f` files merge left to right; explicitly passed flags win. List-valued flags
  replace the template's list wholesale, except `--env`, which merges.
- Everything checkable offline is checked offline: image tag, job-name charset,
  `replicas >= 1`, BCCL needs ≥2 replicas, fault tolerance is PyTorchJob-only, CFS needs
  `options.cfsInstanceId`.

## Queue semantics

- `queueID` is asymmetric: fully managed pools take the queue **ID** and
  `resourcePoolId=aihc-serverless`; self-managed pools take the queue **name** and the
  real pool ID.
- **`--auto-queue` is on by default.** The configured queue is kept when the request fits;
  otherwise the job moves to the emptiest queue that does, across chip types, and the
  choice is printed to stderr (`auto-queue -> aihcq-…`) and recorded in
  `JobManager.last_queue_report`.
- Consequence for reproducibility: the landing queue depends on occupancy at submit time.
  Use `--no-auto-queue` to pin, which fails with exit `4` and lists queues that would fit.
- Capacity checking never blocks a submit: a `DescribeQueues` failure logs a warning, and
  no `queue_pool` skips the check entirely. Only a definite "does not fit" with
  `--no-auto-queue` raises.
- `--no-check-capacity` submits regardless and lets the job queue up.
- **Metrics need the job's own queue.** `DescribeJobMetrics` answers `403 AccessDenied` for
  any other queue, so the tool reads the real one off `DescribeJob` and caches it. Do not
  assume the configured queue when calling the API directly.

## Metrics semantics

- Values are **per pod, averaged over that pod's GPUs**. There is no per-device
  breakdown — one straggling GPU of eight shows up only as a dip in the average. Run
  `nvidia-smi` inside the job for per-device detail.
- Default metric types: `GpuUsage GpuMemoryUsage GpuPowerUsage GpuTemperature CpuUsage
  MemoryUsage`. Also available: `GpuPipeTensorUsage CpuTime MemoryAllocation
  DiskReadRate DiskWriteRate RDMA{Send,Recv}DataRate RDMA{Send,Recv}PacketsRate
  RDMA{Send,Recv}ErrorRate RDMAHealth PCIE{Send,Recv}DataRate NVLink{Send,Recv}DataRate`.
- Percentages are 0–100; power is watts, temperature °C, `*Rate` bytes/s,
  `MemoryAllocation` bytes.
- **Cost:** one API call per metric type per refresh, plus one `DescribeJob`. Trim with
  `--metric`, control frequency with `--interval`. A queued job costs one call per round.
- `--step` sets sampling granularity in seconds (30 here; the API's own default is 5
  minutes, too coarse to watch). Samples land within a couple of seconds of real time.
- A metric type that fails is recorded under `errors` and the rest are still returned —
  RDMA metrics are absent on non-RDMA nodes.
- Metrics stay queryable after a job is terminal.

## Logs semantics

- The rank-0 pod is chosen automatically (`master-0` / `chief-0` / `head` / `worker-0`);
  `--pod` overrides. A job with no pods yet raises `NoPods`.
- Pagination follows `nextMarker`; when following, an empty marker means "nothing new
  yet", not "end of stream". Following stops when the job is terminal.
- `--keywords` filters server-side, `--max-lines` caps a page.

## Python API

Prefer this over shelling out when embedding.

```python
from aihc_job import AihcClient, JobManager, load_config
from aihc_job.errors import ApiError, ConfigError, JobFailed, TemplateError, WaitTimeout

jobs = JobManager(AihcClient(load_config()))

job = jobs.submit({"name": "run-42", "image": "registry/img:tag",
                   "command": "bash /share/you/run.sh", "gpu": "a800:8",
                   "enableRDMA": True})            # ConfigError if no queue fits
jobs.wait(job["jobId"])                            # JobFailed on a bad terminal state

for snapshot in jobs.watch(job["jobId"], interval=30):
    for pod in snapshot["pods"]:
        if snapshot["status"] == "Running" and (pod["metrics"].get("GpuUsage") or 0) < 5:
            ...                                    # stalled: 0% GPU while Running
```

Also useful: `jobs.metrics_snapshot(job_id)`, `jobs.metrics(job_id, "GpuUsage",
start_time=…, end_time=…, step=30)` → `{podName: [{"time","value"}]}`,
`jobs.queues(pool)`, `jobs.find_queues_for(pool, descriptor, count)`,
`jobs.iter_logs(job_id, follow=True)`, `jobs.dry_run(template)`,
`models.build_create_job_body(template)` for validation without a client. Exceptions map
1:1 onto the exit codes above.

## Anything not wrapped

```bash
aihc-job raw <Action> --method GET|POST --query k=v --body '{"…":"…"}'
```

Signing, retries and the version headers are handled. Prefer this over hand-rolling HTTP.
`docs/aihc-api-notes.md` records the field names and quirks that are already verified.

## Hard rules

- Never write an AK/SK into a template, an example, or any tracked file — they belong in
  `.env` (gitignored, mode 600) or the environment.
- Never make `submit --dry-run` or `render` perform I/O over the network.
- Do not renumber exit codes or reshape `--json` output; callers branch on both.
- `delete` refuses to run non-interactively without `--yes`. Deleting is not undoable —
  prefer `stop` while a run is still interesting.
- Do not present GPU load as per-device; it is a per-pod average.
