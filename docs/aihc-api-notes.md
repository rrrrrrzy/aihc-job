# AIHC OpenAPI notes

Condensed from the 百度百舸 docs (read 2026-08-19) so future work does not have to
re-read the Chinese doc site. Doc URLs are given per section; the docs are updated
frequently, so re-check before relying on a detail that surprises you.

## Endpoints and request shape

Every operation is `HTTP(S)` to `/` on a regional host, selected by an `action`
query parameter. <https://cloud.baidu.com/doc/AIHC/s/amaz0nqs7>

| Region | Endpoint |
|---|---|
| 北京 | `aihc.bj.baidubce.com` |
| 广州 | `aihc.gz.baidubce.com` |
| 苏州 | `aihc.su.baidubce.com` |
| 保定 | `aihc.bd.baidubce.com` |
| 武汉 | `aihc.fwh.baidubce.com` |
| 阳泉 | `aihc.yq.baidubce.com` |

Two header conventions coexist: **job** actions want `X-API-Version: v2`, while
resource-pool/queue actions want `version: v2`. `client.py` sends whichever matches
the action name (`_is_job_action`).

Job actions are `POST` with a JSON body; pool/queue reads are documented as `GET`
with everything in the query string. `resourcePoolId` and `queueID` always travel in
the query string, never the body.

## Signing (`bce-auth-v1`) — <https://cloud.baidu.com/doc/AIHC/s/4maz04s1c>

```
Authorization: bce-auth-v1/{accessKeyId}/{timestamp}/{expireSeconds}/{signedHeaders}/{signature}

signingKey     = HMAC-SHA256-HEX(sk, "bce-auth-v1/{ak}/{timestamp}/{expire}")
canonicalReq   = METHOD \n uriEncode(path, safe="-_.~/") \n sortedQuery \n canonicalHeaders
signature      = HMAC-SHA256-HEX(signingKey, canonicalReq)
```

- `timestamp` is ISO-8601 UTC (`2026-08-19T03:04:05Z`) and **must** equal the
  `x-bce-date` header.
- Query pairs: both key and value encoded with `safe="-_.~"`, sorted, `&`-joined;
  the `authorization` key is excluded.
- Canonical headers: `uriEncode(lower(name)) + ":" + uriEncode(trim(value))`, sorted,
  `\n`-joined. AIHC signs only `host` and `x-bce-date` (plus any `x-bce-*`).
- Because the signature covers the encoded query string, the client builds the URL
  itself instead of letting `requests` re-encode `params`.

## `CreateJob` — <https://cloud.baidu.com/doc/AIHC/s/Hmayv96tj>

`POST ?action=CreateJob&resourcePoolId=<id>&queueID=<queue>`

- `resourcePoolId`: pool ID (e.g. `cce-1uji3ib5`) for self-managed pools,
  `aihc-serverless` for fully managed ones.
- `queueID`: queue **name** for self-managed pools, queue **ID** for managed ones.
  This asymmetry is the single most common source of confusion.
- Body: `name`, `jobType` (`PyTorchJob` default, `TFJob`, `MPIJob`, `RayJob`),
  `command`, `jobSpec`, `labels`, `priority` (`low|normal|high`), `datasources`,
  `enableBccl`, `faultTolerance`, `faultToleranceArgs`, `tensorboardConfig`,
  `alertConfig`, `retentionPeriod`, `advancedSettings`, `visibleScope`.
- `jobSpec` is a single object for PyTorchJob, but a **map keyed by role** for TFJob
  (`Chief`/`Worker`/`PS`/`Evaluator`) and RayJob (must include `Head`, whose
  `replicas` is fixed at 1 and which needs its own `command`).
- Response: `requestId`, `jobId`, `jobName`.

### Constraints worth validating locally

| Feature | Rule |
|---|---|
| `faultTolerance` / `faultToleranceArgs` | PyTorchJob only; the two args forms are mutually exclusive with `faultToleranceConfig` |
| `enableBccl` | needs ≥ 2 replicas, 8 cards/instance, RDMA on, A800/HPAS |
| `tensorboardConfig` | not supported for RayJob; needs `datasourceType` (`pfs`/`bos`), `datasourceName`, `logPath` |
| `retentionPeriod` | `1m`/`1h`/`1d` style; not for RayJob |
| `alertConfig` | PyTorchJob all alerts; TFJob/RayJob status alerts only; MPIJob unsupported |
| `visibleScope` | `1` = visible in queue (default), `0` = creator only |
| image | tag must be explicit |

### `faultToleranceArgs` flags

Space-joined `--flag=value` string: `--enable-replace`, `--enable-hang-detection`
(gates the next three), `--hang-detection-log-timeout-minutes`,
`--hang-detection-startup-toleration-minutes`,
`--hang-detection-stack-timeout-minutes`,
`--max-num-of-unconditional-retry` (≤ 3), `--custom-log-patterns` (repeatable).

