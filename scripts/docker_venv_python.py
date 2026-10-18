#!/usr/bin/env python3
"""Decide which interpreter the Docker agent venv must use (#8113).

The WebUI image ships a Python 3.12 base, but the agent it stages gates its
core dependency set on ``python_version >= '3.14'``. A bare ``uv venv`` inherits
the base image interpreter, every one of those markers evaluates false, and the
agent silently fails to import while the container still reports healthy.

Two things have to line up for the fix to hold, so this module answers both
questions in one place:

1. *Which* interpreter does the staged agent actually require? The answer comes
   from the highest ``python_version >= '3.x'`` gate across its dependencies —
   ``requires-python`` alone is not authoritative, because the agent still
   declares ``>=3.11`` while gating its core deps on 3.14.
2. *Does the existing venv still satisfy that?* A restart reuses
   ``/app/venv`` and skips the install when ``.deps_installed`` is present, so a
   venv on the wrong interpreter has to be rebuilt explicitly — otherwise it
   pins the broken state across every subsequent restart.

Contract with ``docker_init.bash``: stdout is exactly ``"<action> <python>"``
where action is ``create`` / ``reuse`` / ``rebuild`` and python is
``major.minor`` or ``-``. Anything unreadable or unparseable fails closed to
``reuse -`` so a staging problem never blocks container startup.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:  # Python 3.11+ (the base image is 3.12); degrade to regex-only below.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on <3.11
    tomllib = None  # type: ignore[assignment]

# `python_version >= '3.14'` inside a PEP 508 environment marker.
_GATE_RE = re.compile(r"python_version\s*>=\s*['\"](\d+)\.(\d+)['\"]")
# `requires-python = ">=3.11,<3.15"` — the `<\` bound is exclusive, so the
# highest admissible minor is one below it. `[^<>]*` allows the separating
# comma between the two bounds without swallowing a different operator.
_REQUIRES_RE = re.compile(r">=\s*(\d+)\.(\d+)[^<>]*<\s*(\d+)\.(\d+)")

DEPS_MARKER = ".deps_installed"

# `python-dotenv` is the first thing #8113 dropped: `run_agent` imports
# `hermes_cli.env_loader`, whose module scope does `import dotenv`. Probing it
# directly reports the dropped dependency instead of only its symptom
# (ModuleNotFoundError deep inside the agent's own import chain).
CRITICAL_IMPORT = "dotenv"
_PROBE_TIMEOUT = 60


def _dependency_strings(pyproject_text: str) -> list[str]:
    """Every PEP 508 requirement in the project, base plus optional extras."""
    if tomllib is not None:
        try:
            data = tomllib.loads(pyproject_text)
        except Exception:
            data = None
        if isinstance(data, dict):
            project = data.get("project")
            if isinstance(project, dict):
                reqs = list(project.get("dependencies") or [])
                optional = project.get("optional-dependencies")
                if isinstance(optional, dict):
                    for extra in optional.values():
                        reqs.extend(extra or [])
                return [str(r) for r in reqs]
    # Fallback: scan the raw text. The gate regex requires the literal
    # `python_version`, so the `requires-python` line cannot be misread as one.
    return [pyproject_text]


def _from_dependencies(pyproject_text: str) -> tuple[int, int] | None:
    best: tuple[int, int] | None = None
    for requirement in _dependency_strings(pyproject_text):
        for major, minor in _GATE_RE.findall(requirement):
            candidate = (int(major), int(minor))
            if best is None or candidate > best:
                best = candidate
    return best


def _from_requires_python(pyproject_text: str) -> tuple[int, int] | None:
    """Highest minor admitted by an exclusive upper bound, if one is declared."""
    match = _REQUIRES_RE.search(pyproject_text)
    if not match:
        return None
    upper_major, upper_minor = int(match.group(3)), int(match.group(4))
    return (upper_major, upper_minor - 1) if upper_minor > 0 else (upper_major, 0)


def required_agent_python(pyproject_text: str) -> tuple[int, int] | None:
    """Interpreter version the staged agent's dependencies require, or None.

    Dependency gates win over ``requires-python``: the agent declares
    ``requires-python = ">=3.11,<3.15"`` while gating its core set on 3.14, and
    trusting the former is precisely what produced the 3.12 venv in #8113.
    """
    try:
        gated = _from_dependencies(pyproject_text)
        if gated is not None:
            return gated
        return _from_requires_python(pyproject_text)
    except Exception:
        # Unknown input is not a licence to guess (GUIDELINES rule 3).
        return None


def venv_python_version(venv_dir: Path) -> tuple[int, int] | None:
    """Major.minor of an existing venv, from its ``pyvenv.cfg``.

    Returns None when the venv is absent or structurally unusable, which the
    caller treats as incompatible rather than as "no venv".
    """
    cfg = venv_dir / "pyvenv.cfg"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^\s*version_info\s*=\s*(\d+)\.(\d+)", text, re.MULTILINE)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def plan_agent_venv(
    pyproject_path: Path,
    venv_dir: Path,
    base_python: tuple[int, int],
) -> dict:
    """Decide whether to create, reuse or rebuild the Docker agent venv.

    ``base_python`` is the interpreter the venv would otherwise silently
    inherit; it is reported back for diagnostics only. ``clear_deps_marker``
    tells the caller to drop ``.deps_installed`` so a rebuilt venv actually
    reinstalls instead of inheriting the fast-restart skip.
    """
    required: tuple[int, int] | None = None
    try:
        if pyproject_path.is_file():
            required = required_agent_python(
                pyproject_path.read_text(encoding="utf-8", errors="replace")
            )
    except OSError:
        required = None

    has_venv = (venv_dir / "bin" / "python3").exists()

    if required is None:
        # Nothing authoritative to enforce: keep today's behaviour exactly.
        return {
            "action": "reuse" if has_venv else "create",
            "python": None,
            "clear_deps_marker": False,
            "required": None,
            "base_python": base_python,
        }

    action = "create"
    if has_venv:
        current = venv_python_version(venv_dir)
        # Only an interpreter older than the gate is incompatible; anything at
        # or above it is left alone (an operator may deliberately pin higher).
        action = "reuse" if current is not None and current >= required else "rebuild"

    return {
        "action": action,
        "python": required,
        "clear_deps_marker": action == "rebuild",
        "required": required,
        "base_python": base_python,
    }


def _venv_python_exe(venv_dir: Path) -> Path | None:
    for name in ("python3", "python"):
        candidate = venv_dir / "bin" / name
        if candidate.exists():
            return candidate
    win = venv_dir / "Scripts" / "python.exe"
    if win.exists():
        return win
    return None


def check_critical_imports(venv_dir: Path, expected: tuple[int, int] | None = None) -> tuple[bool, str]:
    """Import the agent's critical dependency with the venv's own interpreter.

    Returns ``(ok, detail)``; ``detail`` is empty on success and otherwise names
    the missing module together with the interpreter that was actually used, so
    an operator reading the log can tell a dropped dependency apart from any
    other import failure. Never raises — a broken probe must not be what stops a
    container from booting.
    """
    expected_txt = "the staged agent" if expected is None else "Python {}.{}".format(*expected)

    exe = _venv_python_exe(venv_dir)
    if exe is None:
        return False, (
            f"no interpreter found in {venv_dir}; cannot verify the {CRITICAL_IMPORT} import "
            f"required by {expected_txt}"
        )

    current = venv_python_version(venv_dir)
    current_txt = "an unknown Python" if current is None else "Python {}.{}".format(*current)

    try:
        result = subprocess.run(
            [str(exe), "-c", f"import {CRITICAL_IMPORT}"],
            cwd=str(venv_dir),
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except Exception as exc:  # timeout, EACCES, exec format, ...
        return False, (
            f"could not run the critical-import check with {current_txt} in {venv_dir}: "
            f"{type(exc).__name__}: {exc}"
        )

    if result.returncode == 0:
        return True, ""

    stderr = (result.stderr or "").strip().splitlines()
    tail = stderr[-1] if stderr else f"exit status {result.returncode}"
    return False, (
        f"the agent's critical dependency '{CRITICAL_IMPORT}' is not importable with "
        f"{current_txt} in {venv_dir}, but {expected_txt} requires it. "
        f"This is the #8113 signature: dependencies gated on a newer python_version were "
        f"dropped during install. {tail}"
    )


def _parse_version(text: str) -> tuple[int, int] | None:
    try:
        major, minor = text.split(".")[:2]
        return (int(major), int(minor))
    except (ValueError, AttributeError):
        return None


def _run_verify(argv: list[str]) -> int:
    """``docker_venv_python.py --verify <venv_dir> <expected>``.

    The exit status is the contract: 0 when the agent's critical dependency
    imports in that venv, non-zero with the reason on stdout otherwise.
    """
    if len(argv) != 4:
        print("usage: docker_venv_python.py --verify <venv_dir> <major.minor>", file=sys.stderr)
        return 2
    ok, detail = check_critical_imports(Path(argv[2]), _parse_version(argv[3]))
    if ok:
        return 0
    print(detail)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--verify":
        return _run_verify(argv)

    if len(argv) != 4:
        print("usage: docker_venv_python.py <pyproject> <venv_dir> <major.minor>", file=sys.stderr)
        return 2
    pyproject_path, venv_dir, base = Path(argv[1]), Path(argv[2]), argv[3]
    base_python = _parse_version(base) or (0, 0)

    plan = plan_agent_venv(pyproject_path, venv_dir, base_python)

    version = "-" if plan["python"] is None else "{}.{}".format(*plan["python"])
    if plan["action"] == "rebuild":
        current = venv_python_version(venv_dir)
        current_txt = (
            "an unreadable interpreter" if current is None else "Python {}.{}".format(*current)
        )
        print(
            "-- existing venv uses {} but the staged agent requires Python {}; rebuilding".format(
                current_txt, version
            ),
            file=sys.stderr,
        )
    elif plan["action"] == "create" and plan["python"] is not None:
        print(
            "-- no usable venv; creating on Python {} (base image interpreter is {}.{})".format(
                version, base_python[0], base_python[1]
            ),
            file=sys.stderr,
        )
    else:
        print("-- no interpreter constraint to enforce; leaving the venv as found", file=sys.stderr)

    print(f"{plan['action']} {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))