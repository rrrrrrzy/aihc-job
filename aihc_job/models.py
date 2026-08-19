"""Job template -> ``CreateJob`` request body.

The template is a friendlier, flatter view of the OpenAPI ``CreateJob`` body:
GPUs can be written as ``a800:8`` instead of
``{"name": "baidu.com/a800_80g_cgpu", "quantity": 8}``, env vars and labels are
plain mappings, and multi-role frameworks (TFJob/RayJob) use a ``roles`` map.

:func:`build_create_job_body` performs the translation and validates enough up
front that obvious mistakes fail locally instead of after a round trip.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import TemplateError

# Short alias -> Baidu resource descriptor (see the 数据结构 doc's GPU table).
GPU_DESCRIPTORS: dict[str, str] = {
    "a800": "baidu.com/a800_80g_cgpu",
    "a800_80g": "baidu.com/a800_80g_cgpu",
    "a100": "baidu.com/a100_80g_cgpu",
    "a100_80g": "baidu.com/a100_80g_cgpu",
    "a100_40g": "baidu.com/a100_40g_cgpu",
    "a10": "baidu.com/a10_24g_cgpu",
    "h800": "baidu.com/h800_80g_cgpu",
    "h20": "baidu.com/h20_96g_cgpu",
    "h20_96g": "baidu.com/h20_96g_cgpu",
    "h20_141g": "baidu.com/h20_141g_cgpu",
    "h20z": "baidu.com/h20z_141g_cgpu",
    "v100": "baidu.com/v100_32g_cgpu",
    "v100_32g": "baidu.com/v100_32g_cgpu",
    "v100_16g": "baidu.com/v100_16g_cgpu",
    "l20": "baidu.com/l20_cgpu",
    "l40": "baidu.com/l40_cgpu",
    # Present in this account's queues but absent from the published descriptor table;
    # taken from DescribeQueues' acceleratorDescription field.
    "4090": "baidu.com/rtx_4090_cgpu",
    "rtx4090": "baidu.com/rtx_4090_cgpu",
    "3090": "baidu.com/rtx_3090_cgpu",
    "rtx3090": "baidu.com/rtx_3090_cgpu",
    "b200": "baidu.com/b20z_180g_cgpu",
    "b20z": "baidu.com/b20z_180g_cgpu",
    "xpu": "baidu.com/xpu",
    "kunlun": "kunlunxin.com/xpu",
}

# Names that are passed through untouched (non-accelerator resources).
PLAIN_RESOURCES = {"cpu", "memory", "sharedMemory"}

JOB_TYPES = ("PyTorchJob", "TFJob", "MPIJob", "RayJob")
PRIORITIES = ("low", "normal", "high")
# The AIHC API defaults to "normal"; this tool defaults to "high" by project choice.
DEFAULT_PRIORITY = "high"
DATASOURCE_TYPES = (
    "pfs",
    "pfsl1",
    "hostpath",
    "bos",
    "cfs",
    "rapidfs",
    "dataset",
    "public_dataset",
    "emptydir",
)

# Keys the API expects inside DataSource.options rather than at the top level.
_DATASOURCE_OPTION_KEYS = {
    "readOnly",
    "sizeLimit",
    "medium",
    "cfsInstanceId",
    "cfsMountPoint",
    "datasetVersion",
    "pfsL1ClusterIp",
    "pfsL1ClusterPort",
}
# Friendlier spellings accepted in templates and --datasource strings.
_DATASOURCE_OPTION_ALIASES = {
    "instanceId": "cfsInstanceId",
    "instanceID": "cfsInstanceId",
    "mountPoint": "cfsMountPoint",
    "readonly": "readOnly",
}

# Keys accepted at the top level of a template. Anything else is a typo, and
# silently dropping it would be worse than failing.
TEMPLATE_KEYS = {
    "name",
    "pool",
    "resourcePoolId",
    "queue",
    "jobType",
    "framework",
    "command",
    "image",
    "imageConfig",
    "replicas",
    "gpu",
    "resources",
    "envs",
    "env",
    "enableRDMA",
    "hostNetwork",
    "roles",
    "labels",
    "priority",
    "datasources",
    "dataSources",
    "enableBccl",
    "faultTolerance",
    "faultToleranceArgs",
    "tensorboard",
    "tensorboardConfig",
    "alertConfig",
    "advancedSettings",
    "retentionPeriod",
    "visibleScope",
    "preInitCommand",
}

_JOB_SPEC_KEYS = {
    "image",
    "imageConfig",
    "replicas",
    "resources",
    "gpu",
    "envs",
    "env",
    "enableRDMA",
    "hostNetwork",
    "command",
}

_FRAMEWORK_ALIASES = {
    "pytorch": "PyTorchJob",
    "pytorchjob": "PyTorchJob",
    "torch": "PyTorchJob",
    "tensorflow": "TFJob",
    "tf": "TFJob",
    "tfjob": "TFJob",
    "mpi": "MPIJob",
    "mpijob": "MPIJob",
    "ray": "RayJob",
    "rayjob": "RayJob",
}

_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# `{{VAR}}` / `{{VAR:-fallback}}`. Braces rather than the usual `${VAR}` because template
# commands are shell: `$RANK` and `${MASTER_ADDR}` belong to the *remote* shell, and an
# expander that swallowed those would break every multi-node launch command.
_VARIABLE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::-(.*?))?\s*\}\}")


def expand_variables(obj: Any, variables: Mapping[str, str] | None = None) -> Any:
    """Substitute ``{{VAR}}`` placeholders through a template's strings.

    ``variables`` defaults to the ``.env`` file plus the real environment (see
    :func:`config.template_variables`) and is only loaded if a placeholder is actually
    present, so templates without one stay filesystem-free.

    An unset variable with no ``:-fallback`` is an error rather than an empty string: a
    blank ``{{AIHC_WORKDIR}}`` would silently submit a job that `cd`s to ``/``.
    """
    source: dict[str, Mapping[str, str]] = {}

    def values() -> Mapping[str, str]:
        if variables is not None:
            return variables
        if "cached" not in source:
            from .config import template_variables

            source["cached"] = template_variables()
        return source["cached"]

    def replace(match: re.Match[str]) -> str:
        name, fallback = match.group(1), match.group(2)
        value = values().get(name) or ""
        if value:
            return value
        if fallback is not None:
            return fallback
        where = "set it in the repo's .env file"
        if variables is None:
            from .config import find_env_file
            from .errors import ConfigError

            try:
                env_file = find_env_file()
            except ConfigError:  # a bogus $AIHC_ENV_FILE: report the missing variable
                env_file = None
            if env_file:
                where = f"set it in {env_file}"
        raise TemplateError(
            f"template uses {{{{{name}}}}} but {name} is not set; {where} "
            f"(see .env.example), export it, or write {{{{{name}:-fallback}}}}"
        )

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return _VARIABLE_RE.sub(replace, value)
        if isinstance(value, Mapping):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(item) for item in value]
        return value

    return walk(obj)


def load_template(path: str | Path) -> dict[str, Any]:
    """Load a JSON or YAML job template."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise TemplateError(f"template not found: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise TemplateError(
                f"{p} is YAML but PyYAML is not installed; `pip install pyyaml` "
                "or convert the template to JSON"
            ) from exc
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TemplateError(f"{p}: invalid JSON: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TemplateError(f"{p}: expected a mapping at the top level")
    return data


def merge_templates(*templates: Mapping[str, Any] | None) -> dict[str, Any]:
    """Shallow-merge templates left to right; later values win.

    Shallow is deliberate: ``resources``/``envs``/``labels`` given later replace
    the earlier list wholesale, matching the official CLI's override rule for
    slice flags.
    """
    merged: dict[str, Any] = {}
    for template in templates:
        if not template:
            continue
        for key, value in template.items():
            if value is not None:
                merged[key] = value
    return merged


def parse_gpu(spec: str | Mapping[str, Any] | Iterable[Any]) -> list[dict[str, Any]]:
    """Turn ``"a800:8"`` / ``"a800:8,cpu=32,memory=256"`` into resource entries.

    Accepted separators are ``:`` and ``=``; ``,`` splits entries. A bare number
    (``"8"``) is rejected because the descriptor cannot be guessed.
    """
    if isinstance(spec, Mapping):
        return [{"name": _resource_name(k), "quantity": _quantity(v)} for k, v in spec.items()]
    if not isinstance(spec, str):
        entries: list[dict[str, Any]] = []
        for item in spec:  # already-normalized list of resource dicts
            entries.extend(parse_gpu(item) if isinstance(item, (str, Mapping)) else [])
        return entries

    out: list[dict[str, Any]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, sep, value = chunk.replace("=", ":").partition(":")
        if not sep:
            raise TemplateError(
                f"cannot parse gpu spec {chunk!r}; use '<type>:<count>' such as 'a800:8' "
                f"(known types: {', '.join(sorted(GPU_DESCRIPTORS))})"
            )
        out.append({"name": _resource_name(key.strip()), "quantity": _quantity(value.strip())})
    return out


def _resource_name(alias: str) -> str:
    if alias in PLAIN_RESOURCES or "/" in alias:
        return alias  # already a descriptor, or cpu/memory/sharedMemory
    lowered = alias.strip().lower()
    if lowered in ("shm", "sharedmemory", "shared_memory"):
        return "sharedMemory"
    if lowered in PLAIN_RESOURCES:
        return lowered
    if lowered in GPU_DESCRIPTORS:
        return GPU_DESCRIPTORS[lowered]
    raise TemplateError(
        f"unknown resource/GPU type {alias!r}; pass a full descriptor like "
        f"'baidu.com/a800_80g_cgpu', or one of: {', '.join(sorted(GPU_DESCRIPTORS))}"
    )


def _quantity(value: Any) -> int | float:
    if isinstance(value, bool):
        raise TemplateError(f"invalid resource quantity: {value!r}")
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
        return int(text) if re.fullmatch(r"-?\d+", text) else float(text)
    except ValueError as exc:
        raise TemplateError(f"invalid resource quantity: {value!r}") from exc


def _normalize_resources(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Merge the ``gpu`` shorthand with any explicit ``resources`` list."""
    resources: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(name: str, quantity: Any) -> None:
        if name in seen:
            for entry in resources:
                if entry["name"] == name:
                    entry["quantity"] = _quantity(quantity)
            return
        seen.add(name)
        resources.append({"name": name, "quantity": _quantity(quantity)})

    if template.get("gpu"):
        for entry in parse_gpu(template["gpu"]):
            add(entry["name"], entry["quantity"])

    explicit = template.get("resources") or []
    if isinstance(explicit, Mapping):
        explicit = [{"name": k, "quantity": v} for k, v in explicit.items()]
    for entry in explicit:
        if isinstance(entry, str):
            for parsed in parse_gpu(entry):
                add(parsed["name"], parsed["quantity"])
            continue
        if not isinstance(entry, Mapping) or "name" not in entry:
            raise TemplateError(f"resources entry must have a 'name': {entry!r}")
        add(_resource_name(str(entry["name"])), entry.get("quantity", 1))
    return resources


def _normalize_envs(template: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = template.get("envs")
    if raw is None:
        raw = template.get("env")
    if not raw:
        return []
    if isinstance(raw, Mapping):
        return [{"name": str(k), "value": str(v)} for k, v in raw.items()]
    envs: list[dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            if "name" not in entry:
                raise TemplateError(f"env entry must have a 'name': {entry!r}")
            envs.append({"name": str(entry["name"]), "value": str(entry.get("value", ""))})
        elif isinstance(entry, str) and "=" in entry:
            key, _, value = entry.partition("=")
            envs.append({"name": key.strip(), "value": value})
        else:
            raise TemplateError(f"cannot parse env entry {entry!r}; use 'KEY=value'")
    return envs


def _normalize_labels(raw: Any) -> list[dict[str, str]]:
    if not raw:
        return []
    if isinstance(raw, Mapping):
        return [{"key": str(k), "value": str(v)} for k, v in raw.items()]
    labels: list[dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            if "key" not in entry:
                raise TemplateError(f"label entry must have a 'key': {entry!r}")
            labels.append({"key": str(entry["key"]), "value": str(entry.get("value", ""))})
        elif isinstance(entry, str) and "=" in entry:
            key, _, value = entry.partition("=")
            labels.append({"key": key.strip(), "value": value})
        else:
            raise TemplateError(f"cannot parse label entry {entry!r}; use 'key=value'")
    return labels


def _normalize_datasources(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, Mapping):
        raw = [raw]
    sources: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, str):
            entry = _parse_datasource_string(entry)
        if not isinstance(entry, Mapping):
            raise TemplateError(f"datasource entry must be a mapping: {entry!r}")
        ds = _lift_datasource_options({k: v for k, v in entry.items() if v not in (None, "")})
        ds_type = str(ds.get("type", ""))
        if not ds_type:
            raise TemplateError(f"datasource entry needs a 'type': {entry!r}")
        if ds_type not in DATASOURCE_TYPES:
            raise TemplateError(
                f"unsupported datasource type {ds_type!r}; expected one of "
                + ", ".join(DATASOURCE_TYPES)
            )
        if not ds.get("mountPath"):
            raise TemplateError(f"datasource {ds_type!r} needs a 'mountPath'")
        if ds_type in ("dataset", "public_dataset") and not ds.get("id"):
            raise TemplateError(f"datasource type {ds_type!r} requires 'id' (the dataset ID)")
        if ds_type == "pfs" and not ds.get("name"):
            raise TemplateError("datasource type 'pfs' requires 'name' (the PFS instance ID)")
        if ds_type == "cfs":
            # CreateJob rejects a cfs mount without cfsInstanceId ("cfs datasource
            # cfsInstanceId is required"), even though DescribeJob returns the field
            # blank afterwards -- do not infer this requirement from a job read-back.
            options = ds.get("options") or {}
            if not options.get("cfsInstanceId"):
                raise TemplateError(
                    "datasource type 'cfs' requires options.cfsInstanceId (and normally "
                    "options.cfsMountPoint). Read both off a machine that already mounts the "
                    "share: `mount | grep <path>` prints "
                    "'<instanceId>.<mountTarget>:/ on <path> type nfs4'."
                )
            ds.setdefault("name", str(options["cfsInstanceId"]))
            ds.setdefault("sourcePath", "/")
        sources.append(ds)
    return sources


def _lift_datasource_options(ds: dict[str, Any]) -> dict[str, Any]:
    """Move flat ``options`` keys (and their aliases) into the nested ``options`` map."""
    out: dict[str, Any] = {}
    options: dict[str, Any] = dict(ds.get("options") or {})
    for key, value in ds.items():
        if key == "options":
            continue
        canonical = _DATASOURCE_OPTION_ALIASES.get(key, key)
        if canonical in _DATASOURCE_OPTION_KEYS:
            options[canonical] = value
        else:
            out[canonical] = value
    for key in list(options):
        canonical = _DATASOURCE_OPTION_ALIASES.get(key, key)
        if canonical != key:
            options[canonical] = options.pop(key)
    if options:
        out["options"] = options
    return out


def _parse_datasource_string(text: str) -> dict[str, Any]:
    """Parse ``type=pfs,name=pfs-xxx,mountPath=/mnt/cluster`` (CLI shorthand).

    Keys belonging to ``options`` may be written flat -- ``readOnly=true`` and
    ``cfsInstanceId=cfs-xxx`` end up nested where the API expects them.
    """
    ds: dict[str, Any] = {}
    for chunk in text.split(","):
        if not chunk.strip():
            continue
        key, sep, value = chunk.partition("=")
        if not sep:
            raise TemplateError(f"cannot parse datasource {text!r}; expected key=value pairs")
        key = _DATASOURCE_OPTION_ALIASES.get(key.strip(), key.strip())
        value = value.strip()
        if key in _DATASOURCE_OPTION_KEYS:
            options = ds.setdefault("options", {})
            options[key] = value.lower() in ("1", "true", "yes") if key == "readOnly" else value
        else:
            ds[key] = value
    return ds


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _build_job_spec(spec: Mapping[str, Any], *, role: str = "") -> dict[str, Any]:
    where = f"role {role!r}: " if role else ""
    image = spec.get("image")
    if not image:
        raise TemplateError(f"{where}'image' is required (include the tag)")
    if ":" not in str(image).rsplit("/", 1)[-1]:
        raise TemplateError(f"{where}image {image!r} has no tag; AIHC requires an explicit tag")

    replicas = spec.get("replicas", 1)
    try:
        replicas = int(replicas)
    except (TypeError, ValueError) as exc:
        raise TemplateError(f"{where}'replicas' must be an integer, got {replicas!r}") from exc
    if replicas < 1:
        raise TemplateError(f"{where}'replicas' must be >= 1")

    job_spec: dict[str, Any] = {
        "image": str(image),
        "replicas": replicas,
        "resources": _normalize_resources(spec),
        "envs": _normalize_envs(spec),
        "enableRDMA": _as_bool(spec.get("enableRDMA", False)),
        "hostNetwork": _as_bool(spec.get("hostNetwork", False)),
    }
    image_config = spec.get("imageConfig")
    if image_config:
        if not isinstance(image_config, Mapping):
            raise TemplateError(f"{where}'imageConfig' must be a mapping with username/password")
        job_spec["imageConfig"] = dict(image_config)
    if role and spec.get("command"):  # per-role command (RayJob Head/Worker)
        job_spec["command"] = str(spec["command"])
    return job_spec


def _build_tensorboard(raw: Any) -> dict[str, Any] | None:
    if raw in (None, False, {}):
        return None
    if raw is True:
        raise TemplateError("'tensorboard' needs datasourceType/datasourceName/logPath, not just true")
    if not isinstance(raw, Mapping):
        raise TemplateError("'tensorboard' must be a mapping")
    config = {k: v for k, v in raw.items() if v is not None}
    config.setdefault("enable", True)
    if config.get("enable"):
        for required in ("datasourceType", "datasourceName", "logPath"):
            if not config.get(required):
                raise TemplateError(f"tensorboard config requires {required!r} when enabled")
        if config["datasourceType"] not in ("pfs", "bos"):
            raise TemplateError("tensorboard datasourceType must be 'pfs' or 'bos'")
    return config


def resolve_job_type(template: Mapping[str, Any]) -> str:
    raw = str(template.get("jobType") or template.get("framework") or "PyTorchJob").strip()
    resolved = _FRAMEWORK_ALIASES.get(raw.lower(), raw)
    if resolved not in JOB_TYPES:
        raise TemplateError(f"unknown jobType {raw!r}; expected one of {', '.join(JOB_TYPES)}")
    return resolved


def validate_job_name(name: str) -> str:
    if not name:
        raise TemplateError("'name' is required")
    if not _NAME_RE.match(name):
        raise TemplateError(
            f"job name {name!r} must be lowercase alphanumeric with '-' separators "
            "(it becomes a Kubernetes object name)"
        )
    if len(name) > 50:
        raise TemplateError(f"job name {name!r} is longer than 50 characters")
    return name


def build_create_job_body(
    template: Mapping[str, Any], *, variables: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Translate a template into the ``CreateJob`` request body.

    ``{{VAR}}`` placeholders are expanded first (from ``variables``, else ``.env`` plus the
    environment), so validation and ``--dry-run`` show the values that would be sent.

    Raises :class:`TemplateError` for anything checkable locally so a bad
    template costs no API call.
    """
    # Keys starting with '_' are comments -- JSON has no other way to carry one. Dropped
    # before expansion so a comment may mention a placeholder without having to set it.
    template = {k: v for k, v in template.items() if not str(k).startswith("_")}
    template = expand_variables(template, variables)
    unknown = set(template) - TEMPLATE_KEYS
    if unknown:
        raise TemplateError(
            "unknown template key(s): "
            + ", ".join(sorted(unknown))
            + f". Supported keys: {', '.join(sorted(TEMPLATE_KEYS))}"
        )

    job_type = resolve_job_type(template)
    name = validate_job_name(str(template.get("name") or ""))
    command = str(template.get("command") or "").strip()
    roles = template.get("roles")

    if roles:
        if not isinstance(roles, Mapping):
            raise TemplateError("'roles' must be a mapping of role name -> spec")
        if job_type == "PyTorchJob":
            raise TemplateError("'roles' applies to TFJob/RayJob; PyTorchJob uses a single jobSpec")
        if job_type == "RayJob" and "Head" not in roles:
            raise TemplateError("RayJob requires a 'Head' role in 'roles'")
        shared = {k: v for k, v in template.items() if k in _JOB_SPEC_KEYS}
        job_spec: Any = {}
        for role, role_spec in roles.items():
            if not isinstance(role_spec, Mapping):
                raise TemplateError(f"role {role!r} must be a mapping")
            merged = {**shared, **role_spec}
            if job_type == "RayJob" and role == "Head":
                if int(merged.get("replicas", 1)) != 1:
                    raise TemplateError("RayJob 'Head' role must have replicas = 1")
                if not merged.get("command") and not command:
                    raise TemplateError("RayJob 'Head' role requires a command")
            job_spec[role] = _build_job_spec(merged, role=role)
    else:
        if not command:
            raise TemplateError("'command' is required")
        job_spec = _build_job_spec(template)

    if not command and job_type != "RayJob":
        raise TemplateError("'command' is required")

    # Deviates from the API's own default of "normal": this repo's convention is to
    # submit at the highest priority unless a template says otherwise.
    priority = str(template.get("priority") or DEFAULT_PRIORITY).lower()
    if priority not in PRIORITIES:
        raise TemplateError(f"priority must be one of {', '.join(PRIORITIES)}, got {priority!r}")

    body: dict[str, Any] = {
        "name": name,
        "jobType": job_type,
        "jobSpec": job_spec,
        "priority": priority,
        "labels": _normalize_labels(template.get("labels")),
        "datasources": _normalize_datasources(
            template.get("datasources") or template.get("dataSources")
        ),
    }
    if command:
        body["command"] = command

    fault_tolerance = _as_bool(template.get("faultTolerance", False))
    fault_args = str(template.get("faultToleranceArgs") or "").strip()
    if fault_tolerance or fault_args:
        if job_type != "PyTorchJob":
            raise TemplateError("fault tolerance is only supported for PyTorchJob")
        body["faultTolerance"] = True
        if fault_args:
            body["faultToleranceArgs"] = fault_args

    if _as_bool(template.get("enableBccl", False)):
        replicas = job_spec.get("replicas") if isinstance(job_spec, dict) and "replicas" in job_spec else None
        if replicas is not None and int(replicas) < 2:
            raise TemplateError("enableBccl requires at least 2 replicas")
        body["enableBccl"] = True

    tensorboard = _build_tensorboard(template.get("tensorboard") or template.get("tensorboardConfig"))
    if tensorboard is not None:
        if job_type == "RayJob":
            raise TemplateError("RayJob does not support TensorBoard")
        body["tensorboardConfig"] = tensorboard

    if template.get("alertConfig"):
        body["alertConfig"] = dict(template["alertConfig"])
    if template.get("advancedSettings"):
        body["advancedSettings"] = dict(template["advancedSettings"])
    if template.get("preInitCommand"):
        body["preInitCommand"] = str(template["preInitCommand"])

    retention = str(template.get("retentionPeriod") or "").strip()
    if retention:
        if job_type == "RayJob":
            raise TemplateError("RayJob does not support retentionPeriod")
        if not re.fullmatch(r"\d+[mhd]", retention):
            raise TemplateError(f"retentionPeriod must look like '30m', '2h' or '1d', got {retention!r}")
        body["retentionPeriod"] = retention

    if template.get("visibleScope") is not None:
        scope = int(template["visibleScope"])
        if scope not in (0, 1):
            raise TemplateError("visibleScope must be 0 (creator only) or 1 (queue)")
        body["visibleScope"] = scope

    return body


def gpu_requirement(body: Mapping[str, Any]) -> tuple[str, float]:
    """Total accelerator demand of a built ``CreateJob`` body.

    Returns ``(descriptor, total_cards)`` where ``total_cards`` already accounts for
    ``replicas`` -- a 2-replica job asking 8 cards each needs 16 in the queue. Returns
    ``("", 0)`` when the job requests no accelerator (the queue then applies defaults).
    """
    spec = body.get("jobSpec") or {}
    specs = [spec] if isinstance(spec, Mapping) and "image" in spec else [
        s for s in (spec.values() if isinstance(spec, Mapping) else []) if isinstance(s, Mapping)
    ]
    totals: dict[str, float] = {}
    for role_spec in specs:
        replicas = int(role_spec.get("replicas") or 1)
        for entry in role_spec.get("resources") or []:
            name = str(entry.get("name", ""))
            if name in PLAIN_RESOURCES or not ("cgpu" in name or "gpu" in name or "xpu" in name):
                continue
            totals[name] = totals.get(name, 0.0) + float(entry.get("quantity") or 0) * replicas
    if not totals:
        return "", 0.0
    # A job can only target one accelerator type in practice; take the largest.
    descriptor = max(totals, key=lambda k: totals[k])
    return descriptor, totals[descriptor]


def summarize_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """Compact, human-readable view of a built request body (used by ``--dry-run``)."""
    spec = body.get("jobSpec") or {}
    if isinstance(spec, Mapping) and "image" in spec:
        specs = {"": spec}
    else:
        specs = dict(spec) if isinstance(spec, Mapping) else {}
    roles = []
    for role, role_spec in specs.items():
        resources = ", ".join(
            f"{r['name']}={r['quantity']}" for r in (role_spec.get("resources") or [])
        )
        roles.append(
            {
                "role": role or body.get("jobType", ""),
                "image": role_spec.get("image"),
                "replicas": role_spec.get("replicas"),
                "resources": resources or "(queue defaults)",
                "rdma": role_spec.get("enableRDMA"),
            }
        )
    return {
        "name": body.get("name"),
        "jobType": body.get("jobType"),
        "priority": body.get("priority"),
        "command": body.get("command"),
        "roles": roles,
        "datasources": [
            f"{d.get('type')}:{d.get('name') or d.get('id') or d.get('sourcePath', '')}"
            f" -> {d.get('mountPath')}"
            for d in body.get("datasources") or []
        ],
        "faultTolerance": body.get("faultTolerance", False),
        "enableBccl": body.get("enableBccl", False),
    }
