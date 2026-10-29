"""Regression tests for #6126: public-share image reads must be one file object.

``_embed_share_media`` used to resolve + allow-list a pathname, ``stat()`` it,
and then reopen the same pathname with ``read_bytes()``.  Those were three
separate filesystem lookups, so a concurrent writer inside an allowed root
could swap an intermediate directory (or the file itself) for a symlink to an
outside location between validation and read, and the public snapshot would
embed the OUTSIDE bytes.

The fix walks from an opened allowed-root directory descriptor one component
at a time with ``O_NOFOLLOW`` and validates (``fstat``) and reads (bounded)
the SAME final descriptor.  Where that primitive is unavailable the share
embedder fails closed.

The race tests below are deterministic: they intercept every filesystem
primitive the embedder calls (``os.open/stat/lstat/fstat/read`` and
``io.open``) and perform the swap immediately before the N-th call, for EVERY
N observed in an unswapped dry run.  No timing, no threads.
"""

from __future__ import annotations

import base64
import contextlib
import io
import mimetypes
import os
import re
import threading
from pathlib import Path

import pytest

from api import shares

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX symlink / descriptor semantics"
)

# mimetypes lazily reads system mime.types files on first use; warm it so the
# FS-event counts below are identical between the dry run and the swap runs.
mimetypes.init()

PNG = b"\x89PNG\r\n\x1a\n"
INSIDE = PNG + b"INSIDE-ROOT-IMAGE" + b"\x00" * 32
OUTSIDE_MARK = b"OUTSIDE-SECRET-BYTES"
OUTSIDE = PNG + OUTSIDE_MARK + b"\x00" * 32

_DATA_RE = re.compile(r"data:image/[a-z]+;base64,([A-Za-z0-9+/=]+)")


def _payloads(out: str) -> list[bytes]:
    return [base64.b64decode(m) for m in _DATA_RE.findall(out)]


def _embed(text: str, roots) -> str:
    return shares._embed_share_media(text, allowed_roots=tuple(roots))


class _FsEvents:
    """Intercept the filesystem primitives the embedder can use.

    Records each call made by THIS thread (other threads in a shared pytest
    process pass straight through, so counts stay deterministic); when
    ``fire_at`` is set, runs ``action`` immediately BEFORE the
    ``fire_at``-th call executes (a deterministic race window).
    """

    OS_NAMES = ("open", "stat", "lstat", "fstat", "read")

    def __init__(self, fire_at: int | None = None, action=None):
        self.fire_at = fire_at
        self.action = action
        self.fired = False
        self.events: list[str] = []
        self.owner = threading.get_ident()

    def _wrap(self, label, real):
        def wrapper(*args, **kwargs):
            if threading.get_ident() != self.owner:
                return real(*args, **kwargs)
            index = len(self.events)
            self.events.append(label)
            if self.fire_at == index and not self.fired:
                self.fired = True
                self.action()
            return real(*args, **kwargs)

        return wrapper

    @contextlib.contextmanager
    def installed(self, monkeypatch):
        with monkeypatch.context() as mp:
            for name in self.OS_NAMES:
                real = getattr(os, name)
                wrapped = self._wrap(f"os.{name}", real)
                mp.setattr(os, name, wrapped)
                if name == "open" and real in os.supports_dir_fd:
                    # The wrapper forwards dir_fd= to the real os.open, so it
                    # genuinely supports it; keep the platform capability set
                    # truthful for the wrapped function.
                    mp.setattr(
                        os, "supports_dir_fd", set(os.supports_dir_fd) | {wrapped}
                    )
            mp.setattr(io, "open", self._wrap("io.open", io.open))
            yield self


def _build_tree(base: Path) -> dict:
    ws = base / "workspace"
    slot = ws / "slot"
    slot.mkdir(parents=True)
    (slot / "image.png").write_bytes(INSIDE)
    outside = base / "outside"
    outside.mkdir()
    (outside / "image.png").write_bytes(OUTSIDE)
    return {"ws": ws, "slot": slot, "outside": outside}


def _swap_dir(tree):
    def action():
        os.rename(tree["slot"], tree["ws"] / "slot.orig")
        os.symlink(tree["outside"], tree["slot"])

    return action


def _swap_file(tree):
    def action():
        os.rename(tree["slot"] / "image.png", tree["slot"] / "image.orig.png")
        os.symlink(tree["outside"] / "image.png", tree["slot"] / "image.png")

    return action


_SWAPS = {"intermediate-dir": _swap_dir, "final-file": _swap_file}


