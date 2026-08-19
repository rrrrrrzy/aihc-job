# Job template reference — every tunable parameter

Complete list of what a template accepts. Keys not in this list are rejected (a typo
is an error, not a silent default). Anything checkable offline is checked by
`aihc-job render -f <file>` / `submit --dry-run`, which never touch the network.

## Variables

Any string in a template may contain `{{VAR}}`, substituted from the repo-root `.env`
(and from exported environment variables, which win). `{{VAR:-fallback}}` makes one
optional; anything else unset is an error naming the file to fix, not a blank. Every
variable is documented in [`.env.example`](../.env.example); the shipped templates use:

| Variable | Used for |
|---|---|
| `AIHC_IMAGE` | `image` |
| `AIHC_WORKDIR` | the `/share/<you>` prefix inside `command` |
| `AIHC_OWNER` | `labels.owner` |
| `AIHC_CFS_INSTANCE` / `AIHC_CFS_MOUNT` | the `/share` cfs datasource |

Braces rather than `$VAR` because `command` is shell: `$RANK` and `${MASTER_ADDR}` have
to survive into the container untouched. Expansion runs *before* validation, so a
placeholder never trips the "image has no tag" or job-name checks.

Column "maps to" gives the field in the OpenAPI `CreateJob` body
(<https://cloud.baidu.com/doc/AIHC/s/Hmayv96tj>); `jobSpec.*` fields land inside the
per-role spec.

## Identity and placement

| Key | Type | Default | Notes | Maps to |
|---|---|---|---|---|
| `name` | string | **required** | Lowercase alphanumeric + `-`, ≤50 chars; becomes a k8s object name. `--name` overrides. | `name` |
| `pool` / `resourcePoolId` | string | `AIHC_POOL` | For managed pools this is the literal `aihc-serverless`, **not** the real pool ID. | query `resourcePoolId` |
| `queue` | string | `AIHC_QUEUE` | Self-managed pool → queue *name*; managed → queue *ID* (`aihcq-…`). Default here: `aihcq-xxxxxxxxxxxx`. | query `queueID` + body `queue` |
| `visibleScope` | int | `1` | `1` = visible to the whole queue, `0` = creator only. | `visibleScope` |
| `labels` | map or list | `{}` | `{"owner":"{{AIHC_OWNER}}"}` or `["k=v"]`. The platform adds its own `aijob.cce.baidubce.com/*` labels on top. | `labels` |
| `priority` | string | **`high`** | `low` \| `normal` \| `high`. This tool defaults to `high`; the AIHC API's own default is `normal`. Only editable after submit while still `Created` (`aihc-job priority <id> normal`). Note this queue is shared — 82 of 100 recent jobs run at `normal`, so blanket `high` only helps while others stay lower. | `priority` |

## Workload

| Key | Type | Default | Notes | Maps to |
|---|---|---|---|---|
| `framework` / `jobType` | string | `PyTorchJob` | `PyTorchJob` \| `TFJob` \| `MPIJob` \| `RayJob`. Aliases: `pytorch`, `torch`, `tf`, `tensorflow`, `mpi`, `ray`. | `jobType` |
| `command` | string | **required** | The launch command. Runs as the container entrypoint arg, so `bash -c '…'` for anything with pipes/`&&`. | `command` |
| `image` | string | **required** | **Tag mandatory** — a tagless reference is rejected locally. | `jobSpec.image` |
| `imageConfig` | map | — | `{"username":…,"password":…}`, only for a private registry. The VPC CCR registry needs neither. | `jobSpec.imageConfig` |
| `replicas` | int | `1` | Number of *nodes* (pods), not GPUs. Every job in this queue so far is `1`. | `jobSpec.replicas` |
| `gpu` | string/map | — | Shorthand for `resources`; see below. | `jobSpec.resources` |
| `resources` | list/map | `[]` | Explicit form, wins over `gpu` for the same resource name. | `jobSpec.resources` |
| `envs` / `env` | map or list | `{}` | `{"K":"v"}`, `["K=v"]`, or `[{"name":…,"value":…}]`. `--env K=v` *merges* into the template's map instead of replacing it. | `jobSpec.envs` |
| `enableRDMA` | bool | `false` | Adds `rdma/hca: 1`, `NCCL_IB_DISABLE=0` and 10GB shared memory. 79% of jobs in this queue enable it; needed for multi-node NCCL. Requires whole cards (no fractional GPU). | `jobSpec.enableRDMA` |
| `hostNetwork` | bool | `false` | Host networking; risks port conflicts when replicas share a node. Managed pools support container networking only. | `jobSpec.hostNetwork` |
| `roles` | map | — | TFJob/RayJob only: role name → spec, merged over the top-level spec. RayJob must include `Head` (`replicas` = 1, own `command`). Rejected for PyTorchJob. | `jobSpec` as a map |
| `preInitCommand` | string | — | RayJob only. | `preInitCommand` |
| `advancedSettings` | map | — | RayJob: `{"runtimeEnv": "<json>", "SubmitterBackoffLimit": 0}`. | `advancedSettings` |

### `gpu` shorthand

`"a800:8,cpu=122,memory=1964,sharedMemory=256"` — `,` separates entries, `:` or `=`
separates name from quantity. GPU aliases expand to Baidu descriptors:

| Alias | Descriptor | | Alias | Descriptor |
|---|---|---|---|---|
| `a800` | `baidu.com/a800_80g_cgpu` | | `h800` | `baidu.com/h800_80g_cgpu` |
| `a100` / `a100_80g` | `baidu.com/a100_80g_cgpu` | | `h20` | `baidu.com/h20_96g_cgpu` |
| `a100_40g` | `baidu.com/a100_40g_cgpu` | | `h20_141g` | `baidu.com/h20_141g_cgpu` |
| `a10` | `baidu.com/a10_24g_cgpu` | | `h20z` | `baidu.com/h20z_141g_cgpu` |
| `v100` / `v100_32g` | `baidu.com/v100_32g_cgpu` | | `l20` | `baidu.com/l20_cgpu` |
| `v100_16g` | `baidu.com/v100_16g_cgpu` | | `l40` | `baidu.com/l40_cgpu` |
| `xpu` | `baidu.com/xpu` | | `kunlun` | `kunlunxin.com/xpu` |

Non-GPU names pass through: `cpu` (cores), `memory` (GB), `sharedMemory` (GB, also
accepted as `shm`). A full descriptor or an unlisted resource name can be written
directly. Fractional GPU (vGPU queues only): `0.1`–`0.9` in `0.1` steps; the pool here
uses whole cards.

**Shapes actually used in this queue** — useful as a starting point:

| GPUs | cpu | memory | sharedMemory | Note |
|---|---|---|---|---|
| 1 | 4 | 16 | 16 | most common small job |
| 2 | 16 | 64 | 64 | |
| 4 | 32 | 128 | 128 | `examples/a800-4gpu.json` |
| 8 | 122 | 1964 | 256 | a whole A800 node; `examples/a800-8gpu.json`. `cpu=64,memory=256` also works if you prefer a smaller claim. |

Omitting cpu/memory entirely is legal (28 jobs do) — the queue applies defaults.

## Storage (`datasources`)

A list of mounts. Each entry needs `type` and `mountPath`. Flat option keys are lifted
into `options` automatically, and `instanceId` / `mountPoint` are accepted as aliases
for the `cfs*` ones. A string form works too:
`"type=cfs,mountPath=/share,readOnly=true"`.

| `type` | Required fields | Notes |
|---|---|---|
| `cfs` | `mountPath`, `options.cfsInstanceId`, `options.cfsMountPoint` | **This is `/share`:** write `{{AIHC_CFS_INSTANCE}}` / `{{AIHC_CFS_MOUNT}}` and keep the values in `.env` (currently instance `cfs-xxxxxxxx`, mount target `cfs-xxxxxxxx.lb-yyyyyyyy.cfs.bj.baidubce.com`). `CreateJob` rejects a cfs mount without `cfsInstanceId` — note that `DescribeJob` returns both fields *blank* on jobs that were created fine, so never infer the requirement from a read-back. `sourcePath` defaults to `/`. |
| `pfs` | `name` (PFS instance ID), `mountPath` | Faster than CFS for read-heavy training data. |
| `bos` | `sourcePath` (bucket/path), `mountPath` | Object storage; jobs in this account mount buckets under `/mnt/<name>`. |
| `hostpath` | `sourcePath`, `mountPath` | Node-local path. |
| `emptydir` | `mountPath` | With `medium: Memory` this is the `/dev/shm` idiom (see below). |
| `dataset` / `public_dataset` | `id`, `mountPath` | Platform datasets; `options.datasetVersion` defaults to newest. `aihc-job raw DescribeDatasets --method GET` lists the IDs available to your account. |
| `pfsl1`, `rapidfs` | varies | `options.pfsL1ClusterIp` / `pfsL1ClusterPort`. |

`options` keys: `readOnly`, `sizeLimit`, `medium`, `cfsInstanceId`, `cfsMountPoint`,
`datasetVersion`, `pfsL1ClusterIp`, `pfsL1ClusterPort`.

**Two ways to get shared memory** — both are in use in this queue; pick one:

```jsonc
"gpu": "...,sharedMemory=128"                                              // resource form (templates use this)
{ "type": "emptydir", "name": "devshm", "mountPath": "/dev/shm", "medium": "Memory" }   // emptydir form
```

## Reliability

| Key | Type | Default | Notes |
|---|---|---|---|
| `faultTolerance` | bool | `false` | **PyTorchJob only.** 6 of 100 recent jobs use it. Auto-restarts on hang/failure. |
| `faultToleranceArgs` | string | — | Space-joined flags, see below. Setting it implies `faultTolerance: true`. |
| `enableBccl` | bool | `false` | Baidu's collective-comms acceleration. Needs **≥2 replicas**, 8 cards/instance, RDMA on, A800/HPAS. Rejected locally for a 1-replica job. Nothing in this queue uses it (all jobs are single-node). |
| `retentionPeriod` | string | — | Keep the finished job for `30m` / `2h` / `1d`. Not supported for RayJob. |
| `alertConfig` | map | — | `{"instanceId":…,"alertItems":["jobFailed","jobHang",…],"notifyRuleId":"notify-…","for":"0m"}`. Items: `jobRunning`, `jobFT`, `nodeFT`, `jobFailed`, `jobSucceed`, `jobHang`. PyTorchJob supports all; TFJob/RayJob status-only; MPIJob unsupported. Needs a monitoring instance ID + notify rule ID from the console. |
| `tensorboard` / `tensorboardConfig` | map | — | `{"datasourceType":"pfs"\|"bos","datasourceName":…,"logPath":…,"disableAutoAddJobID":false}`. All three required when enabled. Not supported for RayJob. |

### `faultToleranceArgs` flags

```
--enable-replace=true                          # elastic agent replaces a failed worker
--enable-hang-detection=true                   # gates the three timeouts below
--hang-detection-log-timeout-minutes=15        # no log output for N minutes => hung
--hang-detection-startup-toleration-minutes=30 # grace window (init / data loading)
--hang-detection-stack-timeout-minutes=5       # logs flowing but stack frozen => "假活"
--max-num-of-unconditional-retry=2             # <= 3
--custom-log-patterns=<regex>                  # repeatable
```

Tune the startup toleration up if data loading is slow, or a long first epoch will be
misread as a hang.

## Env vars

Set anything you like via `envs`. The platform injects these — do not set them
yourself: `AIHC_JOB_NAME`, `NCCL_IB_DISABLE` (=0 with RDMA), `NCCL_DEBUG` (=INFO),
`LOG_COLLECTION`, `AIHC_TENSORBOARD_LOG_PATH`, and for PyTorch pods `MASTER_ADDR`,
`MASTER_PORT` (23456), `WORLD_SIZE`, `RANK`, `PET_NNODES`, `PET_MAX_RESTARTS`. Use
those in a multi-node `torchrun` line rather than hardcoding.

Most-used env vars in this queue: `CUDA_DEVICE_MAX_CONNECTIONS=1` (74 jobs),
`HF_ENDPOINT` (14), `MUJOCO_GL` (9), `WANDB_MODE` (5).

> Several jobs in this queue pass `BCE_ACCESS_KEY` / `MY_SK` / `FEISHU_APP_SECRET` as
> plain env values, which puts them in the job record for anyone with queue
> visibility. Prefer reading secrets from a file on `/share` with tight permissions,
> or set `visibleScope: 0`.

## Queue selection

`submit` verifies the target queue has room for `replicas x cards`. **Auto-selection is
on by default**: the configured queue is kept when it fits, otherwise the job moves to
the emptiest queue that does. `--no-auto-queue` fails instead of moving (use it when a
run must be reproducible); `--no-check-capacity` skips the check entirely.

`DescribeQueues` returns physical queues at the top level with the **submittable
Elastic queue nested under `children`** — `aihc-job -p <realPoolId> queues` shows the
tree with a `SUBMIT` column marking which ones jobs can target. Free capacity is
`capability - allocated` per accelerator descriptor.

Queues are typically partitioned by **hardware, not by team** (one queue per chip type),
and each is shared by many users (one A800 queue here held jobs from 5
different people). So any open queue with the right card type is a legitimate target.

## CLI overrides

Any of these beats the template; only flags you actually pass override. Slice-valued
flags replace the template's list wholesale, except `--env`, which merges.

```
--name --image --command --command-file --framework --replicas --gpu
--env K=v --label k=v --datasource "type=…,mountPath=…"
--priority --rdma / --no-rdma --bccl --host-network
--fault-tolerance --fault-tolerance-args --retention-period
-p/--pool -q/--queue
```

Behaviour flags (not part of the job): `--dry-run`, `--wait`, `--wait-running`,
`--follow`, `--wait-timeout`, `--poll-interval`, `--auto-queue`,
`--no-check-capacity`, `--queue-pool`, `--env-file`.

Flags may carry `{{VAR}}` too: `--command 'cd {{AIHC_WORKDIR}}/exp && bash run.sh'`.

## After it is running

Nothing in the template controls monitoring — load figures come from `DescribeJobMetrics`
at read time. `aihc-job watch <jobId>` is the live status + per-pod GPU dashboard,
`aihc-job metrics <jobId>` the one-shot reading (`--history` for every sample), and
`aihc-job list` shows each job's `gpuUtil` for free. Metric types are listed in
`jobs.METRIC_TYPES`; values are per pod, averaged over that pod's GPUs. See the README's
"Watch a job" section.

## Not settable here

`chunkSize` on log reads, batch stop, `codeSource`/code upload (no OpenAPI equivalent
— code has to come from `/share` or the image), dev-machine and inference-service
fields. `aihc-job raw <Action> --body '{…}'` reaches any action this tool has not
wrapped.
