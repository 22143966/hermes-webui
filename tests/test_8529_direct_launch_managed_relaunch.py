"""#8529: direct launches must survive a PM-managed Agent's isolated relaunch.

A PM-managed Agent (a `.git` checkout whose install stamp says
`updateMechanism: self`, Agent >= v0.21.6) re-execs any process that imports
`hermes_bootstrap` from a foreign interpreter into its store Python with
`python -I -c "...runpy/exec(<caller>)"`. The child re-runs the caller from the
top with no third-party packages until `hermes_bootstrap` is imported again.

`managed_agent_startup.activate_managed_agent()` makes that first import happen
before WebUI imports YAML, but only did so when `HERMES_WEBUI_AGENT_DIR` was
exported (start.sh/bootstrap.py do; a systemd unit with only PYTHONPATH, the
README's manual launch and `pip install -e` do not). Without it the first
import came from `api.streaming`, after `api.profiles` had imported YAML in the
parent, so the relaunched child died on `import yaml`.

The fake Agent below reproduces the mechanism: its bootstrap re-execs the caller
with `-I -S` (no site-packages, like the store Python) and only "activates" the
dependency path when it is imported again in the child.
"""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

WEBUI_ROOT = Path(__file__).resolve().parent.parent

_FAKE_BOOTSTRAP = textwrap.dedent(
    '''
    """Test double: PM-managed hermes_bootstrap + venv_sync.relaunch_command."""
    import json, os, sys
    _root = os.path.dirname(os.path.abspath(__file__))
    if os.environ.get("FAKE_PM_RELAUNCHED") != "1":
        os.environ["FAKE_PM_RELAUNCHED"] = "1"
        os.environ["FAKE_PM_SITE"] = json.dumps(
            [p for p in sys.path if p.rstrip(os.sep).endswith(("site-packages", "dist-packages"))]
        )
        body = sys.orig_argv[sys.orig_argv.index("-c") + 1]
        prefix = f"import sys, runpy; sys.path.insert(0, {_root!r}); sys.argv = {sys.argv!r}; "
        child_env = dict(os.environ)
        if os.environ.get("FAKE_PM_CHILD_DROPS_PYTHONPATH") == "1":
            # The store Python cannot see what the launching interpreter's own
            # site-packages (.pth, `pip install -e`) put on its path.
            child_env.pop("PYTHONPATH", None)
            child_env["HERMES_WEBUI_PYTHON"] = os.environ["FAKE_PM_MISSING_PYTHON"]
        os.execve(sys.executable, [sys.executable, "-I", "-S", "-c", prefix + f"exec({body!r})"], child_env)
    for _p in json.loads(os.environ["FAKE_PM_SITE"]):
        if _p not in sys.path:
            sys.path.append(_p)
    os.environ["FAKE_PM_ACTIVATED"] = "1"
    '''
)


def _fake_managed_agent(agent_dir: Path) -> Path:
    agent_dir.mkdir(parents=True)
    (agent_dir / "hermes_bootstrap.py").write_text(_FAKE_BOOTSTRAP, encoding="utf-8")
    (agent_dir / "run_agent.py").write_text(
        "import hermes_bootstrap\nclass AIAgent:\n    pass\n", encoding="utf-8"
    )
    (agent_dir / "pyproject.toml").write_text("[project]\nname = 'hermes-agent'\n", encoding="utf-8")
    return agent_dir


def _isolated_env(tmp_path: Path, **extra: str) -> dict:
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("HERMES_", "PYTHON", "FAKE_PM_"))
    }
    env.update(
        HOME=str(tmp_path / "home"),
        HERMES_HOME=str(tmp_path / "hermes-home"),
        HERMES_WEBUI_STATE_DIR=str(tmp_path / "state"),
        HERMES_WEBUI_TEST_NETWORK_BLOCK="1",
        XDG_DATA_HOME=str(tmp_path / "xdg"),
        # No `hermes` launcher on PATH: discovery must not find a host Agent.
        PATH=os.pathsep.join([os.path.dirname(sys.executable), "/usr/bin", "/bin"]),
        # Discovery also asks the selected interpreter where `run_agent` lives.
        # The test interpreter may BE a real Agent's venv: never let a (possibly
        # regressed) hook find and run that Agent's bootstrap.
        HERMES_WEBUI_PYTHON=str(tmp_path / "no-python"),
        FAKE_PM_MISSING_PYTHON=str(tmp_path / "no-python"),
    )
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "hermes-home").mkdir(exist_ok=True)
    env.update(extra)
    return env


_IMPORT_SERVER = (
    "import os, sys\n"
    f"sys.path.insert(0, {str(WEBUI_ROOT)!r})\n"
    "import server\n"
    "print('SERVER_IMPORTED relaunched=%s activated=%s' % (\n"
    "    os.environ.get('FAKE_PM_RELAUNCHED'), os.environ.get('FAKE_PM_ACTIVATED')))\n"
)


