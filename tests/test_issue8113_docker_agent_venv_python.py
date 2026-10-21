"""Regression tests for issue #8113: the Docker venv was built on the base
image interpreter (3.12), so every agent dependency gated on
``python_version >= '3.14'`` was silently dropped and ``run_agent`` failed to
import with the container still reporting healthy.

The chokepoint is the venv decision in ``docker_init.bash``. The decision logic
lives in ``scripts/docker_venv_python.py`` so it is unit-testable without
Docker; these tests cover the fresh, reused-incompatible, reused-compatible and
no-agent-source paths, plus the ``.deps_installed`` interaction that makes a
restart skip the reinstall entirely.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO / "scripts" / "docker_venv_python.py"

_spec = importlib.util.spec_from_file_location("docker_venv_python", HELPER_PATH)
dvp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dvp)

# A pyproject shaped like the agent that triggered #8113: `requires-python`
# still admits 3.11, but the core dependency set is gated on 3.14.
AGENT_PYPROJECT_314 = """
[project]
name = "hermes-agent"
requires-python = ">=3.11,<3.15"
dependencies = [
  "python-dotenv==1.2.2; python_version >= '3.14'",
  "ruamel.yaml==0.18.16; python_version >= '3.14'",
  "pydantic==2.13.4; python_version >= '3.14'",
  "click>=8.1",
]

[project.optional-dependencies]
all = ["mcp>=2.0", "fastapi>=0.133"]
"""

# The pre-#8113 agent: no 3.14 gates, so any interpreter `requires-python`
# admits is fine and the base image must be left alone.
AGENT_PYPROJECT_LEGACY = """
[project]
name = "hermes-agent"
requires-python = ">=3.11"
dependencies = ["python-dotenv==1.1.0", "pydantic==2.9.0"]
"""


def _make_venv(path: Path, version_info: str) -> Path:
    venv = path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text(
        f"home = /usr/local/bin\nversion_info = {version_info}\n", encoding="utf-8"
    )
    return venv


def _agent_dir(tmp_path: Path, pyproject: str) -> Path:
    agent = tmp_path / "hermes-agent"
    agent.mkdir()
    (agent / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return agent


# ── required-version derivation ──────────────────────────────────────────────


def test_required_version_comes_from_the_highest_dependency_gate():
    """The 3.14 gates are the authoritative signal, not `requires-python`.

    `requires-python = ">=3.11,<3.15"` would wrongly admit the base image's
    3.12, which is exactly how the 33 core deps were dropped.
    """
    assert dvp.required_agent_python(AGENT_PYPROJECT_314) == (3, 14)


def test_required_version_falls_back_to_requires_python_upper_bound():
    """With no version gates, honour the agent's own ceiling."""
    pyproject = '[project]\nname="x"\nrequires-python = ">=3.11,<3.14"\ndependencies = ["click"]\n'
    assert dvp.required_agent_python(pyproject) == (3, 13)


def test_required_version_is_none_without_any_constraint():
    """No gates and no usable ceiling must not invent a version."""
    pyproject = '[project]\nname="x"\nrequires-python = ">=3.11"\ndependencies = ["click"]\n'
    assert dvp.required_agent_python(pyproject) is None


def test_unparseable_pyproject_fails_closed_to_none():
    """Malformed input must not be guessed at — caller keeps the base image."""
    assert dvp.required_agent_python("this is not toml {{{") is None


# ── venv plan: fresh, reused-incompatible, reused-compatible ─────────────────


def test_fresh_container_selects_the_gate_version_not_the_base_image(tmp_path):
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = tmp_path / "venv"

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "create"
    assert plan["python"] == (3, 14)


def test_reused_incompatible_venv_is_rebuilt(tmp_path):
    """The restart trap: a 3.12 venv with `.deps_installed` must be rebuilt.

    This is the case the maintainer asked for explicitly — a restart otherwise
    reuses the 3.12 venv *and* skips the install because `.deps_installed` is
    present, pinning the broken interpreter forever.
    """
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = _make_venv(tmp_path, "3.12.15")
    (venv_dir / ".deps_installed").write_text("", encoding="utf-8")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "rebuild"
    assert plan["python"] == (3, 14)
    assert plan["clear_deps_marker"] is True


def test_reused_compatible_venv_still_takes_the_fast_path(tmp_path):
    """A venv already on the required version must not be rebuilt.

    Guards against a fix that rebuilds on every restart and makes container
    restarts pay a full 100+ package reinstall.
    """
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = _make_venv(tmp_path, "3.14.8")
    (venv_dir / ".deps_installed").write_text("", encoding="utf-8")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "reuse"
    assert plan["clear_deps_marker"] is False


