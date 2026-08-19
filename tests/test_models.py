"""Template -> CreateJob body translation."""

from __future__ import annotations

import json

import pytest

from aihc_job.errors import TemplateError
from aihc_job.models import (
    build_create_job_body,
    load_template,
    merge_templates,
    parse_gpu,
)

MINIMAL = {
    "name": "demo-job",
    "image": "registry.baidubce.com/x/y:v1",
    "command": "sleep 1d",
}


def test_minimal_template_produces_documented_shape():
    body = build_create_job_body(MINIMAL)
    assert body["name"] == "demo-job"
    assert body["jobType"] == "PyTorchJob"
    assert body["command"] == "sleep 1d"
    assert body["priority"] == "high"  # project default, not the API's "normal"
    assert body["jobSpec"] == {
        "image": "registry.baidubce.com/x/y:v1",
        "replicas": 1,
        "resources": [],
        "envs": [],
        "enableRDMA": False,
        "hostNetwork": False,
    }


def test_gpu_shorthand_maps_to_baidu_descriptors():
    assert parse_gpu("a800:8") == [{"name": "baidu.com/a800_80g_cgpu", "quantity": 8}]
    assert parse_gpu("a800:8,cpu=64,memory=512") == [
        {"name": "baidu.com/a800_80g_cgpu", "quantity": 8},
        {"name": "cpu", "quantity": 64},
        {"name": "memory", "quantity": 512},
    ]
    assert parse_gpu("shm:32") == [{"name": "sharedMemory", "quantity": 32}]
    assert parse_gpu("baidu.com/l20_cgpu:2") == [{"name": "baidu.com/l20_cgpu", "quantity": 2}]
    assert parse_gpu("a100_40g:0.5") == [{"name": "baidu.com/a100_40g_cgpu", "quantity": 0.5}]


def test_gpu_shorthand_rejects_unparseable_input():
    with pytest.raises(TemplateError, match="cannot parse gpu spec"):
        parse_gpu("8")
    with pytest.raises(TemplateError, match="unknown resource/GPU type"):
        parse_gpu("mi300x:8")


def test_explicit_resources_override_shorthand_for_the_same_name():
    body = build_create_job_body(
        {**MINIMAL, "gpu": "a800:8", "resources": [{"name": "a800", "quantity": 4}]}
    )
    assert body["jobSpec"]["resources"] == [{"name": "baidu.com/a800_80g_cgpu", "quantity": 4}]


def test_envs_and_labels_accept_mappings_and_lists():
    body = build_create_job_body({**MINIMAL, "envs": {"A": "1"}, "labels": {"owner": "me"}})
    assert body["jobSpec"]["envs"] == [{"name": "A", "value": "1"}]
    assert body["labels"] == [{"key": "owner", "value": "me"}]

    body = build_create_job_body({**MINIMAL, "envs": ["A=1", {"name": "B", "value": "2"}]})
    assert body["jobSpec"]["envs"] == [{"name": "A", "value": "1"}, {"name": "B", "value": "2"}]


def test_datasource_shorthand_string():
    body = build_create_job_body(
        {**MINIMAL, "datasources": ["type=pfs,name=pfs-abc,mountPath=/mnt/cluster,readOnly=true"]}
    )
    assert body["datasources"] == [
        {
            "type": "pfs",
            "name": "pfs-abc",
            "mountPath": "/mnt/cluster",
            "options": {"readOnly": True},
        }
    ]


def test_cfs_datasource_lifts_flat_options_and_fills_defaults():
    body = build_create_job_body(
        {
            **MINIMAL,
            "datasources": [
                {
                    "type": "cfs",
                    "mountPath": "/share",
                    "instanceId": "cfs-xxxxxxxx",
                    "mountPoint": "cfs-xxxxxxxx.lb-yyyyyyyy.cfs.bj.baidubce.com",
                }
            ],
        }
    )
    assert body["datasources"] == [
        {
            "type": "cfs",
            "mountPath": "/share",
            "options": {
                "cfsInstanceId": "cfs-xxxxxxxx",
                "cfsMountPoint": "cfs-xxxxxxxx.lb-yyyyyyyy.cfs.bj.baidubce.com",
            },
            "name": "cfs-xxxxxxxx",  # defaulted to the instance ID
            "sourcePath": "/",
        }
    ]


