"""#8113 acceptance coverage the interpreter fix alone does not provide.

The maintainer authorised a repair covering four areas: fresh *and reused*
incompatible venvs, interpreter selection, critical-import readiness, and
read-only staged-source handling. ``test_issue8113_docker_agent_venv_python.py``
covers the first two; this file covers the last two.

*Critical-import readiness* — the container used to start healthy and serve
HTTPS while ``run_agent`` was unimportable, so the only signal was a cosmetic
looking warning. Startup must now verify the critical import against the
interpreter actually in the venv and say so loudly when it fails.

*Read-only staged-source handling* — ``auto_install_agent_deps()`` targeted the
read-only agent mount (``_agent_dir()``), so under the documented ``:ro``
compose mount it died with ``Read-only file system`` and could never recover
the very breakage #8113 reports.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO / "scripts" / "docker_venv_python.py"

_spec = importlib.util.spec_from_file_location("docker_venv_python_readiness", HELPER_PATH)
dvp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dvp)

INIT_SH = (REPO / "docker_init.bash").read_text(encoding="utf-8")


# ── critical-import readiness ────────────────────────────────────────────────


def _fake_venv(tmp_path: Path, *, has_dotenv: bool) -> Path:
    """A venv-ish tree whose probe reports whether ``dotenv`` imports.

    ``dotenv`` is the dependency #8113 dropped first: ``run_agent`` ->
    ``hermes_cli.env_loader`` -> ``import dotenv`` at module scope, so its
    absence is the earliest observable symptom of the wrong interpreter.

    ``bin/python3`` is a real interpreter so the probe genuinely executes.

    The ``pyvenv.cfg`` must be *complete* or the fixture is not hermetic. A
    config without ``home`` leaves CPython unable to locate a base
    installation, so it falls back to the interpreter's own ``base_prefix``
    and keeps user site-packages enabled — the fake venv then inherits
    whatever the *host* happens to have installed. CI runners install
    ``python-dotenv``, so a ``has_dotenv=False`` venv would still import it,
    the dropped-dependency case would silently stop reproducing, and
    ``test_cli_verify_exits_nonzero_when_the_import_is_dropped`` would fail in
    CI while passing locally. ``home`` plus
    ``include-system-site-packages = false`` isolates the fixture; the module
    then goes in the venv's own ``site-packages``, which is where pip puts it.
    """
    major, minor = sys.version_info[:2]
    venv = tmp_path / "venv"
    site_packages = venv / "lib" / f"python{major}.{minor}" / "site-packages"
    site_packages.mkdir(parents=True)
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).parent}\n"
        "include-system-site-packages = false\n"
        f"version = {major}.{minor}.{sys.version_info.micro}\n"
        f"version_info = {major}.{minor}\n",
        encoding="utf-8",
    )
    (venv / "bin" / "python3").symlink_to(sys.executable)
    if has_dotenv:
        (site_packages / "dotenv.py").write_text("", encoding="utf-8")
    return venv


def test_fake_venv_is_hermetic_and_sees_no_ambient_packages(tmp_path):
    """Guard the fixture itself.

    If the fake venv can reach the host interpreter's site-packages, the
    dropped-dependency case stops reproducing and the tests below pass for the
    wrong reason. Assert the isolation directly rather than trusting it.
    """
    venv = _fake_venv(tmp_path, has_dotenv=False)
    result = subprocess.run(
        [str(venv / "bin" / "python3"), "-c",
         "import json,site,sys;"
         "print(json.dumps({'prefix': sys.prefix,"
         "'user_site': site.ENABLE_USER_SITE,"
         "'paths': sys.path}))"],
        cwd=str(venv),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    info = json.loads(result.stdout)
    assert info["prefix"] == str(venv), f"fixture did not become the prefix: {info}"
    assert info["user_site"] is False, f"user site-packages leaked in: {info}"
    foreign = [
        p for p in info["paths"]
        if "packages" in p and not p.startswith(str(venv))
    ]
    assert not foreign, f"ambient site-packages leaked into the fixture: {foreign}"


def test_readiness_passes_when_the_gated_dependency_imports(tmp_path):
    venv = _fake_venv(tmp_path, has_dotenv=True)
    ok, detail = dvp.check_critical_imports(venv, expected=(3, 14))
    assert ok is True
    assert detail == ""


def test_readiness_fails_and_names_the_missing_module(tmp_path):
    """The report must name the missing module, not just say "not ready"."""
    venv = _fake_venv(tmp_path, has_dotenv=False)
    ok, detail = dvp.check_critical_imports(venv, expected=(3, 14))
    assert ok is False
    assert "dotenv" in detail


def test_readiness_reports_the_interpreter_it_actually_used(tmp_path):
    """The whole fault is an interpreter mismatch — the report must show it.

    Asserted against the running interpreter rather than a literal ``3.12``:
    CI fans out 3.11/3.12/3.13 shards, and this fixture is a real interpreter,
    so a hardcoded version would fail on every shard but one.
    """
    venv = _fake_venv(tmp_path, has_dotenv=False)
    _, detail = dvp.check_critical_imports(venv, expected=(3, 14))
    major, minor = sys.version_info[:2]
    assert f"{major}.{minor}" in detail
    assert "3.14" in detail


def test_readiness_treats_a_missing_venv_as_not_ready(tmp_path):
    ok, detail = dvp.check_critical_imports(tmp_path / "absent", expected=(3, 14))
    assert ok is False
    assert detail != ""


def test_readiness_never_raises_on_a_broken_interpreter(tmp_path):
    """A venv with no runnable python must report, not crash the container."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("version_info = 3.12.15\n", encoding="utf-8")
    ok, detail = dvp.check_critical_imports(venv, expected=(3, 14))
    assert ok is False
    assert isinstance(detail, str) and detail