## Data structures — <https://cloud.baidu.com/doc/AIHC/s/Imb0gupts>

`JobSpec`: `image` (req), `imageConfig{username,password}`, `replicas` (req),
`resources[{name,quantity}]`, `envs[{name,value}]`, `enableRDMA`, `hostNetwork`.

Auto-injected env vars: `AIHC_JOB_NAME`, `NCCL_IB_DISABLE=0` (when RDMA),
`NCCL_DEBUG=INFO`. PyTorch pods also get `MASTER_ADDR`, `MASTER_PORT` (23456),
`WORLD_SIZE`, `RANK`, `PET_NNODES`, `PET_MAX_RESTARTS`.

GPU resource descriptors (mirrored in `models.GPU_DESCRIPTORS`):

| Chip | `resources[].name` |
|---|---|
| A800-SXM4-80GB | `baidu.com/a800_80g_cgpu` |
| A100-SXM4-40GB / 80GB | `baidu.com/a100_40g_cgpu` / `baidu.com/a100_80g_cgpu` |
| A10 | `baidu.com/a10_24g_cgpu` |
| H800 | `baidu.com/h800_80g_cgpu` |
| H20 / H20Z / H20-3e | `baidu.com/h20_96g_cgpu` / `baidu.com/h20z_141g_cgpu` / `baidu.com/h20_141g_cgpu` |
| V100 16G / 32G | `baidu.com/v100_16g_cgpu` / `baidu.com/v100_32g_cgpu` |
| L20 / L40 | `baidu.com/l20_cgpu` / `baidu.com/l40_cgpu` |
| Kunlun XPU | `baidu.com/xpu`, `kunlunxin.com/xpu` |

Non-accelerator names: `cpu` (cores), `memory` (GB), `sharedMemory` (GB).
Virtualized GPU quantities: `10000` = 1 card, `1000` = 0.1 card; below one card the
step is 0.1, at or above one card only whole cards.

`DataSource`: `type` (`pfs`, `pfsl1`, `hostpath`, `bos`, `cfs`, `rapidfs`, `dataset`,
`public_dataset`), `name` (PFS instance ID for `pfs`), `sourcePath`, `mountPath`,
`id` (required for `dataset`/`public_dataset`), `options{readOnly, sizeLimit, medium,
cfsInstanceId, cfsMountPoint, datasetVersion, pfsL1ClusterIp, pfsL1ClusterPort}`.

CFS needs `options.cfsInstanceId` plus `options.cfsMountPoint` (the mount-target
domain). Both can be read off an existing client mount — `mount | grep <path>` prints
`<instanceId>.<mountTarget>:/ on <path> type nfs4`. CFS is VPC-scoped, so the pool's
nodes must be able to reach that mount target. `models._lift_datasource_options`
accepts these flat (and as `instanceId`/`mountPoint`) and nests them.

Job statuses: `Created` (queued; priority still editable) → `Starting` → `Running` →
`Succeeded` / `Failed` / `Stopping` → `Stopped`; plus `Abnormal` (≥ 1 failed
instance) and `Restarting` (fault tolerance or preemption). `jobTimeLine` entries use
`conditionType` values including `FaultTolerantStart` and `ManualTermination`.

Pod statuses: `Pending`, `Starting`, `Running`, `Failed`, `Succeed`, `Unknown`;
`replicaType` is `master`/`worker` for PyTorch.

## Other job actions

| Action | Doc | Notes |
|---|---|---|
| `DescribeJobs` | <https://cloud.baidu.com/doc/AIHC/s/xmayvctia> | body: `queue`, `status`, `keywordType` (`name`/`queueName`), `keyword`, `orderBy` (`createdAt`/`finishedAt`), `order`, `pageNumber`, `pageSize` |
| `DescribeJob` | <https://cloud.baidu.com/doc/AIHC/s/Kmayvejf0> | body: `jobId`, `needDetail` (adds `pods` + `historyPods`) |
| `DescribeJobLogs` | <https://cloud.baidu.com/doc/AIHC/s/Hmayvkw26> | body: `jobId`, `podName` (both required), `keywords`, `startTime`, `endTime`, `maxLines`, `chunkSize`, `marker`; page with the returned `nextMarker` until empty |
| `StopJob` | <https://cloud.baidu.com/doc/AIHC/s/0mayvnkik> | PyTorchJob + MPIJob only |
| `DeleteJob` | <https://cloud.baidu.com/doc/AIHC/s/rmayvfzxj> | |
| `ModifyJob` | <https://cloud.baidu.com/doc/AIHC/s/Smayvhq0w> | changes `priority`; only meaningful while `Created` |
| `DescribeJobEvents` / `DescribeJobPodEvents` | <https://cloud.baidu.com/doc/AIHC/s/fmayvjaeq> | `startTime`/`endTime` are epoch seconds |
| `DescribeJobNodes` | <https://cloud.baidu.com/doc/AIHC/s/2mayvq994> | |
| `DescribeJobMetrics` | <https://cloud.baidu.com/doc/AIHC/s/4mayvot8u> | see below; `queueID` **is** required |
| `DescribeJobWebterminal` | <https://cloud.baidu.com/doc/AIHC/s/9mayvri1t> | |
| `StopJobs` (batch) | <https://cloud.baidu.com/doc/AIHC/s/Kmimk5f2e> | returns `successList`/`failedList` |