def test_cfs_datasource_shorthand_string():
    body = build_create_job_body(
        {
            **MINIMAL,
            "datasources": [
                "type=cfs,instanceId=cfs-abc,mountPoint=cfs-abc.lb-x.cfs.bj.baidubce.com,"
                "mountPath=/share,readOnly=true"
            ],
        }
    )
    options = body["datasources"][0]["options"]
    assert options["cfsInstanceId"] == "cfs-abc"
    assert options["cfsMountPoint"] == "cfs-abc.lb-x.cfs.bj.baidubce.com"
    assert options["readOnly"] is True


def test_cfs_datasource_requires_instance_id():
    """CreateJob returns 400 "cfs datasource cfsInstanceId is required" without it --
    even though DescribeJob reports the field blank on jobs that were created fine."""
    with pytest.raises(TemplateError, match="requires options.cfsInstanceId"):
        build_create_job_body({**MINIMAL, "datasources": [{"type": "cfs", "mountPath": "/share"}]})


def test_devshm_emptydir_datasource():
    """How this pool gets shared memory: emptydir on /dev/shm, not a sharedMemory resource."""
    body = build_create_job_body(
        {
            **MINIMAL,
            "datasources": [
                {
                    "type": "emptydir",
                    "name": "devshm",
                    "mountPath": "/dev/shm",
                    "medium": "Memory",
                    "sizeLimit": 64,
                }
            ],
        }
    )
    assert body["datasources"] == [
        {
            "type": "emptydir",
            "name": "devshm",
            "mountPath": "/dev/shm",
            "options": {"medium": "Memory", "sizeLimit": 64},
        }
    ]


@pytest.mark.parametrize(
    "template,message",
    [
        ({**MINIMAL, "name": "Bad_Name"}, "must be lowercase"),
        ({**MINIMAL, "image": "registry.baidubce.com/x/y"}, "has no tag"),
        ({"name": "a", "image": "x/y:1"}, "'command' is required"),
        ({**MINIMAL, "replicas": 0}, "must be >= 1"),
        ({**MINIMAL, "priority": "urgent"}, "priority must be one of"),
        ({**MINIMAL, "framework": "jax"}, "unknown jobType"),
        ({**MINIMAL, "retentionPeriod": "forever"}, "retentionPeriod must look like"),
        ({**MINIMAL, "visibleScope": 7}, "visibleScope must be"),
        ({**MINIMAL, "typo": 1}, "unknown template key"),
        ({**MINIMAL, "datasources": [{"type": "pfs", "mountPath": "/x"}]}, "requires 'name'"),
        ({**MINIMAL, "datasources": [{"type": "nfs", "mountPath": "/x"}]}, "unsupported datasource"),
        ({**MINIMAL, "datasources": [{"type": "dataset", "mountPath": "/x"}]}, "requires 'id'"),
        ({**MINIMAL, "framework": "TFJob"}, None),  # valid: single spec is allowed for TFJob too
    ],
)
def test_validation(template, message):
    if message is None:
        build_create_job_body(template)
        return
    with pytest.raises(TemplateError, match=message):
        build_create_job_body(template)


def test_fault_tolerance_is_pytorch_only():
    body = build_create_job_body({**MINIMAL, "faultTolerance": True, "faultToleranceArgs": "--x=1"})
    assert body["faultTolerance"] is True
    assert body["faultToleranceArgs"] == "--x=1"
    with pytest.raises(TemplateError, match="only supported for PyTorchJob"):
        build_create_job_body({**MINIMAL, "framework": "mpi", "faultTolerance": True})