def _ref(kind: str, tree) -> str:
    if kind == "relative":
        return "MEDIA:slot/image.png"
    return f"MEDIA:{tree['ws']}/slot/image.png"


@pytest.mark.parametrize("ref_kind", ["relative", "absolute"])
@pytest.mark.parametrize("swap", sorted(_SWAPS))
def test_symlink_swap_at_every_fs_step_never_embeds_outside_bytes(
    tmp_path, monkeypatch, ref_kind, swap
):
    # Dry run: the unswapped tree embeds the in-root image (non-vacuous) and
    # tells us how many filesystem steps the embedder takes.
    dry_tree = _build_tree(tmp_path / "dry")
    dry = _FsEvents()
    with dry.installed(monkeypatch):
        out = _embed(_ref(ref_kind, dry_tree), [dry_tree["ws"]])
    assert _payloads(out) == [INSIDE], out[:200]
    assert dry.events, "embedder performed no filesystem steps"

    leaks = []
    for n in range(len(dry.events)):
        tree = _build_tree(tmp_path / f"run{n}")
        hook = _FsEvents(fire_at=n, action=_SWAPS[swap](tree))
        with hook.installed(monkeypatch):
            out = _embed(_ref(ref_kind, tree), [tree["ws"]])
        assert hook.fired, f"swap point {n} ({dry.events[n]}) never reached"
        payloads = _payloads(out)
        if any(OUTSIDE_MARK in p for p in payloads):
            leaks.append((n, dry.events[n]))
            continue
        # Either the original in-root file object or a fail-closed placeholder.
        assert out == shares._PLACEHOLDER or payloads == [INSIDE], (n, out[:200])
    assert not leaks, f"outside bytes embedded when swapped before: {leaks}"


def test_file_growing_past_limit_after_validation_is_never_embedded(
    tmp_path, monkeypatch
):
    limit = shares._SHARE_EMBED_MAX_BYTES

    def make(base: Path) -> Path:
        base.mkdir()
        (base / "grow.png").write_bytes(INSIDE)
        return base

    dry_root = make(tmp_path / "dry")
    dry = _FsEvents()
    with dry.installed(monkeypatch):
        out = _embed("MEDIA:grow.png", [dry_root])
    assert _payloads(out) == [INSIDE]

    oversized = []
    for n in range(len(dry.events)):
        root = make(tmp_path / f"run{n}")

        def grow(root=root):
            with open(root / "grow.png", "ab") as fh:
                fh.write(b"\x00" * (limit + 4096))

        hook = _FsEvents(fire_at=n, action=grow)
        with hook.installed(monkeypatch):
            out = _embed("MEDIA:grow.png", [root])
        assert hook.fired, f"growth point {n} ({dry.events[n]}) never reached"
        sizes = [len(p) for p in _payloads(out)]
        if any(size > limit for size in sizes):
            oversized.append((n, dry.events[n], max(sizes)))
            continue
        assert out == shares._PLACEHOLDER or _payloads(out) == [INSIDE], (n, out[:120])
    assert not oversized, f"embedded more than the byte cap: {oversized}"


# --- Static symlink components (no race) ------------------------------------


@pytest.fixture()
def tree(tmp_path):
    t = _build_tree(tmp_path)
    real = t["ws"] / "real"
    real.mkdir()
    (real / "img.png").write_bytes(INSIDE)
    (t["ws"] / "real.png").write_bytes(INSIDE)
    os.symlink("real", t["ws"] / "alias")  # in-root dir alias
    os.symlink("real.png", t["ws"] / "link.png")  # in-root file alias
    os.symlink(t["outside"], t["ws"] / "escape")  # dir escaping the root
    return t


@pytest.mark.parametrize(
    "ref",
    ["alias/img.png", "link.png", "escape/image.png"],
)
def test_symlinked_component_is_refused_relative(tree, ref):
    out = _embed(f"MEDIA:{ref}", [tree["ws"]])
    assert out == shares._PLACEHOLDER


@pytest.mark.parametrize(
    "ref",
    ["alias/img.png", "link.png", "escape/image.png"],
)
def test_symlinked_component_is_refused_absolute(tree, ref):
    out = _embed(f"MEDIA:{tree['ws']}/{ref}", [tree["ws"]])
    assert out == shares._PLACEHOLDER


def test_absolute_ref_through_outside_alias_of_root_is_refused(tree, tmp_path):
    os.symlink(tree["ws"], tmp_path / "ws-alias")
    out = _embed(f"MEDIA:{tmp_path}/ws-alias/real.png", [tree["ws"]])
    assert out == shares._PLACEHOLDER