@pytest.mark.parametrize(
    "shape", ["discovered", "discovered-cwd-agent", "pythonpath-only", "blank-agent-dir-env"]
)
def test_direct_launch_survives_isolated_relaunch_without_agent_dir_env(tmp_path, shape):
    if shape == "pythonpath-only":
        # systemd unit with Environment=PYTHONPATH=<agent> at a non-standard path (#8529 A)
        agent_dir = _fake_managed_agent(tmp_path / "opt" / "hermes-agent")
        # PYTHONPATH precedes site-packages, so the interpreter finds the fake.
        env = _isolated_env(tmp_path, PYTHONPATH=str(agent_dir), HERMES_WEBUI_PYTHON=sys.executable)
        cwd = WEBUI_ROOT
    else:
        # Agent at $HERMES_HOME/hermes-agent, nothing exported (README manual
        # launch from the Agent checkout, a bare systemd unit). A blank
        # HERMES_WEBUI_AGENT_DIR (`Environment=HERMES_WEBUI_AGENT_DIR=`) is unset
        # for api.config, so it must be unset for activation too.
        agent_dir = _fake_managed_agent(tmp_path / "hermes-home" / "hermes-agent")
        extra = {"HERMES_WEBUI_AGENT_DIR": "  "} if shape == "blank-agent-dir-env" else {}
        env = _isolated_env(tmp_path, **extra)
        cwd = agent_dir if shape == "discovered-cwd-agent" else WEBUI_ROOT
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_SERVER],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, (
        "WebUI died in the Agent's isolated relaunch: activation ran after a "
        f"third-party import.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr[-3000:]}"
    )
    assert "SERVER_IMPORTED relaunched=1 activated=1" in result.stdout, result.stdout


def test_relaunched_child_activates_the_agent_the_parent_discovered(tmp_path):
    """The launching interpreter found the Agent through its own import path; the
    Agent's store Python cannot. The child must get the parent's root handed over
    (as start.sh/bootstrap.py export it), not re-discover and come up empty."""
    agent_dir = _fake_managed_agent(tmp_path / "opt" / "hermes-agent")
    env = _isolated_env(
        tmp_path, PYTHONPATH=str(agent_dir), HERMES_WEBUI_PYTHON=sys.executable,
        FAKE_PM_CHILD_DROPS_PYTHONPATH="1",
    )
    script = _IMPORT_SERVER + "print('CHILD_AGENT_DIR=%s' % os.environ.get('HERMES_WEBUI_AGENT_DIR'))\n"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(WEBUI_ROOT), env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr[-3000:]}"
    )
    assert "SERVER_IMPORTED relaunched=1 activated=1" in result.stdout, result.stdout
    assert f"CHILD_AGENT_DIR={agent_dir.resolve()}" in result.stdout, result.stdout


def test_explicit_agent_dir_still_wins_over_discovery(tmp_path, monkeypatch):
    import managed_agent_startup

    _fake_managed_agent(tmp_path / "hermes-home" / "hermes-agent")
    explicit = tmp_path / "explicit-agent"
    explicit.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", f" {explicit} ")
    assert managed_agent_startup._resolve_agent_dir(WEBUI_ROOT) == str(explicit)


def test_startup_activation_and_api_config_resolve_the_same_agent(tmp_path):
    """Both a legacy ~/.hermes/hermes-agent and a different platform-default home
    (Windows: %LOCALAPPDATA%\\hermes) hold an Agent; HERMES_HOME is unset. The hook
    must activate the Agent api.config then runs against (#8355 review)."""
    legacy = _fake_managed_agent(tmp_path / "home" / ".hermes" / "hermes-agent")
    platform_default = tmp_path / "localappdata" / "hermes"
    current = _fake_managed_agent(platform_default / "hermes-agent")
    env = _isolated_env(tmp_path)
    env.pop("HERMES_HOME")
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(WEBUI_ROOT)!r})\n"
        "from pathlib import Path\n"
        "import api.paths as paths\n"
        f"paths._platform_default_hermes_home = lambda: Path({str(platform_default)!r})\n"
        "import managed_agent_startup as m\n"
        f"print('ACTIVATE', m._resolve_agent_dir(Path({str(WEBUI_ROOT)!r})))\n"
        "import api.config as config\n"
        "print('RUNTIME', config._AGENT_DIR)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=str(WEBUI_ROOT), env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    lines = dict(
        line.split(" ", 1) for line in result.stdout.splitlines()
        if line.startswith(("ACTIVATE", "RUNTIME"))
    )
    assert lines["ACTIVATE"] == lines["RUNTIME"] == str(current.resolve()), (lines, str(legacy))


