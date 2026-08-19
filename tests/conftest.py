"""Isolation shared by every test: no real credentials, no real `.env`.

The repo's own `.env` is found from any working directory (`config.PACKAGE_ROOT`), which
is what makes it useful in practice and exactly what must not reach the tests -- a filled
in `.env` would supply the very fields a test is checking the absence of. Machines here
also export unrelated `AIHC_*` variables, so those go too.
"""

from __future__ import annotations

import os

import pytest

from aihc_job import config


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch, tmp_path):
    for name in [key for key in os.environ if key.startswith("AIHC_")]:
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("AIHC_ENV_FILE", str(empty))
    # Belt and braces: also blind the repo-root fallback, for tests that drop the variable.
    monkeypatch.setattr(config, "PACKAGE_ROOT", tmp_path)