def test_bccl_requires_two_replicas():
    with pytest.raises(TemplateError, match="at least 2 replicas"):
        build_create_job_body({**MINIMAL, "enableBccl": True})
    assert build_create_job_body({**MINIMAL, "replicas": 2, "enableBccl": True})["enableBccl"]


def test_tensorboard_requires_a_datasource():
    with pytest.raises(TemplateError, match="requires 'datasourceType'"):
        build_create_job_body({**MINIMAL, "tensorboard": {"enable": True}})
    body = build_create_job_body(
        {
            **MINIMAL,
            "tensorboard": {
                "datasourceType": "pfs",
                "datasourceName": "pfs-abc",
                "logPath": "/tb",
            },
        }
    )
    assert body["tensorboardConfig"]["enable"] is True


def test_ray_roles_build_a_spec_map():
    body = build_create_job_body(
        {
            "name": "ray-demo",
            "framework": "ray",
            "image": "registry.baidubce.com/x/ray:v1",
            "gpu": "a800:8",
            "roles": {
                "Head": {"replicas": 1, "command": "ray start --head"},
                "worker-a": {"replicas": 2},
            },
        }
    )
    assert set(body["jobSpec"]) == {"Head", "worker-a"}
    assert body["jobSpec"]["Head"]["command"] == "ray start --head"
    assert body["jobSpec"]["worker-a"]["replicas"] == 2


def test_ray_requires_head_role():
    with pytest.raises(TemplateError, match="requires a 'Head' role"):
        build_create_job_body(
            {
                "name": "ray-demo",
                "framework": "ray",
                "image": "x/y:1",
                "roles": {"worker": {"replicas": 1}},
            }
        )


def test_roles_rejected_for_pytorch():
    with pytest.raises(TemplateError, match="applies to TFJob/RayJob"):
        build_create_job_body({**MINIMAL, "roles": {"Worker": {"replicas": 2}}})


def test_merge_templates_is_shallow_and_last_wins():
    merged = merge_templates({"name": "a", "replicas": 1}, {"replicas": 4}, {"gpu": "a800:8"})
    assert merged == {"name": "a", "replicas": 4, "gpu": "a800:8"}
    assert merge_templates({"a": 1}, None, {"a": None}) == {"a": 1}


def test_underscore_keys_are_treated_as_comments():
    assert build_create_job_body({**MINIMAL, "_note": "ignore me"})["name"] == "demo-job"


def test_load_template_json_and_errors(tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps(MINIMAL), encoding="utf-8")
    assert load_template(path) == MINIMAL

    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    with pytest.raises(TemplateError, match="invalid JSON"):
        load_template(bad)

    with pytest.raises(TemplateError, match="template not found"):
        load_template(tmp_path / "missing.json")


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


def _example_variables():
    """Stand-in values for the placeholders the shipped examples use."""
    return {
        "AIHC_IMAGE": "registry.example.com/gpu/img:tag",
        "AIHC_WORKDIR": "/share/someone",
        "AIHC_OWNER": "someone",
        "AIHC_CFS_INSTANCE": "cfs-XXXXXXXX",
        "AIHC_CFS_MOUNT": "cfs-XXXXXXXX.lb-yyyyyyyy.cfs.bj.baidubce.com",
    }


def test_shipped_examples_are_valid():
    examples = _repo_root() / "examples"
    for path in sorted(examples.glob("*.json")):
        template = load_template(path)
        if "name" not in template:  # merge fragments such as base.json
            continue
        build_create_job_body(template, variables=_example_variables())


def test_examples_only_use_variables_that_env_example_documents():
    """A placeholder nobody documents is a placeholder nobody will set."""
    from aihc_job.config import parse_env_file
    from aihc_job.models import _VARIABLE_RE

    root = _repo_root()
    documented = set(parse_env_file((root / ".env.example").read_text(encoding="utf-8")))
    for path in sorted((root / "examples").glob("*.json")):
        used = set(_VARIABLE_RE.findall(path.read_text(encoding="utf-8")))
        undocumented = {name for name, _fallback in used} - documented
        assert not undocumented, f"{path.name} uses undocumented {undocumented}"