def test_no_discoverable_agent_leaves_startup_untouched(tmp_path, monkeypatch):
    import managed_agent_startup
    from api import startup

    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    monkeypatch.setattr(
        startup, "discover_runtime_agent_identity",
        lambda **_kw: startup._AgentDiscovery(None),
    )
    before = list(sys.path)
    managed_agent_startup.activate_managed_agent()
    assert [p for p in sys.path if p not in before and p != str(WEBUI_ROOT)] == []


# ── Docker (docker_init.bash) ────────────────────────────────────────────────
# The single-container compose stack mounts the host's ~/.hermes, so discovery
# lands on the host's own Agent checkout. A git checkout there is a self-managed
# install to a PM-managed Agent, whose bootstrap would sync into the host's
# ~/.hermes/installs and re-exec into the host's store Python. docker_init must
# hand server.py the staged copy (no .git, the tree /app/venv was built from)
# in exactly that case, and leave the multi-container stacks alone.

_DOCKER_INIT = WEBUI_ROOT / "docker_init.bash"


def _docker_init_source() -> str:
    return _DOCKER_INIT.read_text(encoding="utf-8")


def _select_staged_agent_function() -> str:
    match = re.search(
        r"^select_staged_agent_for_git_checkout\(\) \{\n.*?^\}\n",
        _docker_init_source(), re.M | re.S,
    )
    assert match, "docker_init.bash must define select_staged_agent_for_git_checkout()"
    return match.group(0)


def _run_selection(tmp_path: Path, src: str, stage: str, agent_dir_env: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "HERMES_WEBUI_AGENT_DIR"}
    if agent_dir_env is not None:
        env["HERMES_WEBUI_AGENT_DIR"] = agent_dir_env
    script = (
        "set -e\n"
        + _select_staged_agent_function()
        + 'select_staged_agent_for_git_checkout "$1" "$2"\n'
        + 'bash -c \'printf "RESULT=%s\\n" "${HERMES_WEBUI_AGENT_DIR-<unset>}"\'\n'
    )
    result = subprocess.run(
        ["bash", "-c", script, "docker_init", src, stage],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1].removeprefix("RESULT=")


@pytest.fixture
def staged_agent(tmp_path):
    src = tmp_path / "mount" / "hermes-agent"
    src.mkdir(parents=True)
    (src / "run_agent.py").write_text("", encoding="utf-8")
    stage = tmp_path / "app" / "hermes-agent-src"
    stage.mkdir(parents=True)
    (stage / "run_agent.py").write_text("", encoding="utf-8")
    return src, stage


def test_docker_single_container_git_checkout_runs_the_staged_agent(tmp_path, staged_agent):
    src, stage = staged_agent
    (src / ".git").mkdir()
    assert _run_selection(tmp_path, str(src), str(stage), None) == str(stage)
    # A blank value is unset everywhere else too (api.config, activation).
    assert _run_selection(tmp_path, str(src), str(stage), "") == str(stage)


def test_docker_selection_leaves_every_other_topology_alone(tmp_path, staged_agent):
    src, stage = staged_agent
    # Two-/three-container: the agent image's source volume carries no .git.
    assert _run_selection(tmp_path, str(src), str(stage), None) == "<unset>"
    # No Agent mounted at all (browser-only container).
    assert _run_selection(tmp_path, "", str(stage), None) == "<unset>"
    (src / ".git").mkdir()
    # The operator's own choice always wins.
    assert _run_selection(tmp_path, str(src), str(stage), "/srv/agent") == "/srv/agent"
    # No usable stage (staging failed): keep today's behaviour.
    (stage / "run_agent.py").unlink()
    assert _run_selection(tmp_path, str(src), str(stage), None) == "<unset>"
    # A stage that still has .git would relaunch just the same: do not use it.
    (stage / "run_agent.py").write_text("", encoding="utf-8")
    (stage / ".git").mkdir()
    assert _run_selection(tmp_path, str(src), str(stage), None) == "<unset>"


def test_docker_init_selects_the_stage_before_launching_server():
    source = _docker_init_source()
    call = source.index('select_staged_agent_for_git_checkout "$_agent_src" "$_agent_stage"')
    launch = source.index("cd /app; python server.py")
    assert call < launch
    # Both names must refer to the tree the staging block writes, and that tree
    # must be copied without .git.
    assert re.search(r'^_agent_stage=/app/hermes-agent-src$', source, re.M)
    assert '_stage_src="/app/hermes-agent-src"' in source
    assert "--exclude='.git'" in source
    # The selection must not be skipped by the fast-restart path: it sits after
    # the dependency install / reuse decision, not inside install_venv_dependencies.
    assert call > source.index("ensure_hindsight_client_docker_dependency\n\n")
