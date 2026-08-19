# aihc-job

Submit and manage training jobs on **Baidu AIHC (百度百舸 / Baige)** from the command
line. Built for the case where the local box has a handful of GPUs and anything bigger
has to go to the cluster.

Talks to the 百舸 OpenAPI v2 directly (`POST /?action=CreateJob&…`) with its own
`bce-auth-v1` signer, so the only runtime dependency is `requests` — no
`bce-python-sdk`, no `aihc` binary.

- **Driving this from an agent or a script?** → [README.agent.md](README.agent.md)
  (exit codes, `--json` shapes, recipes)
- **Every template key?** → [docs/template-reference.md](docs/template-reference.md)
- **Changing this repo's code?** → [CLAUDE.md](CLAUDE.md)

## Install

```bash
pip install -e .            # or: pip install -e '.[dev]' for tests + YAML templates
```

`aihc-job` and `python -m aihc_job` are equivalent. Without installing, run
`python -m aihc_job` from the repo (only `requests` is needed).

## Configure: one `.env`

Everything that differs per person lives in `.env` at the repo root:

```bash
cp .env.example .env
chmod 600 .env                # it holds your secret key
$EDITOR .env
aihc-job config show          # shows what was picked up; secrets masked
```

| Variable | What it is |
|---|---|
| `AIHC_AK` / `AIHC_SK` | **your** BCE keys — job ownership follows the key |
| `AIHC_REGION` | `cn-bj` (also `cn-gz cn-su cn-bd cn-fwh cn-yq`) |
| `AIHC_POOL` | `aihc-serverless` for a fully managed pool, else the pool ID |
| `AIHC_QUEUE` | default queue: *ID* for managed pools, *name* for self-managed |
| `AIHC_QUEUE_POOL` | the **real** pool ID; without it capacity checks and auto-queue switch off |
| `AIHC_IMAGE` | your container image, tag included |
| `AIHC_WORKDIR` | your directory on the shared filesystem, e.g. `/share/you` |
| `AIHC_OWNER` | goes on every job as the `owner` label |
| `AIHC_CFS_INSTANCE` / `AIHC_CFS_MOUNT` | the CFS behind `/share` |

`.env.example` documents all of them. `.env` is gitignored and is read from the repo root
**wherever you run the tool from**, so `aihc-job` in your home directory still uses this
checkout's settings. `--env-file` points somewhere else.

Older paths still work and merge per field, so an already-configured machine needs no
change — first hit wins per field:

```
flags → exported environment → .env → $AIHC_CONFIG → ./.aihc/config* → ~/.aihc/config*
```

An export beating `.env` is deliberate: `AIHC_QUEUE=aihcq-x aihc-job submit …` works for a
one-off without editing the file. `aihc-job config init` writes `~/.aihc/config.json`
(mode 600) for anyone who prefers a per-machine file, and the official `aihc` CLI's
`~/.aihc/config` is read as-is.

Finding the IDs, if you do not have them yet:

```bash
aihc-job pools                            # --type dedicatedV2 for fully managed pools
aihc-job queues -p aihc-xxxxxxxxxxxx      # per-queue GPU capacity, free counts, job counts
```

> **Managed pools take two different pool IDs.** Job actions want the literal
> `aihc-serverless` plus the queue **ID**; `DescribeQueues` wants the *real* pool ID and
> rejects the sentinel — that is why `AIHC_POOL` and `AIHC_QUEUE_POOL` are separate.
> `queues` prints the tree: physical queues with their submittable children indented
> under them and a `SUBMIT` column marking the ones a job can target. `pools` can
> legitimately come back empty under permission scoping; when it does, read the IDs off
> something already running with
> `aihc-job raw DescribeDevInstances --method GET --no-pool --query pageSize=100`.

## Your first job

```bash
aihc-job submit -f examples/cfs-share.json --name smoke --wait
```

That is a 1-GPU probe: it pulls your image, mounts `/share`, prints `nvidia-smi` and the
torch version, and exits. If it succeeds, the plumbing works.

