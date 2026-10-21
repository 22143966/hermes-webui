"""#8113: a venv rebuild must never leave the container without a working venv.

When the staged Agent needs an interpreter the existing ``/app/venv`` cannot
provide, ``docker_init.bash`` rebuilds it. It used to ``rm -rf /app/venv`` first
and only then ask uv for the new interpreter, so an offline container (or one
with ``UV_PYTHON_DOWNLOADS=never``) turned a WebUI that still booted into a
failed start. ``rebuild_agent_venv`` moves the old venv aside, deletes it only
once the replacement exists, and puts it back when uv fails.

These tests run the real bash function from ``docker_init.bash`` against a fake
``uv`` on ``PATH``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INIT_SH = (REPO / "docker_init.bash").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is needed to run docker_init.bash code")

FAKE_UV = """#!/bin/bash
# fake uv: only `uv venv <dir>` is supported
[ "$1" = venv ] || exit 64
printf '%s\\n' "${UV_PYTHON:--}" >> "$FAKE_UV_LOG"
mkdir -p "$2"
if [ "${FAKE_UV_FAIL:-0}" = 1 ]; then
  echo partial > "$2/partial"
  exit 1
fi
echo new > "$2/marker"
"""


def _bash_function(src: str, name: str) -> str:
    start = src.index(f"{name}() {{")
    end = src.index("\n}\n", start) + len("\n}\n")
    return src[start:end]


def _old_venv(root: Path) -> Path:
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").write_text("old interpreter\n", encoding="utf-8")
    (venv / "marker").write_text("old\n", encoding="utf-8")
    (venv / ".deps_installed").write_text("", encoding="utf-8")
    return venv


def _run(tmp_path: Path, venv: Path, python: str, *, fail: bool) -> tuple[int, str]:
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    uv = bindir / "uv"
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(0o755)
    log = tmp_path / "uv.log"
    script = (
        "set -e\n"
        + _bash_function(INIT_SH, "rebuild_agent_venv")
        + '\nrc=0\nrebuild_agent_venv "$1" "$2" || rc=$?\necho "rc=$rc"\n'
    )
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_UV_LOG": str(log),
        "FAKE_UV_FAIL": "1" if fail else "0",
        "HOME": str(tmp_path),
    }
    out = subprocess.run(
        ["bash", "-c", script, "bash", str(venv), python],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=True,
    )
    rc = int(out.stdout.strip().rsplit("rc=", 1)[1])
    return rc, log.read_text(encoding="utf-8") if log.exists() else ""


def test_successful_rebuild_replaces_the_venv_and_removes_the_previous_copy(tmp_path):
    venv = _old_venv(tmp_path)
    rc, log = _run(tmp_path, venv, "3.14", fail=False)
    assert rc == 0
    assert (venv / "marker").read_text(encoding="utf-8") == "new\n"
    assert not (venv / ".deps_installed").exists(), "a rebuilt venv must reinstall its dependencies"
    assert not Path(f"{venv}.previous").exists()
    assert log.splitlines() == ["3.14"], "the rebuild must name the selected interpreter"


def test_failed_rebuild_restores_the_working_venv(tmp_path):
    venv = _old_venv(tmp_path)
    rc, log = _run(tmp_path, venv, "3.14", fail=True)
    assert rc == 1
    assert (venv / "marker").read_text(encoding="utf-8") == "old\n"
    assert (venv / "bin" / "python3").read_text(encoding="utf-8") == "old interpreter\n"
    assert (venv / ".deps_installed").exists(), "the restored venv keeps its fast-restart marker"
    assert not (venv / "partial").exists(), "nothing from the failed attempt may remain"
    assert not Path(f"{venv}.previous").exists()
    assert log.splitlines() == ["3.14"]


def test_a_stale_previous_copy_never_swallows_the_venv(tmp_path):
    venv = _old_venv(tmp_path)
    stale = Path(f"{venv}.previous")
    (stale / "venv").mkdir(parents=True)
    (stale / "leftover").write_text("stale\n", encoding="utf-8")
    rc, _ = _run(tmp_path, venv, "3.14", fail=True)
    assert rc == 1
    assert (venv / "marker").read_text(encoding="utf-8") == "old\n"
    assert not (venv / "venv").exists()
    assert not stale.exists()


def test_rebuild_without_a_named_interpreter_uses_uv_default(tmp_path):
    venv = _old_venv(tmp_path)
    rc, log = _run(tmp_path, venv, "-", fail=False)
    assert rc == 0
    assert log.splitlines() == ["-"], "UV_PYTHON must not be set when no interpreter is selected"


def test_docker_init_rebuild_branch_keeps_the_venv_until_the_replacement_exists():
    branch_start = INIT_SH.index('if [ "$_venv_action" = "rebuild" ]; then')
    branch_end = INIT_SH.index("\nfi\n", branch_start)
    branch = INIT_SH[branch_start:branch_end]
    assert "rebuild_agent_venv /app/venv" in branch
    assert "rm -rf /app/venv" not in branch, "the rebuild branch must not delete the venv before uv has a new one"
    assert 'error_exit' in branch, "a rebuild that leaves no venv at all must still stop startup"
    # rc 1 keeps booting on the restored venv instead of stopping
    assert '_venv_action="reuse"' in branch
    assert INIT_SH.index("rebuild_agent_venv() {") < branch_start
