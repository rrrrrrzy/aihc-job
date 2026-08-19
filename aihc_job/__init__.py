"""Submit and manage Baidu AIHC (百舸) training jobs from the CLI or Python.

Typical programmatic use::

    from aihc_job import JobManager, AihcClient, load_config

    config = load_config()                       # env / ~/.aihc/config*
    jobs = JobManager(AihcClient(config), config)
    result = jobs.submit({
        "name": "vla-pretrain",
        "image": "registry.baidubce.com/.../torch:2.4-cu124",
        "command": "bash /mnt/cluster/run.sh",
        "replicas": 4,
        "gpu": "a800:8",
        "enableRDMA": True,
    })
    jobs.wait(result["jobId"])
"""

from __future__ import annotations

__version__ = "0.1.0"

from .client import AihcClient
from .config import Config, load_config, write_config
from .errors import AihcError, ApiError, ConfigError, JobFailed, TemplateError, WaitTimeout
from .jobs import JobManager
from .models import build_create_job_body, load_template, merge_templates

__all__ = [
    "AihcClient",
    "AihcError",
    "ApiError",
    "Config",
    "ConfigError",
    "JobFailed",
    "JobManager",
    "TemplateError",
    "WaitTimeout",
    "__version__",
    "build_create_job_body",
    "load_config",
    "load_template",
    "merge_templates",
    "write_config",
]
