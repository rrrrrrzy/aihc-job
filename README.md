# aihc-job

Submit and manage training jobs on **Baidu AIHC (百度百舸 / Baige)** from the CLI or
from Python. Built so that an agent can drive large multi-node runs on Baidu while
the local 4×A800 box stays for interactive work.

Talks to the 百舸 OpenAPI v2 directly (`POST /?action=CreateJob&…`) with its own
`bce-auth-v1` signer, so the only runtime dependency is `requests` — no
`bce-python-sdk`, no `aihc` binary.

## Install

```bash
pip install -e .            # or: pip install -e '.[dev]' for tests + YAML templates
```

`aihc-job` and `python -m aihc_job` are equivalent entry points.

## Configure: one `.env` at the repo root

Everything that differs per user lives in `.env`. Copy the template, fill it in, done:

```bash
cp .env.example .env
chmod 600 .env                # holds your secret key
$EDITOR .env
aihc-job config show          # shows what was picked up, secrets masked
```

`.env.example` documents every variable. The short version:

| Variable | What it is |
|---|---|
| `AIHC_AK` / `AIHC_SK` | **your** BCE keys — job ownership follows the key |
| `AIHC_REGION` | `cn-bj` (also `cn-gz cn-su cn-bd cn-fwh cn-yq`) |
| `AIHC_POOL` | `aihc-serverless` for a managed pool, else the pool ID |
| `AIHC_QUEUE` | default queue: *ID* for managed pools, *name* for self-managed |
| `AIHC_QUEUE_POOL` | the **real** pool ID; without it capacity checks and auto-queue switch off |
| `AIHC_IMAGE` | your container image, tag included |
| `AIHC_WORKDIR` | your directory on the shared filesystem, e.g. `/share/you` |
| `AIHC_OWNER` | goes on every job as the `owner` label |
| `AIHC_CFS_INSTANCE` / `AIHC_CFS_MOUNT` | the CFS behind `/share` |

`.env` is gitignored, so a colleague can take the whole checkout and only edit that one
file. It is read from the repo root **wherever you run the tool from**, so `aihc-job` in
`~` still uses the checkout's settings; `--env-file` or `$AIHC_ENV_FILE` points somewhere
else (a path that does not exist is an error, never a silent fallback).

Nothing else is required, but the older paths still work and merge per field, so an
already-configured machine needs no change. Precedence, first hit wins per field:

```
flags → exported environment → .env → $AIHC_CONFIG → ./.aihc/config* → ~/.aihc/config*
```

An exported variable beating `.env` is deliberate: `AIHC_QUEUE=aihcq-x aihc-job submit …`
works for a one-off without editing the file. `aihc-job config init` still writes
`~/.aihc/config.json` (mode 600) for anyone who prefers a per-machine file, and the
official `aihc` CLI's `~/.aihc/config` (flat YAML) is read as-is.

### Templates read the same variables

Any `{{VAR}}` in a template is substituted from `.env` (plus the environment), which is
what keeps `examples/` free of anyone's personal paths:

```jsonc
"image":   "{{AIHC_IMAGE}}",
"command": "bash -c 'cd {{AIHC_WORKDIR}}/myproject && torchrun --standalone --nproc_per_node 8 train.py'",
"labels":  { "owner": "{{AIHC_OWNER}}" }
```

Braces, not `$VAR`, precisely so that `$RANK` and `${MASTER_ADDR}` in a launch command
still reach the *remote* shell untouched. `{{VAR:-fallback}}` makes one optional; an unset
variable with no fallback is an error naming the file to edit, rather than an empty string
that would `cd` to `/`. Expansion happens before validation, so `render` and
`submit --dry-run` show the real values.

Find the pool and queue IDs:

```bash
aihc-job pools                       # add --type dedicatedV2 for fully managed pools
aihc-job queues -p cce-xxxxxxx       # shows per-queue GPU capacity and job counts
```

> **Managed pools take two different pool IDs.** Job actions want the literal
> `aihc-serverless` plus the queue **ID**; `DescribeQueues` wants the *real* pool ID
> and rejects the sentinel. `queues` prints the tree — physical queues with their
> submittable Elastic children indented under them, and a `SUBMIT` column marking the
> ones a job can target. Note `pools` can legitimately return an empty list even when a
> pool exists (permission scoping); when it does, read the pool/queue off something
> already running:
>
> ```bash
> aihc-job raw DescribeDevInstances --method GET --no-pool --query pageSize=100
> ```

## Submit a job

Ready-made templates for this environment (`{{AIHC_IMAGE}}` from your `.env`, `/share`
mounted at `/share`, RDMA on, resource shapes copied from jobs that actually run in the
queue):

