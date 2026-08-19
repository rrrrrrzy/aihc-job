"""Config resolution precedence and file formats."""

from __future__ import annotations

import json

import pytest

from aihc_job.config import Config, load_config, normalize_region, write_config
from aihc_job.errors import ConfigError

ENV_KEYS = [
    "AIHC_ACCESS_KEY",
    "AIHC_AK",
    "AIHC_SECRET_KEY",
    "AIHC_SK",
    "AIHC_REGION",
    "AIHC_ENDPOINT",
    "AIHC_POOL",
    "AIHC_RESOURCE_POOL_ID",
    "AIHC_QUEUE",
    "AIHC_CONFIG",
    "AIHC_TIMEOUT",
    "AIHC_RETRIES",
]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Keep ./.aihc and ~/.aihc discovery from reaching the developer's real files.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    yield


def test_region_endpoint_mapping():
    assert Config(region="cn-bj").endpoint == "aihc.bj.baidubce.com"
    assert Config(region="cn-guangzhou").region == "cn-gz"
    assert Config(region="cn-gz").endpoint == "aihc.gz.baidubce.com"
    assert normalize_region("CN-SU") == "cn-su"
    with pytest.raises(ConfigError, match="unknown region"):
        Config(region="us-east-1")


def test_base_url_and_host():
    assert Config().base_url == "https://aihc.bj.baidubce.com"
    assert Config(endpoint="http://127.0.0.1:8080").base_url == "http://127.0.0.1:8080"
    assert Config(endpoint="http://127.0.0.1:8080").host == "127.0.0.1:8080"


