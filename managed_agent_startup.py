"""Initialize the WebUI's Agent source checkout before importing WebUI modules."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys


def _resolve_agent_dir(webui_root: Path) -> str | None:
    """The Agent root to activate: the exported one, else the one api.config uses.

    Direct launches (the README manual launch, a systemd unit that only sets
    PYTHONPATH, ``pip install -e``) export nothing. A PM-managed Agent
    (v0.21.6+) re-execs the process that first imports ``hermes_bootstrap``
    into its store Python under ``-I``, and that child has no third-party
    packages until the bootstrap runs again. The first import must therefore
    happen here, before ``api.profiles`` imports YAML, not later from
    ``api.streaming`` (#7843, #8529).

    * A non-blank ``HERMES_WEBUI_AGENT_DIR`` always wins, even when it names a
      directory without ``run_agent.py``: that is the browser-only signal and
      keeps this hook a no-op.
    * Unset, empty or whitespace-only falls back to
      ``api.startup.discover_runtime_agent_identity``, the exact discovery
      ``api.config`` runs, so startup cannot activate one Agent's dependency
      layer while the runtime imports another Agent's code. ``api.config``
      treats a blank value as unset too; if a blank value disabled activation,
      the runtime would still import the discovered Agent, only late, which is
      the #8529 crash.

    Discovery is best-effort: any failure leaves startup exactly as before.
    """
    explicit = os.environ.get("HERMES_WEBUI_AGENT_DIR", "").strip()
    if explicit:
        return str(Path(explicit).expanduser())
    try:
        # Stdlib-only (api.startup -> api.paths), so safe before activation.
        from api import startup as _startup

        found = _startup.discover_runtime_agent_identity(repo_root=webui_root).agent_dir
    except Exception:  # noqa: BLE001 - discovery is best-effort, never fatal here
        return None
    return str(found) if found else None


def activate_managed_agent() -> None:
    webui_root = str(Path(__file__).resolve().parent)
    # Discovery imports api.startup: make sure it is THIS checkout's package.
    if webui_root not in sys.path:
        sys.path.insert(0, webui_root)
    agent_dir = _resolve_agent_dir(Path(webui_root))
    if not agent_dir:
        return
    # Browser-only setups may deliberately point at an empty Agent directory.
    if not (Path(agent_dir) / "run_agent.py").is_file():
        return
    # Activate dependencies without importing the application: api.config must
    # select the active profile before Agent modules cache profile-sensitive paths.
    # Older Agents and browser-only shims have no bootstrap layer to activate; for
    # them leave sys.path alone so api.config appends the Agent dir at the END as
    # before (a front position lets `pip install -t .` packages in the checkout
    # shadow site-packages).
    if (Path(agent_dir) / "hermes_bootstrap.py").is_file():
        # The bootstrap may re-exec this process into the Agent's store Python,
        # which cannot re-discover an Agent this interpreter found through its
        # own site-packages (`pip install -e`, a .pth). Hand the child the same
        # root, as start.sh/bootstrap.py do, so it activates the same Agent.
        if not os.environ.get("HERMES_WEBUI_AGENT_DIR", "").strip():
            os.environ["HERMES_WEBUI_AGENT_DIR"] = agent_dir
        # Bootstrap's probe adds the checkout to its own PYTHONPATH, not ours.
        if agent_dir not in sys.path:
            sys.path.insert(1, agent_dir)
        try:
            importlib.import_module("hermes_bootstrap")
        except Exception as exc:  # noqa: BLE001 - SystemExit (relaunch/repair exit) still propagates
            # A broken Agent must not stop WebUI from starting: before this hook the
            # Agent import was lazy and an ImportError only disabled chat, leaving the
            # UI, diagnostics and updater reachable. Keep that behavior.
            print(
                f"[!!] Hermes Agent dependency activation failed: {type(exc).__name__}: {exc}; "
                "continuing startup. If WebUI then fails to import a dependency, run "
                "`hermes pm repair` or set HERMES_WEBUI_PYTHON.",
                file=sys.stderr,
                flush=True,
            )