def test_queue_reported_gpu_aliases_resolve():
    """4090 / B200 appear in this account's queues but not in the published table."""
    assert parse_gpu("4090:2") == [{"name": "baidu.com/rtx_4090_cgpu", "quantity": 2}]
    assert parse_gpu("b200:8") == [{"name": "baidu.com/b20z_180g_cgpu", "quantity": 8}]
    assert parse_gpu("3090:1") == [{"name": "baidu.com/rtx_3090_cgpu", "quantity": 1}]


# ------------------------------------------------------- {{VAR}} expansion


def test_expansion_reaches_nested_strings_only():
    from aihc_job.models import expand_variables

    template = {
        "image": "{{AIHC_IMAGE}}",
        "replicas": 4,
        "enableRDMA": True,
        "envs": {"WORKDIR": "{{AIHC_WORKDIR}}/data"},
        "datasources": [{"options": {"cfsInstanceId": "{{AIHC_CFS_INSTANCE}}"}}],
    }
    expanded = expand_variables(
        template,
        {"AIHC_IMAGE": "img:1", "AIHC_WORKDIR": "/share/x", "AIHC_CFS_INSTANCE": "cfs-1"},
    )
    assert expanded == {
        "image": "img:1",
        "replicas": 4,
        "enableRDMA": True,
        "envs": {"WORKDIR": "/share/x/data"},
        "datasources": [{"options": {"cfsInstanceId": "cfs-1"}}],
    }


def test_shell_variables_survive_expansion():
    """$RANK / ${MASTER_ADDR} are for the remote shell; only {{...}} is ours."""
    from aihc_job.models import expand_variables

    command = "torchrun --node_rank $RANK --master_addr ${MASTER_ADDR} --dir {{AIHC_WORKDIR}}"
    assert expand_variables(command, {"AIHC_WORKDIR": "/share/x"}) == (
        "torchrun --node_rank $RANK --master_addr ${MASTER_ADDR} --dir /share/x"
    )


def test_unset_variable_is_an_error_not_an_empty_string():
    """An empty {{AIHC_WORKDIR}} would submit a job that cd's to /."""
    from aihc_job.models import expand_variables

    with pytest.raises(TemplateError) as excinfo:
        expand_variables({"command": "cd {{AIHC_WORKDIR}}"}, {})
    message = str(excinfo.value)
    assert "AIHC_WORKDIR is not set" in message
    assert ".env" in message  # says where to fix it

    # An empty value counts as unset: a placeholder left blank in .env is a mistake.
    with pytest.raises(TemplateError):
        expand_variables("{{AIHC_OWNER}}", {"AIHC_OWNER": ""})


def test_fallback_syntax_makes_a_variable_optional():
    from aihc_job.models import expand_variables

    assert expand_variables("{{AIHC_OWNER:-nobody}}", {}) == "nobody"
    assert expand_variables("{{AIHC_OWNER:-nobody}}", {"AIHC_OWNER": "someone"}) == "someone"
    assert expand_variables("{{AIHC_TAG:-}}", {}) == ""  # deliberately empty


def test_comments_may_mention_placeholders_without_setting_them():
    body = build_create_job_body(
        {
            "_comment": "set {{AIHC_IMAGE}} in .env",
            "name": "demo",
            "image": "r/x:1",
            "command": "c",
        }
    )
    assert body["jobSpec"]["image"] == "r/x:1"


def test_expansion_happens_before_validation():
    """A placeholder must not look like an image without a tag, or a bad job name."""
    body = build_create_job_body(
        {"name": "{{AIHC_OWNER}}-run", "image": "{{AIHC_IMAGE}}", "command": "c"},
        variables={"AIHC_OWNER": "someone", "AIHC_IMAGE": "registry/img:tag"},
    )
    assert body["name"] == "someone-run"
    assert body["jobSpec"]["image"] == "registry/img:tag"
