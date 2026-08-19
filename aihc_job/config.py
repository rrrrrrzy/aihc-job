"""Configuration resolution: credentials, region/endpoint, default pool + queue.

Precedence (first hit wins per field, so sources merge rather than shadow):

1. explicit keyword arguments (CLI flags)
2. environment variables -- ``AIHC_ACCESS_KEY``/``AIHC_AK``, ``AIHC_SECRET_KEY``/
   ``AIHC_SK``, ``AIHC_REGION``, ``AIHC_ENDPOINT``, ``AIHC_POOL``, ``AIHC_QUEUE``
3. the ``.env`` file at the repo root (or ``$AIHC_ENV_FILE`` / ``--env-file``) -- one
   file holding everything that differs per user, so a checkout can be handed to a
   colleague who only edits that
4. ``$AIHC_CONFIG`` if set, else ``./.aihc/config{,.json,.yaml}``
5. ``~/.aihc/config{,.json,.yaml}`` -- the official ``aihc`` CLI's location, so a
   machine already configured for that CLI needs no extra setup here

A real exported variable deliberately beats ``.env``: a one-off
``AIHC_QUEUE=... aihc-job submit`` must not require editing the file.

Files use the official CLI's key names (``region``, ``credentials.accesskey``,
``credentials.secretkey``, ``defaultpool``, ``defaultqueue``). JSON is always
readable; YAML needs PyYAML, except for the flat official layout which a small
built-in fallback parser handles.

``.env`` carries the template variables too (``AIHC_IMAGE``, ``AIHC_OWNER``,
``AIHC_WORKDIR``, ...): :func:`template_variables` hands them to
``models.expand_variables``, which substitutes ``{{VAR}}`` placeholders.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

# region short name -> (long name, OpenAPI endpoint host)
REGIONS: dict[str, tuple[str, str]] = {
    "cn-bj": ("cn-beijing", "aihc.bj.baidubce.com"),
    "cn-gz": ("cn-guangzhou", "aihc.gz.baidubce.com"),
    "cn-su": ("cn-suzhou", "aihc.su.baidubce.com"),
    "cn-bd": ("cn-baoding", "aihc.bd.baidubce.com"),
    "cn-fwh": ("cn-wuhan", "aihc.fwh.baidubce.com"),
    "cn-yq": ("cn-yangquan", "aihc.yq.baidubce.com"),
}
_LONG_TO_SHORT = {long: short for short, (long, _) in REGIONS.items()}

DEFAULT_REGION = "cn-bj"
# Fully managed ("全托管") pools use this sentinel instead of a real pool ID.
SERVERLESS_POOL_ID = "aihc-serverless"

CONFIG_FILENAMES = ("config", "config.json", "config.yaml", "config.yml")

ENV_FILENAME = ".env"
# Documented in .env.example. Only used to decide what `config show` is worth printing --
# a .env may define anything, and templates may reference anything.
KNOWN_VARIABLES = (
    "AIHC_AK",
    "AIHC_SK",
    "AIHC_REGION",
    "AIHC_ENDPOINT",
    "AIHC_POOL",
    "AIHC_QUEUE",
    "AIHC_QUEUE_POOL",
    "AIHC_IMAGE",
    "AIHC_WORKDIR",
    "AIHC_OWNER",
    "AIHC_CFS_INSTANCE",
    "AIHC_CFS_MOUNT",
    "AIHC_TIMEOUT",
    "AIHC_RETRIES",
)
# The repo root: aihc_job/config.py -> aihc_job -> <root>. Lets an installed entry point
# still find the checkout's .env, not only a .env in whatever directory it was run from.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def normalize_region(region: str) -> str:
    """Accept either ``cn-bj`` or ``cn-beijing`` and return the short form."""
    r = (region or "").strip().lower()
    if r in REGIONS:
        return r
    if r in _LONG_TO_SHORT:
        return _LONG_TO_SHORT[r]
    raise ConfigError(
        f"unknown region {region!r}; expected one of "
        + ", ".join(sorted(REGIONS) + sorted(_LONG_TO_SHORT))
    )


@dataclass
class Config:
    """Resolved settings for one AIHC client."""

    access_key: str = ""
    secret_key: str = ""
    region: str = DEFAULT_REGION
    endpoint: str = ""
    pool: str = ""
    queue: str = ""
    # Real resource pool ID used only for DescribeQueues/capacity checks: job actions
    # need the aihc-serverless sentinel, which DescribeQueues rejects.
    queue_pool: str = ""
    timeout: float = 60.0
    retries: int = 3
    sources: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.region = normalize_region(self.region or DEFAULT_REGION)
        if not self.endpoint:
            self.endpoint = REGIONS[self.region][1]
        self.endpoint = self.endpoint.rstrip("/")

    @property
    def base_url(self) -> str:
        if self.endpoint.startswith(("http://", "https://")):
            return self.endpoint
        return f"https://{self.endpoint}"

    @property
    def host(self) -> str:
        return self.base_url.split("://", 1)[1].split("/", 1)[0]

    def require_credentials(self) -> None:
        missing = [n for n, v in (("access key", self.access_key), ("secret key", self.secret_key)) if not v]
        if missing:
            raise ConfigError(
                f"missing {' and '.join(missing)}. Set AIHC_AK / AIHC_SK, or run "
                "`aihc-job config init --access-key ... --secret-key ...`"
            )

    def require_pool(self) -> str:
        if not self.pool:
            raise ConfigError(
                "no resource pool. Pass --pool, set AIHC_POOL, or store `defaultpool` "
                f"in the config file (use {SERVERLESS_POOL_ID!r} for fully managed pools)."
            )
        return self.pool

    def require_queue(self) -> str:
        if not self.queue:
            raise ConfigError(
                "no queue. Pass --queue, set AIHC_QUEUE, or store `defaultqueue` in the "
                "config file (self-managed pools use the queue *name*, fully managed pools "
                "use the queue *ID*)."
            )
        return self.queue

    def redacted(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "endpoint": self.endpoint,
            "access_key": _mask(self.access_key),
            "secret_key": _mask(self.secret_key),
            "pool": self.pool,
            "queue": self.queue,
            "queue_pool": self.queue_pool,
            "sources": self.sources,
        }


def _mask(secret: str) -> str:
    if not secret:
        return ""
    return f"{secret[:6]}{'*' * 8}" if len(secret) > 8 else "*" * len(secret)


def _parse_flat_yaml(text: str) -> dict[str, Any]:
    """Minimal parser for the official CLI's flat config layout.

    Handles ``key: value`` plus one level of indented nesting (``credentials:``)
    and nothing else -- enough to read ``~/.aihc/config`` without PyYAML. Any
    richer YAML must go through PyYAML.
    """
    result: dict[str, Any] = {}
    nested: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indented = line[:1].isspace()
        key, sep, value = line.strip().partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip("'\"")
        if indented:
            if nested is not None and value:
                nested[key] = value
            continue
        if value:
            result[key] = value
            nested = None
        else:  # `credentials:` and friends open a nested block
            nested = {}
            result[key] = nested
    return result


def parse_env_file(text: str) -> dict[str, str]:
    """Parse a ``.env`` file: ``KEY=value`` per line, ``#`` comments, optional quotes.

    Deliberately literal -- values are not expanded against each other and ``$VAR`` is
    left alone, because these values end up inside job commands where ``$RANK`` and
    friends must reach the *remote* shell untouched.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote, value = value[0], value[1:-1]
            if quote == '"':  # only double quotes carry escapes, as in every dotenv
                value = value.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
        else:
            # An unquoted trailing comment is a comment; a '#' inside a value must be quoted.
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def find_env_file(explicit: str | Path | None = None) -> Path | None:
    """Locate the ``.env``: ``--env-file``, then ``$AIHC_ENV_FILE``, cwd, repo root.

    A path given explicitly (flag or variable) must exist -- falling back to a different
    file after a typo would be worse than failing, since the wrong queue or image would go
    unnoticed. Auto-discovery finding nothing is fine and simply means no ``.env``.
    """
    named = explicit or os.environ.get("AIHC_ENV_FILE")
    if named:
        path = Path(named).expanduser()
        if not path.is_file():
            source = "--env-file" if explicit else "$AIHC_ENV_FILE"
            raise ConfigError(f"env file not found: {path} (from {source})")
        return path
    for candidate in (Path.cwd() / ENV_FILENAME, PACKAGE_ROOT / ENV_FILENAME):
        if candidate.is_file():
            return candidate
    return None