def test_dotdot_component_is_refused_even_when_lexically_inside(tree):
    assert _embed("MEDIA:slot/../real.png", [tree["ws"]]) == shares._PLACEHOLDER
    assert (
        _embed(f"MEDIA:{tree['ws']}/slot/../real.png", [tree["ws"]])
        == shares._PLACEHOLDER
    )


def test_root_configured_through_symlink_is_still_served(tree, tmp_path):
    # The allowed root itself is trusted configuration: a workspace configured
    # through a symlinked spelling keeps working, for both spellings.
    os.symlink(tree["ws"], tmp_path / "ws-link")
    roots = [tmp_path / "ws-link"]
    for ref in (
        "MEDIA:real.png",
        f"MEDIA:{tmp_path}/ws-link/real.png",
        f"MEDIA:{tree['ws']}/real.png",
    ):
        assert _payloads(_embed(ref, roots)) == [INSIDE], ref


# --- Normal images keep working ---------------------------------------------


_FORMATS = {
    "a.png": (PNG + b"\x00" * 16, "image/png"),
    "b.jpg": (b"\xff\xd8\xff\xe0" + b"\x00" * 16, "image/jpeg"),
    "c.gif": (b"GIF89a" + b"\x00" * 16, "image/gif"),
    "d.webp": (b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 16, "image/webp"),
}


@pytest.mark.parametrize("name", sorted(_FORMATS))
def test_normal_images_are_embedded_byte_exact(tmp_path, name):
    data, mime = _FORMATS[name]
    ws = tmp_path / "ws"
    (ws / "deep" / "er").mkdir(parents=True)
    (ws / name).write_bytes(data)
    (ws / "deep" / "er" / name).write_bytes(data)
    for ref in (
        name,
        f"./{name}",
        f"deep/er/{name}",
        f"deep//er/./{name}",
        f"{ws}/{name}",
        f"{ws}/deep/er/{name}",
    ):
        out = _embed(f"MEDIA:{ref}", [ws])
        assert f"data:{mime};base64," in out, ref
        assert _payloads(out) == [data], ref
        assert f'alt="{name}"' in out, ref


def test_second_allowed_root_is_searched_for_relative_refs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    att = tmp_path / "attachments"
    att.mkdir()
    (att / "up.png").write_bytes(INSIDE)
    assert _payloads(_embed("MEDIA:up.png", [ws, att])) == [INSIDE]
    assert _payloads(_embed(f"MEDIA:{att}/up.png", [ws, att])) == [INSIDE]


def test_non_regular_files_are_refused_without_blocking(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "dir.png").mkdir()
    assert _embed("MEDIA:dir.png", [ws]) == shares._PLACEHOLDER

    fifo = ws / "pipe.png"
    os.mkfifo(fifo)
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.setdefault("out", _embed("MEDIA:pipe.png", [ws])),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=10)
    if worker.is_alive():
        # Release a reader blocked in open() so the daemon thread can exit.
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        worker.join(timeout=5)
        pytest.fail("opening a FIFO in an allowed root blocked the share builder")
    assert result["out"] == shares._PLACEHOLDER


# --- Empty roots / unsupported platform: fail closed, touch nothing ---------


def test_empty_allowed_roots_refuse_without_opening_anything(tree, monkeypatch):
    hook = _FsEvents()
    with hook.installed(monkeypatch):
        assert _embed("MEDIA:real.png", []) == shares._PLACEHOLDER
        assert _embed(f"MEDIA:{tree['ws']}/real.png", []) == shares._PLACEHOLDER
    assert not [e for e in hook.events if e.endswith("open")], hook.events


def _assert_refuses_without_opening(tree, monkeypatch):
    hook = _FsEvents()
    with hook.installed(monkeypatch):
        for ref in ("real.png", f"{tree['ws']}/real.png"):
            assert _embed(f"MEDIA:{ref}", [tree["ws"]]) == shares._PLACEHOLDER, ref
    assert not [e for e in hook.events if e.endswith("open")], hook.events


def test_platform_without_dir_fd_open_fails_closed(tree, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())
    _assert_refuses_without_opening(tree, monkeypatch)


@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_DIRECTORY"])
def test_platform_without_required_open_flag_fails_closed(tree, monkeypatch, flag):
    monkeypatch.delattr(os, flag)
    _assert_refuses_without_opening(tree, monkeypatch)


def test_capability_check_false_fails_closed(tree, monkeypatch):
    monkeypatch.setattr(
        shares, "_share_nofollow_open_supported", lambda: False, raising=False
    )
    _assert_refuses_without_opening(tree, monkeypatch)