## Submitting

```bash
aihc-job submit -f job.json --dry-run       # prints the exact request, sends nothing
aihc-job submit -f job.json
aihc-job submit -f job.json --wait          # block until the job ends
aihc-job submit -f job.json --follow        # wait for start, then stream rank-0 logs
```

Templates are JSON (or YAML with PyYAML) — a flattened view of the API body:

```jsonc
{
  "name": "vla-pretrain",                   // lowercase, '-' separated
  "framework": "pytorch",                   // PyTorchJob | TFJob | MPIJob | RayJob
  "image": "{{AIHC_IMAGE}}",                // tag required
  "command": "bash -c 'cd {{AIHC_WORKDIR}}/proj && torchrun --standalone --nproc_per_node 8 train.py'",
  "replicas": 4,                            // nodes, not GPUs
  "gpu": "a800:8,cpu=122,memory=1964",
  "enableRDMA": true,
  "enableBccl": true,                       // needs >= 2 replicas + 8 cards + RDMA
  "envs": { "NCCL_DEBUG": "INFO" },
  "datasources": [{ "type": "cfs", "mountPath": "/share",
                    "instanceId": "{{AIHC_CFS_INSTANCE}}", "mountPoint": "{{AIHC_CFS_MOUNT}}" }],
  "faultTolerance": true,
  "retentionPeriod": "1d"
}
```

`{{VAR}}` comes from your `.env`, which is what keeps the shipped examples free of anyone's
personal paths. (Braces, not `$VAR` — `$RANK` and `${MASTER_ADDR}` in a command have to
reach the *remote* shell untouched.) An unset variable is an error naming the file to fix,
not a blank; `{{VAR:-fallback}}` makes one optional.

Ready-made templates:

| File | What it is |
|---|---|
| `examples/a800-8gpu.json` | 8×A800, a whole node (`cpu=122, memory=1964`) |
| `examples/a800-4gpu.json` | 4×A800, half a node (`cpu=32, memory=128`) |
| `examples/cfs-share.json` | 1-GPU probe: does the image pull and does `/share` mount? |
| `examples/base.json` | merge base for `-f base.json -f my-exp.json` |
| `examples/pytorch-multi-node.json` | multi-node + BCCL + fault tolerance reference |
| `examples/gpu-load-probe.json` | 1 GPU pinned at 100% for 4 min, to try `watch` against |

Several `-f` files merge left to right and flags override the result, so a shared base plus
a small per-experiment file works well:

```bash
aihc-job submit -f examples/base.json -f exp.json \
  --name vla-lr3e4 --replicas 8 --env LR=3e-4 --gpu a800:8
```

Only flags you actually pass override, list-valued flags replace the template's list
wholesale, and `--env` merges into the template's env map. `aihc-job render -f job.json`
validates offline and prints the exact API body.

Unknown template keys are rejected rather than ignored — a typo'd `faultTolerence` would
otherwise cost a multi-node run. Everything checkable offline (image tag, replica count,
BCCL prerequisites, CFS fields) is checked before a request is sent.

## Queues

Before sending, `submit` checks the target queue has room for `replicas × cards`:

```console
$ aihc-job submit -f examples/a800-8gpu.json --name run1 --gpu a800:24
config error: job needs 24 x baidu.com/a800_80g_cgpu but queue aihcq-xxxxxxxxxxxx has 16 free.
  Queues that fit:
    aihcq-yyyyyyyyyyyy  team-a800-2-sub-f  68/80 free
    aihcq-zzzzzzzzzzzz  team-a800-3-sub-f  38/112 free
  Pass -q <queueId>, or --auto-queue to pick automatically.
```

**`--auto-queue` is on by default**: your queue is kept whenever it fits, and otherwise the
job moves to the emptiest one that does — across chip types, so `--gpu 4090:2` with an A800
queue configured lands on the 4090 queue. The choice is printed to stderr:

```console
$ aihc-job submit -f examples/a800-4gpu.json --name run1
auto-queue -> aihcq-yyyyyyyyyyyy: needs 4 x a800_80g_cgpu
submitted run1 -> job-xxxxxxxx
```