def load_env_file(explicit: str | Path | None = None) -> tuple[dict[str, str], Path | None]:
    """Read the ``.env`` that applies, returning its values and where they came from."""
    path = find_env_file(explicit)
    if path is None:
        return {}, None
    try:
        return parse_env_file(path.read_text(encoding="utf-8")), path
    except OSError as exc:
        raise ConfigError(f"could not read env file {path}: {exc}") from exc


def template_variables(env_file: str | Path | None = None) -> dict[str, str]:
    """Values available to ``{{VAR}}`` placeholders in templates.

    The ``.env`` file, overridden by anything actually exported in the environment.
    """
    values, _ = load_env_file(env_file)
    values.update({k: v for k, v in os.environ.items() if v})
    return values


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Read a config file (JSON, YAML, or the official flat layout)."""
    p = Path(path).expanduser()
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    if p.suffix == ".json":
        return json.loads(text)
    try:  # a `config` file with no suffix is YAML in the official CLI, but JSON also works
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return _parse_flat_yaml(text)
    loaded = yaml.safe_load(text)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{p}: expected a mapping at the top level")
    return loaded


def candidate_config_paths(explicit: str | Path | None = None) -> list[Path]:
    """Config file locations in precedence order."""
    if explicit:
        return [Path(explicit).expanduser()]
    paths: list[Path] = []
    env_path = os.environ.get("AIHC_CONFIG")
    if env_path:
        paths.append(Path(env_path).expanduser())
    for base in (Path.cwd() / ".aihc", Path.home() / ".aihc"):
        paths.extend(base / name for name in CONFIG_FILENAMES)
    return paths


def default_config_path() -> Path:
    """Where ``config init`` writes when no path is given."""
    env_path = os.environ.get("AIHC_CONFIG")
    return Path(env_path).expanduser() if env_path else Path.home() / ".aihc" / "config.json"


def _from_file_dict(data: dict[str, Any]) -> dict[str, str]:
    creds = data.get("credentials") or {}
    if not isinstance(creds, dict):
        creds = {}

    def pick(*keys: str) -> str:
        for scope in (data, creds):
            for key in keys:
                value = scope.get(key)
                if value:
                    return str(value)
        return ""

    return {
        "access_key": pick("accesskey", "accessKey", "access_key", "ak"),
        "secret_key": pick("secretkey", "secretKey", "secret_key", "sk"),
        "region": pick("region"),
        "endpoint": pick("endpoint"),
        "pool": pick("defaultpool", "defaultPool", "pool", "resourcePoolId", "resourcepoolid"),
        "queue": pick("defaultqueue", "defaultQueue", "queue", "queueID", "queueid"),
        "queue_pool": pick("queuepool", "queuePool", "queue_pool", "inspectpool"),
    }


def _from_env_mapping(env: Mapping[str, str]) -> dict[str, str]:
    """Config fields as spelled in the environment -- and in ``.env``, same names."""
    return {
        "access_key": env.get("AIHC_ACCESS_KEY") or env.get("AIHC_AK") or "",
        "secret_key": env.get("AIHC_SECRET_KEY") or env.get("AIHC_SK") or "",
        "region": env.get("AIHC_REGION", ""),
        "endpoint": env.get("AIHC_ENDPOINT", ""),
        "pool": env.get("AIHC_POOL") or env.get("AIHC_RESOURCE_POOL_ID") or "",
        "queue": env.get("AIHC_QUEUE", ""),
        "queue_pool": env.get("AIHC_QUEUE_POOL", ""),
    }


def load_config(
    *,
    access_key: str | None = None,
    secret_key: str | None = None,
    region: str | None = None,
    endpoint: str | None = None,
    pool: str | None = None,
    queue: str | None = None,
    queue_pool: str | None = None,
    config_path: str | Path | None = None,
    env_file: str | Path | None = None,
    timeout: float | None = None,
    retries: int | None = None,
) -> Config:
    """Merge flags, environment, and config files into a :class:`Config`."""
    resolved: dict[str, str] = {}
    sources: list[str] = []

    def absorb(values: dict[str, str], label: str) -> None:
        used = False
        for key, value in values.items():
            if value and not resolved.get(key):
                resolved[key] = value
                used = True
        if used:
            sources.append(label)

    absorb(
        {
            "access_key": access_key or "",
            "secret_key": secret_key or "",
            "region": region or "",
            "endpoint": endpoint or "",
            "pool": pool or "",
            "queue": queue or "",
            "queue_pool": queue_pool or "",
        },
        "flags",
    )
    absorb(_from_env_mapping(os.environ), "env")

    # The repo's .env: same variable names, lower precedence than a real export.
    dotenv, dotenv_path = load_env_file(env_file)
    if dotenv:
        absorb(_from_env_mapping(dotenv), str(dotenv_path))

    for path in candidate_config_paths(config_path):
        if not path.is_file():
            continue
        try:
            absorb(_from_file_dict(load_config_file(path)), str(path))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"could not read config file {path}: {exc}") from exc
        if config_path:
            break

    if config_path and not Path(config_path).expanduser().is_file():
        raise ConfigError(f"config file not found: {config_path}")

    return Config(
        access_key=resolved.get("access_key", ""),
        secret_key=resolved.get("secret_key", ""),
        region=resolved.get("region") or DEFAULT_REGION,
        endpoint=resolved.get("endpoint", ""),
        pool=resolved.get("pool", ""),
        queue=resolved.get("queue", ""),
        queue_pool=resolved.get("queue_pool", ""),
        timeout=timeout if timeout is not None else float(_setting("AIHC_TIMEOUT", dotenv, 60)),
        retries=retries if retries is not None else int(_setting("AIHC_RETRIES", dotenv, 3)),
        sources=sources,
    )


def _setting(name: str, dotenv: Mapping[str, str], default: object) -> object:
    """Numeric knobs: environment first, then ``.env``, then the built-in default."""
    return os.environ.get(name) or dotenv.get(name) or default


def write_config(
    path: str | Path | None = None,
    *,
    access_key: str = "",
    secret_key: str = "",
    region: str = "",
    pool: str = "",
    queue: str = "",
    queue_pool: str = "",
) -> Path:
    """Create or update a JSON config file, preserving fields not passed in."""
    target = Path(path).expanduser() if path else default_config_path()
    if target.is_dir():
        target = target / "config.json"
    existing: dict[str, Any] = {}
    if target.is_file():
        try:
            existing = load_config_file(target)
        except (OSError, ValueError):
            existing = {}

    creds = dict(existing.get("credentials") or {})
    if access_key:
        creds["accesskey"] = access_key
    if secret_key:
        creds["secretkey"] = secret_key

    merged: dict[str, Any] = dict(existing)
    merged["credentials"] = creds
    if region:
        merged["region"] = normalize_region(region)
    merged.setdefault("region", DEFAULT_REGION)
    if pool:
        merged["defaultpool"] = pool
    if queue:
        merged["defaultqueue"] = queue
    if queue_pool:
        merged["queuepool"] = queue_pool

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    target.chmod(0o600)  # the file holds a secret key
    return target