def test_reused_venv_on_a_newer_compatible_version_is_still_reused(tmp_path):
    """Only an *incompatible* interpreter forces a rebuild.

    `requires-python = ">=3.11,<3.15"` admits any 3.x above the gate, so a
    3.15+ venv must not be torn down on the strength of an exact match.
    """
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = _make_venv(tmp_path, "3.15.1")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "reuse"


def test_legacy_agent_leaves_the_base_image_alone(tmp_path):
    """No 3.14 gate must not pin an interpreter — avoid a needless 3.14 fetch."""
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_LEGACY)
    venv_dir = _make_venv(tmp_path, "3.12.15")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["python"] is None
    assert plan["action"] == "reuse"


def test_missing_agent_source_falls_back_to_the_base_image(tmp_path):
    """No staged agent (single-container / dev) must keep today's behaviour."""
    venv_dir = _make_venv(tmp_path, "3.12.15")

    plan = dvp.plan_agent_venv(tmp_path / "nope" / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["python"] is None
    assert plan["action"] == "reuse"


def test_no_venv_yet_creates_on_the_gate_version(tmp_path):
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", tmp_path / "absent", base_python=(3, 12))

    assert plan["action"] == "create"
    assert plan["python"] == (3, 14)


def test_venv_without_pyvenv_cfg_is_treated_as_incompatible(tmp_path):
    """A structurally broken venv must be rebuilt, not reused and crash later."""
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = tmp_path / "venv"
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "bin" / "python3").write_text("", encoding="utf-8")

    plan = dvp.plan_agent_venv(agent / "pyproject.toml", venv_dir, base_python=(3, 12))

    assert plan["action"] == "rebuild"


# ── the CLI docker_init.bash actually calls ──────────────────────────────────


def test_cli_prints_the_planned_python_for_the_shell(tmp_path):
    """`docker_init.bash` consumes stdout, so the contract is a real value."""
    agent = _agent_dir(tmp_path, AGENT_PYPROJECT_314)
    venv_dir = tmp_path / "venv"
    venv_dir.mkdir()
    (venv_dir / "bin").mkdir()

    result = subprocess.run(
        [sys.executable, str(HELPER_PATH), str(agent / "pyproject.toml"), str(venv_dir), "3.12"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "create 3.14"


def test_cli_is_silent_when_no_agent_is_staged(tmp_path):
    """A missing agent must not fail the container — plain no-version output."""
    venv_dir = _make_venv(tmp_path, "3.12.15")

    result = subprocess.run(
        [sys.executable, str(HELPER_PATH), str(tmp_path / "nope.toml"), str(venv_dir), "3.12"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reuse -"


# ── docker_init.bash wiring ──────────────────────────────────────────────────

INIT_SH = (REPO / "docker_init.bash").read_text(encoding="utf-8")


def test_docker_init_resolves_the_venv_plan_before_creating_the_venv():
    """The decision must precede `uv venv`, or the base image wins by default."""
    plan_pos = INIT_SH.find("docker_venv_python.py")
    # Match the real invocation, not the explanatory comment that also names it.
    create_pos = INIT_SH.find("uv venv venv")
    assert plan_pos != -1, "docker_init.bash must consult scripts/docker_venv_python.py"
    assert create_pos != -1, "uv venv invocation not found"
    assert plan_pos < create_pos, "interpreter selection must run before venv creation"


def test_docker_init_passes_the_requested_version_to_uv():
    """`uv venv` alone cannot pick a non-base interpreter."""
    assert "UV_PYTHON=" in INIT_SH or "--python" in INIT_SH


def test_docker_init_rebuild_path_drops_the_deps_marker():
    """Without removing `.deps_installed` the rebuild skips its own install."""
    plan_pos = INIT_SH.find("docker_venv_python.py")
    rebuild_pos = INIT_SH.find('if [ "$_venv_action" = "rebuild" ]; then')
    marker_pos = INIT_SH.find("rm -f /app/venv/.deps_installed")
    assert rebuild_pos != -1, "no rebuild branch — a reused 3.12 venv would stay broken"
    assert marker_pos != -1, "rebuild path must remove the fast-restart marker"
    assert plan_pos < rebuild_pos, "the plan must be computed before the rebuild branch"
    assert rebuild_pos < marker_pos, "the marker must be removed inside the rebuild branch"