The trade-off: where a command lands now depends on cluster occupancy at submit time. So
`--dry-run` reports the *configured* queue and warns that it may move, and
`--no-auto-queue` is the reproducible path. Capacity checking never blocks a submit — a
`DescribeQueues` failure is a warning, and without `AIHC_QUEUE_POOL` the check is skipped
silently. `--no-check-capacity` submits regardless and lets the job wait in the queue.

## Watching a job

```console
$ aihc-job watch job-xxxxxxxx
job-xxxxxxxx  my-run  Running
queue aihcq-xxxxxxxxxxxx   8 gpu / 1 node   elapsed 0:03:29   job avg gpu 96.4% mem 41.8%   at 13:48:03

POD       STATUS   NODE            RESTARTS  GPU%   GPUMEM%  POWER  TEMP  CPU%  MEM%  GPU% TREND
master-0  Running  192.168.32.231  0         100.0  41.8     303W   57C   6.3   0.8   ▆▇███▇█
# per-pod averages over all its GPUs, 30s samples
```

The trend column is `GpuUsage` history over `--window` (300 s) on a fixed 0–100 scale, so
an idle job looks idle.

```bash
aihc-job watch job-xxxx --once              # one snapshot, then exit
aihc-job watch job-xxxx --interval 30       # refresh every 30s (default 10)
aihc-job watch job-xxxx --rdma              # add RDMA tx/rx columns for multi-node runs
aihc-job watch job-xxxx --metric GpuUsage --metric GpuTemperature   # pick the columns
```

`watch` runs until the job ends and exits `3` if that end was not `Succeeded`, so it
doubles as `wait` with a view of what the GPUs were doing.

For a single reading or a numeric history:

```console
$ aihc-job metrics job-xxxx --metric GpuUsage --metric GpuPowerUsage --since 6m --step 60 --history
master-0
TIME      GPU%   POWER
13:45:24  100.0  300W
13:46:24  100.0  306W
13:47:24  100.0  303W
```

Three things worth knowing about the numbers:

- **They are per pod, averaged over that pod's GPUs.** The API exposes no per-device
  breakdown, so one straggling GPU out of eight only shows up as a dip in the average —
  use `nvidia-smi` inside the job for per-device detail.