def test_flags_beat_env_beats_file(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "region": "cn-su",
                "credentials": {"accesskey": "file-ak", "secretkey": "file-sk"},
                "defaultpool": "cce-file",
                "defaultqueue": "queue-file",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIHC_CONFIG", str(path))
    monkeypatch.setenv("AIHC_AK", "env-ak")
    monkeypatch.setenv("AIHC_POOL", "cce-env")

    config = load_config(pool="cce-flag")
    assert config.pool == "cce-flag"  # flag wins
    assert config.access_key == "env-ak"  # env wins over file
    assert config.secret_key == "file-sk"  # file fills the gap
    assert config.queue == "queue-file"
    assert config.region == "cn-su"


def test_official_cli_yaml_config_is_readable(tmp_path, monkeypatch):
    """The official `aihc` CLI writes flat YAML; read it even without PyYAML."""
    path = tmp_path / "config"
    path.write_text(
        "region: cn-gz\n"
        "credentials:\n"
        "    accesskey: ALTAKyaml\n"
        "    secretkey: yamlsecret\n"
        'defaultpool: "cce-yaml"\n'
        "defaultqueue: default\n"
        "# a comment\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AIHC_CONFIG", str(path))
    config = load_config()
    assert (config.access_key, config.secret_key) == ("ALTAKyaml", "yamlsecret")
    assert (config.region, config.pool, config.queue) == ("cn-gz", "cce-yaml", "default")


def test_project_config_beats_home_config(tmp_path, monkeypatch):
    home = tmp_path / "home" / ".aihc"
    home.mkdir(parents=True)
    (home / "config.json").write_text(
        json.dumps({"defaultpool": "cce-home", "defaultqueue": "q-home"}), encoding="utf-8"
    )
    project = tmp_path / ".aihc"
    project.mkdir()
    (project / "config.json").write_text(json.dumps({"defaultpool": "cce-project"}), encoding="utf-8")

    config = load_config()
    assert config.pool == "cce-project"
    assert config.queue == "q-home"  # home still fills fields the project omits


def test_missing_explicit_config_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(config_path=str(tmp_path / "nope.json"))


def test_require_helpers_have_actionable_messages():
    config = Config()
    with pytest.raises(ConfigError, match="AIHC_AK"):
        config.require_credentials()
    with pytest.raises(ConfigError, match="aihc-serverless"):
        config.require_pool()
    with pytest.raises(ConfigError, match="queue"):
        config.require_queue()


def test_redacted_never_leaks_the_secret_key():
    config = Config(access_key="ALTAKabcdefgh", secret_key="supersecretvalue")
    info = config.redacted()
    assert "supersecretvalue" not in json.dumps(info)
    assert info["secret_key"].endswith("*" * 8)


def test_write_config_merges_and_chmods(tmp_path):
    path = write_config(tmp_path / "config.json", access_key="ak1", secret_key="sk1", region="cn-bj")
    assert json.loads(path.read_text())["credentials"] == {"accesskey": "ak1", "secretkey": "sk1"}
    assert path.stat().st_mode & 0o777 == 0o600

    write_config(path, pool="cce-x")
    data = json.loads(path.read_text())
    assert data["defaultpool"] == "cce-x"
    assert data["credentials"]["accesskey"] == "ak1"  # preserved


# ------------------------------------------------------------------ .env file


def test_env_file_parsing_handles_quotes_comments_and_export():
    from aihc_job.config import parse_env_file

    values = parse_env_file(
        "\n".join(
            [
                "# a comment",
                "",
                "AIHC_AK=ALTAK123",
                "export AIHC_SK=secret",
                'AIHC_IMAGE="registry/img:tag"',
                "AIHC_OWNER='someone'",
                "AIHC_WORKDIR=/share/someone   # trailing comment",
                'AIHC_MULTI="one\\ntwo"',
                "AIHC_EMPTY=",
                "not a pair",
            ]
        )
    )
    assert values == {
        "AIHC_AK": "ALTAK123",
        "AIHC_SK": "secret",
        "AIHC_IMAGE": "registry/img:tag",
        "AIHC_OWNER": "someone",
        "AIHC_WORKDIR": "/share/someone",
        "AIHC_MULTI": "one\ntwo",
        "AIHC_EMPTY": "",
    }


def test_dollar_signs_in_env_values_are_left_alone():
    """These values land in job commands, where $RANK belongs to the remote shell."""
    from aihc_job.config import parse_env_file

    values = parse_env_file("AIHC_CMD=torchrun --node_rank $RANK --addr ${MASTER_ADDR}")
    assert values["AIHC_CMD"] == "torchrun --node_rank $RANK --addr ${MASTER_ADDR}"


def test_env_file_fills_config_and_a_real_export_wins(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AIHC_AK=from-env-file\nAIHC_SK=sk\nAIHC_QUEUE=queue-from-file\n"
        "AIHC_POOL=aihc-serverless\nAIHC_QUEUE_POOL=aihc-real\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AIHC_ENV_FILE", str(env_file))
    monkeypatch.setenv("AIHC_QUEUE", "queue-from-export")

    config = load_config()
    assert config.access_key == "from-env-file"
    assert config.queue_pool == "aihc-real"
    assert config.queue == "queue-from-export"  # a one-off export must not need an edit
    assert str(env_file) in config.sources


def test_env_file_beats_the_home_config_file(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".aihc").mkdir(parents=True)
    (home / ".aihc" / "config.json").write_text(
        json.dumps({"credentials": {"accesskey": "from-home", "secretkey": "sk"},
                    "defaultqueue": "home-queue"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    env_file = tmp_path / ".env"
    env_file.write_text("AIHC_QUEUE=env-file-queue\n", encoding="utf-8")
    monkeypatch.setenv("AIHC_ENV_FILE", str(env_file))

    config = load_config()
    assert config.queue == "env-file-queue"
    assert config.access_key == "from-home"  # merges per field, does not shadow the file


def test_env_file_is_discovered_in_cwd_then_repo_root(monkeypatch, tmp_path):
    from aihc_job import config as config_module
    from aihc_job.config import find_env_file

    monkeypatch.delenv("AIHC_ENV_FILE", raising=False)
    repo, work = tmp_path / "repo", tmp_path / "work"
    repo.mkdir()
    work.mkdir()
    monkeypatch.setattr(config_module, "PACKAGE_ROOT", repo)
    monkeypatch.chdir(work)

    assert find_env_file() is None
    (repo / ".env").write_text("AIHC_QUEUE=repo\n", encoding="utf-8")
    assert find_env_file() == repo / ".env"  # found from any working directory
    (work / ".env").write_text("AIHC_QUEUE=cwd\n", encoding="utf-8")
    assert find_env_file() == work / ".env"  # ...but cwd wins


def test_a_named_env_file_that_does_not_exist_is_an_error(monkeypatch, tmp_path):
    """Falling back after a typo would silently submit with the wrong queue or image."""
    from aihc_job.config import find_env_file

    (tmp_path / ".env").write_text("AIHC_QUEUE=fallback\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="--env-file"):
        find_env_file(tmp_path / "missing.env")
    monkeypatch.setenv("AIHC_ENV_FILE", str(tmp_path / "missing.env"))
    with pytest.raises(ConfigError, match=r"\$AIHC_ENV_FILE"):
        find_env_file()


def test_template_variables_merge_env_file_under_the_environment(monkeypatch, tmp_path):
    from aihc_job.config import template_variables

    env_file = tmp_path / ".env"
    env_file.write_text("AIHC_IMAGE=from-file\nAIHC_OWNER=someone\n", encoding="utf-8")
    monkeypatch.setenv("AIHC_ENV_FILE", str(env_file))
    monkeypatch.setenv("AIHC_IMAGE", "from-export")

    variables = template_variables()
    assert variables["AIHC_IMAGE"] == "from-export"
    assert variables["AIHC_OWNER"] == "someone"