def test_cli_verdict_is_printed_for_the_shell(tmp_path):
    """`docker_init.bash` gates on the exit status, so it must be real."""
    venv = _fake_venv(tmp_path, has_dotenv=True)
    result = subprocess.run(
        [sys.executable, str(HELPER_PATH), "--verify", str(venv), "3.14"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_cli_verify_exits_nonzero_when_the_import_is_dropped(tmp_path):
    """The shell branch keys on this status; a green exit would hide the fault."""
    venv = _fake_venv(tmp_path, has_dotenv=False)
    result = subprocess.run(
        [sys.executable, str(HELPER_PATH), "--verify", str(venv), "3.14"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "dotenv" in result.stdout + result.stderr


# ── docker_init.bash must run the readiness check ────────────────────────────


def test_docker_init_verifies_the_critical_import_after_install():
    assert "--verify" in INIT_SH, "startup must verify the agent's critical import"


def test_docker_init_readiness_check_runs_after_the_venv_is_active():
    """Checking before activation would probe the base interpreter, not the venv."""
    activate_pos = INIT_SH.find("source /app/venv/bin/activate")
    verify_pos = INIT_SH.find("--verify")
    assert activate_pos != -1 and verify_pos != -1
    assert verify_pos > activate_pos, "readiness must be checked with the venv active"


def test_docker_init_readiness_runs_after_the_dependency_install():
    """Checking before the install would report the pre-install state."""
    install_pos = INIT_SH.find("uv pip install -e")
    verify_pos = INIT_SH.find("--verify")
    assert install_pos != -1, "agent editable install not found"
    assert verify_pos > install_pos, "readiness must be checked after the install"


# ── read-only staged-source handling ─────────────────────────────────────────


@pytest.fixture(params=[0o022, 0o002], ids=["umask022", "umask002"])
def ambient_umask(request):
    """Run a positive fixture under both common umasks.

    CI and most hosts use 0022, but 0002 (per-user groups) is common too and
    makes a plain ``mkdir`` 0775 — which the real trust check correctly rejects
    as group-writable. Positive fixtures must not depend on which one is set.
    """
    previous = os.umask(request.param)
    try:
        yield request.param
    finally:
        os.umask(previous)


def _agent(tmp_path: Path, name: str, *, reqs: bool = True, mode: int = 0o755) -> Path:
    """An Agent-shaped source directory with an explicit, trusted mode."""
    d = tmp_path / name
    d.mkdir()
    d.chmod(mode)
    if reqs:
        (d / "requirements.txt").write_text("python-dotenv==1.2.2\n", encoding="utf-8")
    else:
        (d / "pyproject.toml").write_text('[project]\nname="hermes-agent"\n', encoding="utf-8")
    return d


def _install_dir_prefers_staged_copy(tmp_path):
    """Export the chokepoint this test drives."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _agent(tmp_path, "hermes-agent-src")
    env = {"HERMES_WEBUI_AGENT_STAGE_DIR": str(staged)}
    with patch.dict("os.environ", env, clear=False):
        return _agent_install_dir(mount)


def test_auto_install_targets_the_writable_staged_copy(tmp_path, ambient_umask):
    """The `:ro` mount can never satisfy the install that would fix #8113."""
    assert _install_dir_prefers_staged_copy(tmp_path).name == "hermes-agent-src"


def test_auto_install_still_rejects_a_group_writable_stage(tmp_path):
    """Explicit fixture modes must not paper over the production trust check."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _agent(tmp_path, "hermes-agent-src", mode=0o775)
    env = {"HERMES_WEBUI_AGENT_STAGE_DIR": str(staged)}
    with patch.dict("os.environ", env, clear=False):
        assert _agent_install_dir(mount) == mount


def test_auto_install_falls_back_to_the_mount_without_a_stage_dir(tmp_path):
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    with patch.dict("os.environ", {"HERMES_WEBUI_AGENT_STAGE_DIR": ""}, clear=False):
        assert _agent_install_dir(mount) == mount


def test_auto_install_ignores_a_staged_path_that_does_not_exist(tmp_path):
    """A stale env var must not redirect the install into nowhere."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    env = {"HERMES_WEBUI_AGENT_STAGE_DIR": str(tmp_path / "gone")}
    with patch.dict("os.environ", env, clear=False):
        assert _agent_install_dir(mount) == mount


def test_auto_install_ignores_a_staged_path_that_fails_the_trust_check(tmp_path):
    """Fail closed: an untrusted staged path must not become the install target."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _agent(tmp_path, "hermes-agent-src")
    env = {"HERMES_WEBUI_AGENT_STAGE_DIR": str(staged)}
    with patch.dict("os.environ", env, clear=False):
        with patch("api.startup._trusted_agent_dir", return_value=False):
            assert _agent_install_dir(mount) == mount


def test_auto_install_runs_pip_against_the_staged_copy(tmp_path):
    """End-to-end through auto_install_agent_deps, not just the helper."""
    from api.startup import auto_install_agent_deps

    mount = _agent(tmp_path, "hermes-agent")
    staged = _agent(tmp_path, "hermes-agent-src")
    env = {
        "HERMES_WEBUI_AGENT_DIR": str(mount),
        "HERMES_WEBUI_AGENT_STAGE_DIR": str(staged),
        "HERMES_WEBUI_AUTO_INSTALL": "1",
    }
    with patch.dict("os.environ", env, clear=False):
        with patch("api.startup._trusted_agent_dir", return_value=True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="")
                assert auto_install_agent_deps(expected_agent_dir=mount) is True
    args = mock_run.call_args[0][0]
    assert any(str(staged) in str(a) for a in args), f"install did not target the staged copy: {args}"


def test_docker_init_exports_the_staged_dir_for_the_recovery_path():
    assert "HERMES_WEBUI_AGENT_STAGE_DIR" in INIT_SH


def test_docker_init_exports_the_stage_dir_before_the_server_starts():
    """`docker_init.bash` exports it; `server.py` consumes it on failure."""
    export_pos = INIT_SH.find("HERMES_WEBUI_AGENT_STAGE_DIR")
    server_pos = INIT_SH.find("python server.py")
    assert export_pos != -1, "staged dir must be exported to the recovery path"
    assert server_pos != -1
    assert export_pos < server_pos