## `DescribeJobMetrics` — <https://cloud.baidu.com/doc/AIHC/s/4mayvot8u>

`POST ?action=DescribeJobMetrics&resourcePoolId=<id>&queueID=<queue>`, body
`jobId`, `metricType`, and optionally `startTime`, `endTime`, `timeStep`, `rateInterval`.
Response: `metrics[{podName, metrics[{time, value}]}]`.

Verified live against a running 8-GPU job (2026-08-19):

- **`queueID` is required**, contrary to what the parameter table implies: omitting it on a
  serverless pool gives `400 InvalidParam: queueID must be set in serverless pool`, and
  passing any queue other than the job's own gives `403 AccessDenied: no permission to get
  job`. `DescribeJob`, by contrast, answers regardless and reports the real queue — which
  is how `jobs.job_queue` resolves it.
- **`startTime`/`endTime`/`timeStep` must be JSON *strings*.** Numbers fail with
  `400 MalformedJSON: cannot unmarshal number into Go struct field
  DescribeMetricsRequestV2.startTime of type string`.
- `timeStep` defaults to **5 minutes**, far too coarse to watch a job; `15` works, and the
  response includes a sample at exactly `endTime`, lagging real time by ~2 s.
- Values are **per pod, already averaged over that pod's GPUs** — there is no per-device
  breakdown. `value` is a string; `GpuUsage`/`GpuMemoryUsage`/`CpuUsage`/`MemoryUsage` are
  percentages 0–100, `GpuPowerUsage` watts, `GpuTemperature` °C, `MemoryAllocation` bytes,
  the `*DataRate` family bytes/s.
- Metrics stay queryable after the job reaches a terminal state.

`metricType` values (all confirmed to answer, mirrored in `jobs.METRIC_TYPES`): `GpuUsage`,
`GpuMemoryUsage`, `GpuTemperature`, `GpuPowerUsage`, `GpuPipeTensorUsage`, `CpuUsage`,
`CpuTime`, `MemoryUsage`, `MemoryAllocation`, `DiskReadRate`, `DiskWriteRate`,
`RDMASendDataRate`, `RDMARecvDataRate`, `RDMASendPacketsRate`, `RDMARecvPacketsRate`,
`RDMASendErrorRate`, `RDMARecvErrorRate`, `RDMAHealth`, `PCIESendDataRate`,
`PCIERecvDataRate`, `NVLinkSendDataRate`, `NVLinkRecvDataRate`.

Cheaper than any of it: **`DescribeJob` and `DescribeJobs` already carry
`gpuUtilizationPercent`, `gpuMemoryUtilizationPercent` and `runTimeNanoseconds`** per job
(undocumented in the response tables). Job-wide averages rather than per-pod, but free —
`list` shows them without a metrics call, and `runTimeNanoseconds` matched the `Running`
timeline entry to within a second.

`DescribeResourcePools` (<https://cloud.baidu.com/doc/AIHC/s/dmgrw0t8l>) takes
`resourcePoolType` = `common` (self-managed) or `dedicatedV2` (fully managed).
`DescribeQueues` (<https://cloud.baidu.com/doc/AIHC/s/vmfcjld9v>) takes
`resourcePoolId`, `keywordType`, `keyword`, `pageNumber`, `pageSize`.

## Official CLI, for comparison

`aihc` v1.6.2 (<https://cloud.baidu.com/doc/AIHC/s/em7x6wb9v>,
<https://cloud.baidu.com/doc/AIHC/s/Tm7x702fo>) covers the same job APIs plus things
absent from the OpenAPI:

- `aihc code upload -f <folder>` — uploads a local code directory to the job's
  workspace, then `job create --code-dir/--code-url/--local-code` mounts it. There is
  no documented OpenAPI equivalent, so this tool cannot ship code; mount it from
  PFS/BOS or bake it into the image.
- `aihc job exec` / `job export`, dev machines, inference services.
- Config lives in `~/.aihc/config` as flat YAML (`region`, `credentials.accesskey`,
  `credentials.secretkey`, `defaultpool`, `defaultqueue`) — `config.py` reads it.