def test_inline_data_uri_is_unaffected_by_unsupported_platform(monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())
    b64 = base64.b64encode(INSIDE).decode("ascii")
    out = _embed(f"MEDIA:data:image/png;base64,{b64}", [])
    assert _payloads(out) == [INSIDE]


# --- Descriptor hygiene -------------------------------------------------------


def _fds_into(base: Path) -> list[str]:
    """Open descriptors of this process that point into *base* (Linux)."""
    found = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target == str(base) or target.startswith(str(base) + os.sep):
            found.append(target)
    return found


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
def test_no_descriptor_leak_across_success_and_refusal_paths(tree, tmp_path):
    refs = [
        "slot/image.png",
        "alias/img.png",
        "link.png",
        "escape/image.png",
        "missing/none.png",
        "slot/missing.png",
        "slot",
        "real.png",
    ]
    assert _fds_into(tmp_path) == []
    for _ in range(25):
        for ref in refs:
            _embed(f"MEDIA:{ref}", [tree["ws"], tree["outside"]])
            _embed(f"MEDIA:{tree['ws']}/{ref}", [tree["ws"], tree["outside"]])
    assert _fds_into(tmp_path) == []


# --- Codex round 1 (#6126) ----------------------------------------------------
#
# R1-1  The allowed root is now opened from "/" one component at a time with
#       O_NOFOLLOW, so an ANCESTOR of a nested root (an attachments root inside
#       a writable workspace) swapped for a symlink after root resolution can no
#       longer redirect the root descriptor outside.


def _nested_root_tree(base: Path) -> dict:
    ws = base / "workspace"
    inbox = ws / "inbox"
    att = inbox / "attachments"
    att.mkdir(parents=True)
    (att / "img.png").write_bytes(INSIDE)
    outside = base / "outside"
    (outside / "attachments").mkdir(parents=True)
    (outside / "attachments" / "img.png").write_bytes(OUTSIDE)
    return {"ws": ws, "inbox": inbox, "att": att, "outside": outside}


def _swap_inbox(tree):
    def action():
        os.rename(tree["inbox"], tree["ws"] / "inbox.saved")
        os.symlink(tree["outside"], tree["inbox"])

    return action


def _nested_roots(kind: str, tree) -> list[Path]:
    return [tree["att"]] if kind == "attachments-only" else [tree["ws"], tree["att"]]


def _nested_ref(kind: str, tree) -> str:
    return "MEDIA:img.png" if kind == "relative" else f"MEDIA:{tree['att']}/img.png"


@pytest.mark.parametrize("ref_kind", ["relative", "absolute"])
@pytest.mark.parametrize("roots_kind", ["attachments-only", "workspace+attachments"])
def test_nested_root_ancestor_swap_after_resolution_never_embeds_outside_bytes(
    tmp_path, monkeypatch, ref_kind, roots_kind
):
    # Dry run: unswapped, the nested-root image embeds (non-vacuous) and we
    # learn every filesystem step.  Root resolution (realpath's lstat calls)
    # happens before the first os.open; a swap BEFORE resolution redirects
    # the trusted configuration itself (pre-existing policy, out of scope), so
    # the swap is applied at every step from the first open onwards.
    dry_tree = _nested_root_tree(tmp_path / "dry")
    dry = _FsEvents()
    with dry.installed(monkeypatch):
        out = _embed(_nested_ref(ref_kind, dry_tree), _nested_roots(roots_kind, dry_tree))
    assert _payloads(out) == [INSIDE], out[:200]
    first_open = dry.events.index("os.open")

    leaks = []
    for n in range(first_open, len(dry.events)):
        tree = _nested_root_tree(tmp_path / f"run{n}")
        hook = _FsEvents(fire_at=n, action=_swap_inbox(tree))
        with hook.installed(monkeypatch):
            out = _embed(_nested_ref(ref_kind, tree), _nested_roots(roots_kind, tree))
        assert hook.fired, f"swap point {n} ({dry.events[n]}) never reached"
        payloads = _payloads(out)
        if any(OUTSIDE_MARK in p for p in payloads):
            leaks.append((n, dry.events[n]))
            continue
        assert out == shares._PLACEHOLDER or payloads == [INSIDE], (n, out[:200])
    assert not leaks, f"outside bytes embedded when swapped before: {leaks}"