| File | What it is |
|---|---|
| `examples/a800-8gpu.json` | 8×A800, a whole node (`cpu=122, memory=1964`) |
| `examples/a800-4gpu.json` | 4×A800, half a node (`cpu=32, memory=128`) |
| `examples/cfs-share.json` | 1-GPU probe: does the image pull and does `/share` mount? |
| `examples/base.json` | merge base for `-f base.json -f my-exp.json` |
| `examples/pytorch-multi-node.json` | multi-node + BCCL + fault tolerance reference |
| `examples/gpu-load-probe.json` | 1 GPU pinned at 100% for 4 min, to try `watch` against |

```bash
aihc-job submit -f examples/a800-8gpu.json --name my-run --dry-run
```

**[`docs/template-reference.md`](docs/template-reference.md) documents every tunable
parameter** — each key's type, default, constraint, and the API field it maps to, plus
the GPU descriptor table, all datasource types, `faultToleranceArgs` flags, and the
resource shapes observed in this queue.

A template is JSON (always) or YAML (needs PyYAML). It is a flattened view of the
`CreateJob` body: GPUs as `a800:8`, env vars and labels as plain maps.

```jsonc
{
  "name": "vla-pretrain",                         // lowercase, '-' separated, <= 50 chars
  "framework": "pytorch",                         // PyTorchJob | TFJob | MPIJob | RayJob
  "image": "registry.baidubce.com/.../torch:2.4", // tag is required
  "command": "bash /mnt/cluster/code/run.sh",
  "replicas": 4,                                  // nodes
  "gpu": "a800:8,cpu=96,memory=1024,sharedMemory=128",
  "enableRDMA": true,
  "enableBccl": true,                             // needs >= 2 replicas + 8 cards + RDMA
  "envs": { "NCCL_DEBUG": "INFO" },
  "datasources": [
    { "type": "pfs", "name": "pfs-xxxxxx", "sourcePath": "/", "mountPath": "/mnt/cluster" }
  ],
  "faultTolerance": true,
  "faultToleranceArgs": "--enable-replace=true --enable-hang-detection=true --max-num-of-unconditional-retry=2",
  "retentionPeriod": "1d"
}
```

Before sending, `submit` checks that the target queue actually has room for the
accelerators requested (`replicas x cards`), and refuses with the queues that would fit
rather than letting the job sit pending:

```console
$ aihc-job submit -f examples/a800-8gpu.json --name run1 --gpu a800:24
config error: job needs 24 x baidu.com/a800_80g_cgpu but queue aihcq-xxxxxxxxxxxx has 16 free.
  Queues that fit:
    aihcq-yyyyyyyyyyyy  team-a800-2-sub-f  68/80 free
    aihcq-zzzzzzzzzzzz  team-a800-3-sub-f  38/112 free
  Pass -q <queueId> or --auto-queue to pick automatically; --no-check-capacity to submit anyway.
```

**`--auto-queue` is on by default**: when the configured queue lacks room, the job moves
to the emptiest queue that fits and the choice is printed to stderr. The configured queue
is kept whenever it fits, so the default only takes effect when it would otherwise
block.

```console
$ aihc-job submit -f examples/a800-4gpu.json --name run1
auto-queue -> aihcq-yyyyyyyyyyyy: needs 4 x a800_80g_cgpu
submitted run1 -> job-xxxxxxxx
```

Pass `--no-auto-queue` to pin the job to the configured queue and fail instead. Worth
knowing: with auto-queue on, the queue a command lands in depends on cluster occupancy
at submit time, so `--dry-run` shows the *configured* queue and warns that it may move.
Use `--no-auto-queue` when you need a run to be reproducible.

The check needs the *real* pool ID (`DescribeQueues` rejects the `aihc-serverless`
sentinel that job actions require), so store it once as `queuepool`:

```bash
aihc-job --queue-pool aihc-xxxxxxxxxxxx config init
```

Without it the check is skipped silently; a `DescribeQueues` failure never blocks a
submit either.

```bash
# always check the request first -- this sends nothing
aihc-job submit -f job.json --dry-run

aihc-job submit -f job.json
aihc-job submit -f job.json --wait          # block until terminal; exit 3 if it failed
aihc-job submit -f job.json --follow        # wait for start, then stream rank-0 logs
```

Flags override template keys, and several `-f` files merge left to right — so a
shared `base.json` plus a small per-experiment file works well:

```bash
aihc-job submit -f examples/base.json -f exp.json \
  --name vla-lr3e4 --replicas 8 --env LR=3e-4 --gpu a800:8
```

Override rules match the official CLI: only explicitly passed flags override, and
list-valued flags replace the template's list wholesale — except `--env`, which
merges into the template's env map.

`render` validates a template offline and prints the exact API body:

```bash
aihc-job render -f job.json
```

## Mounting the CFS share (`/share`)

`/share` on this machine is a Baidu CFS (NFS) mount, so the same filesystem can be
mounted into the job container — mount it at `/share` too and a command that works
locally works unchanged on AIHC.

