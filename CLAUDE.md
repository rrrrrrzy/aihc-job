# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`aihc-job` — a job submission tool for **Baidu AIHC (百度百舸 / Baige)**, meant to be
driven by agents. The local machine has 4 GPUs; anything larger is submitted to AIHC
through this tool. It speaks the 百舸 OpenAPI v2 directly and deliberately does *not*
depend on `bce-python-sdk` or the official `aihc` binary — `requests` is the only
runtime dependency (PyYAML optional, for YAML templates).

Three docs, three audiences — keep them that way when editing:

| File | For | Contains |
|---|---|---|
| `README.md` | humans using the tool | install, `.env`, submit, watch, manage, handover |
| `README.agent.md` | agents/scripts driving it | exit codes, `--json` shapes, recipes, hard rules |
| `CLAUDE.md` (this) | anyone changing the code | architecture, invariants, testing approach |

`README.agent.md` documents a contract (exit codes, JSON shapes, offline guarantees).
Changing behaviour it describes means updating it in the same commit.

## Commands

```bash
pip install -e '.[dev]'                     # editable install + pytest + PyYAML
python -m pytest                            # full suite; no network needed
python -m pytest tests/test_auth.py -v      # one file
python -m pytest -k "signature or gpu"      # one test / subset
python -m aihc_job --help                   # CLI without installing (same as `aihc-job`)
python -m aihc_job render -f examples/pytorch-multi-node.json   # offline template check
python -m aihc_job submit -f job.json --dry-run                 # offline request check
```

There is no linter config beyond `[tool.ruff] line-length = 100` in `pyproject.toml`.

## Architecture

Four layers, each usable on its own; keep the boundaries when adding features.

```
cli.py      argparse surface, output formatting, exit codes
  └── jobs.py     JobManager: submit / wait / iter_logs / stop, pool+queue defaulting
        └── client.py   AihcClient: one action == one signed HTTP call, retries, errors
              └── auth.py     bce-auth-v1 signing (stdlib only)
models.py   template -> CreateJob body translation + local validation
config.py   credential/region/pool/queue resolution
```

- **`auth.py`** implements `bce-auth-v1` from scratch. The signature covers the
  percent-encoded query string, so `client.build_request` builds the URL itself and
  passes no `params=` to `requests` — otherwise `requests` re-encodes and the
  signature breaks. Only `host` and `x-bce-date` are signed.
- **`client.py`** is the only place that knows HTTP. Two header conventions coexist in
  the API: job actions need `X-API-Version: v2`, pool/queue actions need `version: v2`
  — `_is_job_action()` (any action containing `"Job"`) picks. Every attempt re-signs,
  because signatures are timestamped. 5xx/429 retry with jittered backoff; 4xx does
  not. All non-2xx become `ApiError` carrying the BCE `code`/`requestId`.
- **`models.py`** owns the template schema. `TEMPLATE_KEYS` is a closed set: an
  unrecognized key is an error, not silently dropped, because a typo'd
  `faultTolerence` would otherwise cost a multi-node run. Everything checkable
  offline is checked here (image tag present, BCCL needs ≥2 replicas, fault tolerance
  is PyTorchJob-only, …) so a bad template costs no API call.
- **`jobs.py`** is what agents and scripts should import. It owns log pagination
  (`nextMarker`), terminal-state definitions, rank-0 pod selection, and the
  monitoring layer (`metrics` / `metrics_snapshot` / `watch`, plus `METRIC_TYPES`).

### Invariants that matter

- **`queueID` is asymmetric**: self-managed (`common`) pools take the queue *name*;
  fully managed (`dedicatedV2`) pools take the queue *ID*, and their
  `resourcePoolId` is the literal `aihc-serverless`. Most confusing failures trace
  back to this.
- **Config merges per field**, it does not shadow whole sources: flags → exported env →
  **`.env`** → `$AIHC_CONFIG` → `./.aihc/config*` → `~/.aihc/config*`. `~/.aihc/config` is
  the official CLI's flat-YAML file and is read with a small built-in parser when PyYAML
  is absent, so an already-configured machine needs no setup.
- **`.env` at the repo root is the single per-user file** (`.env.example` is the committed
  template; `KNOWN_VARIABLES` lists the documented names). It sits *below* a real exported
  variable so a one-off `AIHC_QUEUE=… aihc-job submit` needs no edit, and *above* the
  config files so a checkout's settings win over a machine's. It is found from any working
  directory via `config.PACKAGE_ROOT`, which is why `tests/conftest.py` must blind both
  `$AIHC_ENV_FILE` and `PACKAGE_ROOT` — a filled-in `.env` would otherwise supply the very
  fields a test is asserting the absence of. A `--env-file`/`$AIHC_ENV_FILE` path that does
  not exist raises instead of falling back: silently using a different file would submit to
  the wrong queue with the wrong image.
- **Templates expand `{{VAR}}`, never `$VAR`.** `models.expand_variables` runs before
  validation, so `render`/`--dry-run` show real values. Braces are load-bearing: commands
  are shell, and `$RANK`/`${MASTER_ADDR}` must reach the *remote* shell untouched. An unset
  variable is a `TemplateError` naming the file to edit — an empty `{{AIHC_WORKDIR}}` would
  quietly `cd /` — and `{{VAR:-fallback}}` is the escape hatch. `_comment` keys are
  stripped before expansion so docs may mention placeholders freely.