@pytest.mark.parametrize("ref_kind", ["relative", "absolute"])
def test_nested_root_ancestor_already_swapped_is_placeholder(tmp_path, monkeypatch, ref_kind):
    # Deterministic single-shot shape of the Codex probe: resolve the roots,
    # then swap workspace/inbox for a symlink before the reader's first open.
    tree = _nested_root_tree(tmp_path)
    swap = _swap_inbox(tree)
    real_open = os.open
    fired = []
    owner = threading.get_ident()

    def open_after_swap(*args, **kwargs):
        if threading.get_ident() == owner and not fired:
            fired.append(True)
            swap()
        return real_open(*args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(os, "open", open_after_swap)
        mp.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) | {open_after_swap})
        out = _embed(_nested_ref(ref_kind, tree), [tree["ws"], tree["att"]])
    assert fired, "the reader never opened anything"
    assert OUTSIDE_MARK not in b"".join(_payloads(out))
    assert out == shares._PLACEHOLDER, out[:200]


def test_root_with_symlinked_ancestor_is_still_served(tmp_path):
    # Configured roots are resolved first, then walked no-follow: a symlink
    # ANYWHERE in the configured spelling of the root keeps working.
    real = tmp_path / "real-parent"
    (real / "ws" / "sub").mkdir(parents=True)
    (real / "ws" / "sub" / "img.png").write_bytes(INSIDE)
    os.symlink(real, tmp_path / "parent-link")
    root = tmp_path / "parent-link" / "ws"
    for ref in (
        "MEDIA:sub/img.png",
        f"MEDIA:{root}/sub/img.png",
        f"MEDIA:{real}/ws/sub/img.png",
    ):
        assert _payloads(_embed(ref, [root])) == [INSIDE], ref


# R1-2  A malformed reference (lone surrogate in a ``~user`` name or in a
#       path) is a placeholder, never an exception that fails the whole share
#       snapshot (the route would turn it into HTTP 400).

_MALFORMED_REFS = [
    "~\ud800/img.png",  # expanduser -> pwd.getpwnam -> UnicodeEncodeError
    "~\ud800",
    "\ud800.png",  # relative: os.open(name) -> UnicodeEncodeError
    "sub\ud800/img.png",
]


@pytest.mark.parametrize("ref", _MALFORMED_REFS)
def test_malformed_ref_is_placeholder(tmp_path, ref):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _embed(f"see MEDIA:{ref}", [ws]) == "see " + shares._PLACEHOLDER
    absolute = f"MEDIA:{ws}/{ref.lstrip('~')}"
    assert _embed(absolute, [ws]) == shares._PLACEHOLDER


def test_malformed_tilde_ref_does_not_fail_share_snapshot(tmp_path):
    from types import SimpleNamespace

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.png").write_bytes(INSIDE)
    session = SimpleNamespace(
        workspace=str(ws),
        title="malformed media ref",
        messages=[
            {"role": "user", "content": "share it"},
            {"role": "assistant", "content": "bad MEDIA:~\ud800/img.png good MEDIA:ok.png"},
        ],
    )
    snapshot = shares.build_share_snapshot(session)
    content = snapshot["messages"][-1]["content"]
    assert content.startswith("bad " + shares._PLACEHOLDER + " good <img "), content[:200]
    assert _payloads(content) == [INSIDE]


# R1-3  Permission denied below a root is TERMINAL: the ref becomes the
#       placeholder and a namesake in a later root is never substituted.

_as_root = hasattr(os, "geteuid") and os.geteuid() == 0
needs_non_root = pytest.mark.skipif(_as_root, reason="root bypasses DAC permission bits")


@contextlib.contextmanager
def _chmod(path: Path, mode: int):
    old = path.stat().st_mode & 0o7777
    path.chmod(mode)
    try:
        yield
    finally:
        path.chmod(old)


@needs_non_root
@pytest.mark.parametrize(
    "denied", ["final-file", "intermediate-dir"]
)
def test_permission_denied_in_first_root_never_substitutes_later_root(tmp_path, denied):
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    for root, payload in ((root_a, INSIDE), (root_b, OUTSIDE)):
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "img.png").write_bytes(payload)
    target = root_a / "sub" / "img.png" if denied == "final-file" else root_a / "sub"
    with _chmod(target, 0):
        out = _embed("MEDIA:sub/img.png", [root_a, root_b])
    assert OUTSIDE_MARK not in b"".join(_payloads(out)), "root B namesake substituted"
    assert out == shares._PLACEHOLDER, out[:200]
    # Control: a plain miss in root A still falls through to root B.
    (root_a / "sub" / "img.png").unlink()
    assert _payloads(_embed("MEDIA:sub/img.png", [root_a, root_b])) == [OUTSIDE]