Read the values off the existing mount rather than the console:

```console
$ mount | grep /share
cfs-xxxxxxxx.lb-yyyyyyyy.cfs.bj.baidubce.com:/ on /share type nfs4 (rw,...)
#  └── instance ID     └── mount-target domain (cfsMountPoint)
```

Both fields are **required by `CreateJob`**, so put them in the template:

```jsonc
"datasources": [
  {
    "type": "cfs",
    "name": "share",
    "sourcePath": "/",                 // path inside the CFS instance
    "mountPath": "/share",             // path inside the container
    "options": {
      "cfsInstanceId": "cfs-xxxxxxxx",
      "cfsMountPoint": "cfs-xxxxxxxx.lb-yyyyyyyy.cfs.bj.baidubce.com"
    }
  }
]
```

> Do not be fooled by `aihc-job get <jobId>`: it reports `cfsInstanceId` and
> `cfsMountPoint` as **empty strings** on jobs that were created successfully. Omitting
> them on create fails with
> `400 InvalidParam: cfs datasource cfsInstanceId is required`.

Mounting at `/share` is why commands here look like
`bash /share/<user>/<project>/scripts/train.sh` — the same path as locally. The same
thing as a one-liner (flat keys are lifted into `options`, and `instanceId` /
`mountPoint` are accepted as aliases):

```bash
aihc-job submit --name cfs-check --image <image> --command 'ls /share' \
  --datasource "type=cfs,instanceId=cfs-xxxxxxxx,mountPoint=cfs-xxxxxxxx.lb-yyyyyyyy.cfs.bj.baidubce.com,mountPath=/share"
```

Shared memory in this pool comes from an `emptydir`, not the `sharedMemory` resource:

```jsonc
{ "type": "emptydir", "name": "devshm", "mountPath": "/dev/shm", "medium": "Memory" }
```

Two caveats:

- **CFS is VPC-scoped.** The pool's nodes must be able to reach the mount target.
  `examples/cfs-share.json` is a cheap 1-GPU probe to confirm that before a real run.
- **This share is nearly full** (182T of 185T used as of 2026-08-19). Point
  checkpoints somewhere with headroom, or a multi-node run will die partway through.

For read-heavy training data, PFS is the faster option AIHC is tuned for; CFS is the
right choice when the point is *sharing the exact same tree* as the local box.

## Manage jobs

```bash
aihc-job list                       # add --status Running, --name substr, --all-queues
aihc-job get job-xxxx --pods
aihc-job logs job-xxxx --follow     # rank-0 pod by default; --pod to choose
aihc-job events job-xxxx
aihc-job wait job-xxxx --until running --timeout 1800
aihc-job priority job-xxxx high     # only while still queued
aihc-job stop job-xxxx
aihc-job delete job-xxxx --yes
```

`list` shows the load of every job it prints (`gpuUtil`/`gpuMemUtil`) — those come with
`DescribeJobs`, so it costs nothing extra.

## Watch a job

`watch` is a live dashboard: job status plus per-pod GPU load, redrawn in place until the
job reaches a terminal state.

```console
$ aihc-job watch job-ncmxeqgd9ron
job-ncmxeqgd9ron  rzy-watch-probe  Running
queue aihcq-xxxxxxxxxxxx   1 gpu / 1 node   elapsed 0:03:29   job avg gpu 100.0% mem 1.7%   at 13:48:03

POD       STATUS   NODE            RESTARTS  GPU%   GPUMEM%  POWER  TEMP  CPU%  MEM%  GPU% TREND
master-0  Running  192.168.32.231  0         100.0  1.7      303W   57C   6.3   0.8   ███████
# per-pod averages over all its GPUs, 30s samples
```

The trend column is the `GpuUsage` history over `--window` (300 s by default) on a fixed
0–100 scale, so an idle job looks idle. Useful flags:

```bash
aihc-job watch job-xxxx --once              # one snapshot, exit 0
aihc-job watch job-xxxx --interval 30       # refresh every 30s (default 10)
aihc-job watch job-xxxx --rdma              # add RDMA tx/rx columns for multi-node runs
aihc-job watch job-xxxx --metric GpuUsage --metric GpuTemperature   # pick the columns
aihc-job watch job-xxxx --no-clear          # append refreshes instead of redrawing
```

`watch` exits `3` if the job ends in anything but `Succeeded`, so it doubles as `wait` with
a view of what the GPUs were doing. `--once` is an observation, not a wait, and always
exits `0`.

For a one-shot reading or a numeric history, use `metrics`:

```console
$ aihc-job metrics job-xxxx --metric GpuUsage --metric GpuPowerUsage --since 6m --step 60 --history
master-0
TIME      GPU%   POWER
13:45:24  100.0  300W
13:46:24  100.0  306W
13:47:24  100.0  303W
```