- **Exit codes are API**: `0` ok, `1` error, `2` usage/template, `3` job ended in a
  non-success terminal state, `4` config, `5` wait timeout, `6` API error. Callers
  branch on these; do not renumber. `--json` writes exactly one JSON document to
  stdout with everything else on stderr.
- **`submit --dry-run` and `render` must never touch the network.** They are how a
  plan gets reviewed before GPUs are spent.
- **Capacity checking must never block a submit.** `resolve_queue` swallows
  `DescribeQueues` errors and skips silently when `config.queue_pool` is unset; only a
  definite "does not fit" raises. **`auto_queue` defaults to True** (user's call): the
  configured queue is kept when it fits, else the job moves to the emptiest that does.
  Consequence to keep in mind — the landing queue now depends on occupancy at submit
  time, so `dry_run` stays offline, reports the *configured* queue, and prints a warning
  that it may move; `--no-auto-queue` is the reproducible path.
- `config.queue_pool` (file key `queuepool`) is the **real** pool ID, needed because
  `DescribeQueues` rejects the `aihc-serverless` sentinel that job actions require.
- Any argparse flag whose `dest` collides with the subparser `dest` silently
  overwrites the subcommand — the subparser dest is `subcommand`, not `command`,
  because `submit --command` exists. The same trap applies to **global** flags: a
  subcommand `--timeout` used to land on the global connection `--timeout` and set it
  to `0`, which `requests` rejects outright (`wait --timeout` was broken by this).
  `wait`/`watch` therefore use `dest="wait_timeout"`/`"watch_timeout"`, and `_manager`
  passes `args.timeout or None` as a second guard.
- **Monitoring never blocks or crashes on a bad metric.** `metrics_snapshot` records a
  failing metric type under `errors` and returns the rest — RDMA metrics are absent on
  non-RDMA nodes, and losing the GPU numbers over that would be worse than useless.
  `watch` only samples while the job is in `ACTIVE_STATES`, so a queued job costs one
  `DescribeJob` per round.
- **`watch --json` is JSON Lines**, one document per refresh — the "exactly one JSON
  document" rule cannot hold for a stream (`logs --follow` already prints lines). Text
  mode redraws in place only when stdout is a TTY; `--no-clear` and pipes append.
- `watch` exits `3` on a non-success terminal state (same as `wait`), but `--once`
  always exits `0`: it is an observation, not a wait.
- GPU load figures are **per pod, averaged over that pod's GPUs** — the API has no
  per-device breakdown, so never present them as per-GPU. Sparklines scale percentages
  against a fixed 0–100 (autoscaling drew an idle job as a busy one).

## This environment

Everything that identifies a particular tenant — pool and queue IDs, the image, the CFS
instance behind `/share`, verified live runs, who else shares the queues — lives in
**`LOCAL.md`**, which is not committed (this repo is public). Read it if it is present;
it is the companion to the generic invariants above.

Per-user values come from **`.env`** at the repo root (mode 600, gitignored;
`.env.example` is the committed template). Never hard-code any of them: templates carry
`{{AIHC_IMAGE}}`, `{{AIHC_WORKDIR}}`, `{{AIHC_OWNER}}`, `{{AIHC_CFS_INSTANCE}}`,
`{{AIHC_CFS_MOUNT}}`, and `config show` masks anything whose name looks like a key.

When adding docs or examples, keep real IDs out of tracked files — a placeholder plus
"run `aihc-job queues` to find yours" beats a value that has to be scrubbed later.

## Testing approach

No network, no credentials, no mocking library. `tests/test_client_jobs.py` defines
`FakeSession`/`FakeResponse` (a queue of canned responses plus a record of the
requests that produced them); `tests/test_cli.py` imports them and monkeypatches
`cli.AihcClient` to inject the stub. Signing tests re-derive HMACs by hand rather
than pinning an opaque fixture, since Baidu publishes no signature test vector.

When adding an action, assert on the recorded request (URL, headers, body), not just
the parsed result — the header/verb split per action type is easy to get wrong.

## API reference

`docs/template-reference.md` is the user-facing list of every template key (type,
default, constraint, API mapping) plus the resource shapes real jobs in this queue use.
Keep it in sync with `models.TEMPLATE_KEYS` when adding a key.

`docs/aihc-api-notes.md` condenses the Baidu docs actually used to build this
(signing algorithm, `CreateJob` body, GPU descriptors, status values, per-action
parameter tables, and each source URL). Read it before guessing at a field name; the
upstream docs are Chinese web pages and change often. Anything not yet wrapped can be
reached with `aihc-job raw <Action> --body '{...}'` — prefer that over hand-rolling a
new HTTP path.

## Known gaps (candidates for the next iteration)

Code upload has no OpenAPI equivalent (only `aihc code upload` in the official CLI),
so code must come from a PFS/BOS mount or the image. Also unimplemented: dev
machines, inference services, datasets/models, `job exec`/WebTerminal, batch stop,
alert configuration (`alertConfig` passes through but has no flags), and a
local-vs-AIHC "same command, either target" launcher. Monitoring covers job status and
pod-level load; a fleet view (`watch` across every job in a queue) and stall detection
built on `watch` are the obvious next steps.