- Sampling is `--step` seconds apart (30 by default; the API's own default is 5 minutes)
  and lands within a couple of seconds of real time.
- Each metric type costs one API call per refresh, so `--metric` trims cost and
  `--interval` controls it. Metrics stay queryable after a job finishes, which is what
  makes a post-mortem `--history` possible.

## Managing jobs

```bash
aihc-job list                       # add --status Running, --name substr, --all-queues
aihc-job get job-xxxx --pods
aihc-job logs job-xxxx --follow     # rank-0 pod by default; --pod to choose
aihc-job events job-xxxx            # where scheduling and image-pull problems show up
aihc-job wait job-xxxx --until running --timeout 1800
aihc-job priority job-xxxx high     # only while still queued
aihc-job stop job-xxxx
aihc-job delete job-xxxx --yes
```

`list` shows each job's `gpuUtil`/`gpuMemUtil` at no extra cost — those come with the
listing response. Anything this tool has not wrapped is reachable directly:

```bash
aihc-job raw DescribeJobWebterminal --body '{"jobId":"job-xxxx"}'
```

## Mounting a CFS share at the same path

If `/share` on your machine is a Baidu CFS (NFS) mount, mount it into the container at
`/share` too and a command that works locally works unchanged on AIHC. Read the values off
the existing mount rather than the console:

```console
$ mount | grep /share
cfs-xxxxxxxx.lb-yyyyyyyy.cfs.bj.baidubce.com:/ on /share type nfs4 (rw,...)
#  └── instance ID     └── mount-target domain
```

Both fields are **required by `CreateJob`** — put them in `.env` as `AIHC_CFS_INSTANCE` and
`AIHC_CFS_MOUNT`. As a one-liner (flat keys are lifted into `options`, and `instanceId` /
`mountPoint` are accepted as aliases):

```bash
aihc-job submit --name cfs-check --image '{{AIHC_IMAGE}}' --command 'ls /share' \
  --datasource "type=cfs,instanceId={{AIHC_CFS_INSTANCE}},mountPoint={{AIHC_CFS_MOUNT}},mountPath=/share"
```

> Do not be fooled by `aihc-job get <jobId>`: it reports `cfsInstanceId` and
> `cfsMountPoint` as **empty strings** on jobs that were created successfully. Omitting
> them on create fails with `400 InvalidParam: cfs datasource cfsInstanceId is required`.

Shared memory can come from an `emptydir` instead of the `sharedMemory` resource:

```jsonc
{ "type": "emptydir", "name": "devshm", "mountPath": "/dev/shm", "medium": "Memory" }
```

Two caveats: **CFS is VPC-scoped**, so the pool's nodes must be able to reach the mount
target (`examples/cfs-share.json` is a cheap 1-GPU probe for exactly that), and a shared
filesystem near capacity will kill a long run partway through — check free space before
pointing checkpoints at it. For read-heavy training data PFS is faster; CFS is the right
choice when the point is sharing the same tree as your local box.

## Python

```python
from aihc_job import AihcClient, JobManager, load_config

jobs = JobManager(AihcClient(load_config()))

job = jobs.submit({"name": "vla-pretrain", "image": "registry/img:tag",
                   "command": "bash /share/you/run.sh", "replicas": 4,
                   "gpu": "a800:8", "enableRDMA": True})
jobs.wait(job["jobId"])                       # raises JobFailed on a bad terminal state
for line in jobs.iter_logs(job["jobId"]):
    print(line)
```

`JobManager.watch` is the generator the `watch` command renders, so a supervisor can react
to load directly. See [README.agent.md](README.agent.md) for the full surface.

## Handing this to a colleague

Everything personal is in `.env`, so onboarding is: copy the tree, copy the template, fill
in a handful of values.

```bash
cp -r /path/to/aihc-job ~/aihc-job && cd ~/aihc-job   # the original may be read-only
cp .env.example .env && chmod 600 .env
$EDITOR .env          # AIHC_AK, AIHC_SK, AIHC_IMAGE, AIHC_WORKDIR, AIHC_OWNER
aihc-job config show  # confirm the .env was picked up
aihc-job submit -f examples/cfs-share.json --name smoke --wait
```

Pool, queue-pool and the CFS pair are tenant-wide and can stay as shipped. `AIHC_QUEUE`
only decides where jobs land *first* — auto-queue moves them if they do not fit.

Two things that are easy to miss:

- **IP allowlist.** If the new AK is IP-restricted, the machine's egress IP has to be on
  its allowlist or every call returns
  `403 IamSignatureInvalid, cause: request ip not allowed`. That is a console-side IAM
  change; nothing here works around it.
- **`priority` defaults to `high`** (`models.DEFAULT_PRIORITY`), and queues are usually
  shared by hardware rather than by person. If everyone submits at `high` the field stops
  meaning anything — agree as a group, or put `"priority": "normal"` in your templates.

Each copy keeps its own `.env`. `pip install -e .` needs a writable tree; otherwise
`python -m aihc_job` works straight from the directory.

## Tests

```bash
python -m pytest                     # no network, no credentials, no mocking library
python -m pytest tests/test_auth.py -k signature -v
```

## Scope

Implemented: signing, config resolution, pools, queues, job
create/list/get/pods/events/nodes/logs/wait/watch/metrics/stop/delete/priority, templates
with merge + validation, and a `raw` escape hatch.

Not yet: code upload (the official `aihc code upload` has no OpenAPI equivalent — mount
code from PFS/BOS or bake it into the image), dev machines, inference services,
datasets/models, `job exec`/WebTerminal, batch stop, alert configuration. See
[docs/aihc-api-notes.md](docs/aihc-api-notes.md) for the API facts those would build on.