Three things to know about the numbers:

- **They are per pod, averaged over that pod's GPUs.** The API exposes no per-device
  breakdown, so one straggling GPU out of eight shows up only as a dip in the average.
  For per-device detail, run `nvidia-smi` inside the job.
- Sampling is `--step` seconds apart (30 by default; the API's own default is 5 minutes)
  and lands within a couple of seconds of real time.
- Each metric type is one API call per refresh, so `--metric` trims cost and
  `--interval` controls it. A queued job costs one `DescribeJob` per round and nothing else.

Metrics remain queryable after a job finishes, which is how a post-mortem
`metrics --history` works.

Anything not wrapped yet is reachable through `raw`:

```bash
aihc-job raw DescribeJobWebterminal --body '{"jobId":"job-xxxx"}'
```

## Agent-facing contract

- `--json` on any command prints one JSON document to stdout; logs and progress go
  to stderr. The two streaming commands are the exception: `logs --follow` prints lines,
  and `watch --json` prints one JSON document per refresh (JSON Lines).
- Exit codes: `0` ok, `1` error, `2` usage/template, `3` job ended in a non-success
  terminal state, `4` config/credentials, `5` wait timeout, `6` API error.
- `submit --dry-run` never contacts the API, so a plan can be reviewed before any
  GPU is allocated.
- `delete` refuses to run non-interactively without `--yes`.

## Python API

```python
from aihc_job import AihcClient, JobManager, load_config

config = load_config()
jobs = JobManager(AihcClient(config), config)

job = jobs.submit({
    "name": "vla-pretrain",
    "image": "registry.baidubce.com/.../torch:2.4",
    "command": "bash /mnt/cluster/code/run.sh",
    "replicas": 4,
    "gpu": "a800:8",
    "enableRDMA": True,
})
jobs.wait(job["jobId"])                       # raises JobFailed on Failed/Stopped
for line in jobs.iter_logs(job["jobId"]):
    print(line)
```

`JobManager.watch` is the same generator the `watch` command renders, so a supervisor can
act on load directly:

```python
for snapshot in jobs.watch(job_id, interval=30):
    for pod in snapshot["pods"]:
        if (pod["metrics"].get("GpuUsage") or 0) < 5:
            print(f"{pod['name']} looks stalled")
```

`jobs.metrics_snapshot(job_id)` gives the same numbers once, and
`jobs.metrics(job_id, "GpuUsage", start_time=…, end_time=…, step=30)` returns the raw
`{podName: [{"time", "value"}]}` series.

## Handing this to a colleague

Everything personal is in `.env`, so onboarding is: copy the tree, copy the template, fill
in four or five values.

```bash
cp -r /path/to/aihc-job ~/aihc-job && cd ~/aihc-job   # the original may be read-only
cp .env.example .env && chmod 600 .env
$EDITOR .env          # AIHC_AK, AIHC_SK, AIHC_IMAGE, AIHC_WORKDIR, AIHC_OWNER
aihc-job config show  # confirm the .env was picked up
aihc-job submit -f examples/cfs-share.json --name smoke --wait
```

`AIHC_POOL`, `AIHC_QUEUE_POOL` and the CFS pair are tenant-wide and already correct in the
template. `AIHC_QUEUE` only sets *where jobs land first* — request `--gpu 4090:2` with an
A800 queue configured and auto-queue moves the job to the 4090 queue anyway.

Two things that are easy to miss:

- **IP allowlist.** If the new AK is IP-restricted, the machine's egress IP has to be on
  its allowlist or every call returns
  `403 IamSignatureInvalid, cause: request ip not allowed`. That is a console-side IAM
  change; no setting here works around it.
- **`priority` defaults to `high`** (`models.DEFAULT_PRIORITY`), and these queues are
  shared by hardware, not by person. If everyone submits at `high` the field stops meaning
  anything — decide as a group, or set `AIHC_*`-independent `"priority": "normal"` in your
  own templates.

Each copy keeps its own `.env`, and `pip install -e .` needs a writable tree — otherwise
`python -m aihc_job` works straight from the directory (only `requests` is required).

## Tests

```bash
python -m pytest                     # no network access required
python -m pytest tests/test_auth.py -k signature -v
```

## Scope of this first version

Implemented: signing, config resolution, pools, queues, job create/list/get/pods/
events/nodes/logs/wait/watch/metrics/stop/delete/priority, templates with merge +
validation, and a `raw` escape hatch.

Not yet: code upload (the official `aihc code upload` has no OpenAPI equivalent —
mount code from PFS/BOS or bake it into the image), dev machines, inference
services, datasets/models, `job exec`/WebTerminal, batch stop, alert configuration.
See `docs/aihc-api-notes.md` for the API facts these would build on.
