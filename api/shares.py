"""
Hermes Web UI -- public read-only share snapshots.

Stores a sanitized, immutable snapshot of a conversation under STATE_DIR/shares.
The snapshot is intentionally narrower than a full session export so public
links do not leak local workspace paths, profile details, or raw tool payloads.
"""

from __future__ import annotations

import base64
import binascii
import html
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from api.config import STATE_DIR
from api.helpers import _json_response_body, redact_session_data, split_media_token_ref
# _redact_fn_cached is the ALWAYS-ON credential redactor (agent redactor with
# force=True + local fallback regex). Unlike redact_session_data it does NOT
# consult the user-toggleable api_redact_enabled setting — a public share is a
# hard safety boundary that must redact credentials even if the operator turned
# API-response redaction off.
from api.helpers import _redact_fn_cached as _force_redact_credentials

logger = logging.getLogger(__name__)

SHARES_DIR = STATE_DIR / "shares"
_SHARE_LOCK = threading.Lock()


def _ensure_share_dir() -> None:
    SHARES_DIR.mkdir(parents=True, exist_ok=True)


def _share_path(token: str) -> Path:
    token = str(token or "").strip()
    if not token:
        raise ValueError("share token is required")
    if not token.replace("-", "").replace("_", "").isalnum():
        raise ValueError("invalid share token")
    return SHARES_DIR / f"{token}.json"


def _write_json_atomic(path: Path, payload: dict) -> None:
    body = _json_response_body(payload, pretty=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f"{path.stem}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _share_message_text(message: dict) -> str:
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                # Non-dict list items (e.g. nested structures) are NOT plain text —
                # never stringify them into the public snapshot.
                continue
            if item.get("type") == "text":
                # Only append genuine string text — a dict-valued "text" (possible
                # via /api/session/import) must NOT be str()'d into the public
                # snapshot (that would publish structured/tool payload verbatim).
                _t = item.get("text")
                if isinstance(_t, str):
                    parts.append(_t)
        return "".join(parts).strip()
    if isinstance(content, str):
        return content.strip()
    # A dict/other structured content (e.g. a tool-result object) is NOT shareable
    # text — do NOT str() it (that would publish raw structured/tool payload).
    return ""


def _redact_share_paths(text: str, extra_paths) -> str:
    """Strip known local session/workspace/home paths out of public-share text.

    A workspace path or Hermes home can be embedded inside message prose (an
    agent quoting a file path, a traceback, etc.). Redact the concrete local
    paths so a public share never discloses the operator's filesystem layout.
    """
    if not isinstance(text, str) or not text:
        return text
    for p in extra_paths:
        if not p:
            continue
        p = str(p).strip()
        if len(p) >= 4 and p in text:
            text = text.replace(p, "[redacted-path]")
    return text


# Regex matching local MEDIA:<path> references — same pattern that
# _inlineMediaHtmlForRef() in ui.js handles when rendering messages.
# Excludes MEDIA: followed by http/https URLs so external images pass
# through unchanged.  file:// references are NOT matched here — they are
# always rejected at the public-share boundary (absolute, un-scoped).
# `data:` URIs DO match: _replace_ref() routes them to _embed_share_data_uri(),
# which applies the same public-share policy as a local file (raster MIME
# allow-list, byte cap, magic-byte check) and never touches the filesystem.
# Feeding a multi-KB base64 blob to Path(...).resolve()/stat() raised OSError
# ENAMETOOLONG (errno 36) and 500'd share creation (#7949); passing every data:
# URI through unchanged would instead republish arbitrary bytes (text/plain,
# octet-stream, SVG) that credential redaction cannot see.
_SHARE_MEDIA_RE = re.compile(
    r"MEDIA:(?!https?://)([^\s\)\]>]+)"
)

# Strict shape for an inline data URI: data:<type/subtype>[;param...];base64,<payload>.
# Parameters (e.g. charset, name) are tolerated before the mandatory ;base64.
_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[a-z0-9.+-]+/[a-z0-9.+-]+)(?P<params>(?:;[^;,]*)*?);base64,(?P<payload>[A-Za-z0-9+/]*={0,2})$",
    re.IGNORECASE,
)
# Normalise common aliases to the canonical names used by _SHARE_ALLOWED_MIME_TYPES.
_DATA_URI_MIME_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}
# Upper bound on the ENCODED payload, checked before decoding so an oversized
# blob is refused without allocating its decoded bytes.
_SHARE_EMBED_MAX_B64_CHARS = ((512 * 1024 + 2) // 3) * 4

# Max size (in bytes) for files we'll embed as base64 in a share snapshot.
_SHARE_EMBED_MAX_BYTES = 512 * 1024  # 512 KiB

# Only these image MIME types may be embedded in public shares.
# Non-image files and SVG are NEVER embedded — embedding arbitrary file
# bytes circumvents the credential-redaction boundary that protects
# message prose, and a public share is not a file-transfer service.
# SVG is excluded because it is the only text-bearing type in this set;
# agent-authored SVGs can carry credentials in their text content which
# _redact_share_paths (which only touches message prose, not embedded
# bytes) cannot reach.
_SHARE_ALLOWED_MIME_TYPES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})

# SVG namespace URI used during sanitisation.
_SVG_NS = "http://www.w3.org/2000/svg"

# Pattern matching on* event-handler attributes.
_ON_ATTR_RE = re.compile(r"^on\w+$", re.IGNORECASE)

# Dangerous href/xlink:href schemes.
_DANGEROUS_HREF_RE = re.compile(r"^\s*javascript\s*:", re.IGNORECASE)

# Static placeholder emitted when a media reference cannot be embedded.
_PLACEHOLDER = "[*Local attachment omitted from public share*]"

# Magic byte signatures for allowed image formats — content-based validation
# that catches mismatched extensions (e.g. a .png that is actually a script).
# SVG is excluded here because it is validated by XML parsing in
# _sanitize_svg_bytes.
_IMAGE_MAGIC: dict[str, bytes] = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "image/gif": b"GIF8",
    "image/webp": b"RIFF",
}
# Offset for WebP magic: "RIFF" at 0, file size at 4, "WEBP" at 8.
_WEBP_MAGIC_OFFSET = 8
_WEBP_MAGIC = b"WEBP"


def _check_image_magic(data: bytes, mime_type: str) -> bool:
    """Verify *data* header bytes match the expected magic for *mime_type*.

    Returns ``True`` if the content is consistent with the claimed type.
    SVG is exempt because it is validated structurally by
    :func:`_sanitize_svg_bytes`.
    """
    if mime_type == "image/svg+xml":
        return True
    magic = _IMAGE_MAGIC.get(mime_type)
    if magic is None:
        return False
    if not data.startswith(magic):
        return False
    # Extra check for WebP: "WEBP" at offset 8.
    if mime_type == "image/webp":
        if len(data) < 12 or data[_WEBP_MAGIC_OFFSET:_WEBP_MAGIC_OFFSET + 4] != _WEBP_MAGIC:
            return False
    return True


def _sanitize_svg_bytes(data: bytes) -> bytes:
    """Strip script elements, on* handlers, and javascript: hrefs from SVG.

    SVG images served via ``<img src="data:image/svg+xml;base64,…">`` are
    sandboxed by modern browsers and script execution is blocked.  However,
    a sufficiently determined adversary with an older or exotic client may
    still extract credentials embedded in the SVG, so we strip the unsafe
    content at the server before it ever reaches a share page.

    Returns sanitised SVG bytes on success, or the original *data* unchanged
    if the content cannot be parsed as XML (fail-closed).
    """
    try:
        ET.register_namespace("", _SVG_NS)
        root = ET.fromstring(data.decode("utf-8", errors="replace"))
    except ET.ParseError:
        # Not valid XML — cannot sanitise safely.  Return a minimal empty SVG
        # so the <img> renders nothing rather than embedding un-sanitised bytes.
        return b'<svg xmlns="http://www.w3.org/2000/svg"/>'

    # Walk the tree depth-first, stripping on* attrs, dangerous hrefs,
    # and removing <script> children.
    def _walk(elem: ET.Element) -> None:
        for attr_name in list(elem.attrib):
            if _ON_ATTR_RE.match(attr_name):
                del elem.attrib[attr_name]
            elif attr_name in ("href", "xlink:href", "{http://www.w3.org/1999/xlink}href"):
                val = elem.attrib[attr_name]
                if _DANGEROUS_HREF_RE.match(val):
                    del elem.attrib[attr_name]

        for child in list(elem):
            tag = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
            if tag == "script":
                elem.remove(child)
            else:
                _walk(child)

    _walk(root)

    buf = io.BytesIO()
    tree = ET.ElementTree(root)
    tree.write(buf, encoding="utf-8", xml_declaration=False)
    return buf.getvalue()


def _embed_share_data_uri(raw: str) -> str:
    """Validate an inline ``data:`` media reference for a public share.

    Applies the same policy as an embedded local file: only the raster types in
    ``_SHARE_ALLOWED_MIME_TYPES``, strictly valid base64, at most
    ``_SHARE_EMBED_MAX_BYTES`` decoded, and magic bytes that match the declared
    type. A valid image is re-emitted as a canonical ``<img>`` built from the
    decoded bytes; anything else (non-image, SVG, malformed, oversized,
    mismatched magic, non-base64) becomes ``_PLACEHOLDER`` so none of its bytes
    reach the public snapshot. Never touches the filesystem (#7949).
    """
    if len(raw) > _SHARE_EMBED_MAX_B64_CHARS + 256:
        return _PLACEHOLDER
    m = _DATA_URI_RE.match(raw)
    if not m:
        return _PLACEHOLDER
    mime_type = m.group("mime").lower()
    mime_type = _DATA_URI_MIME_ALIASES.get(mime_type, mime_type)
    if mime_type not in _SHARE_ALLOWED_MIME_TYPES:
        return _PLACEHOLDER
    payload = m.group("payload")
    if not payload or len(payload) > _SHARE_EMBED_MAX_B64_CHARS:
        return _PLACEHOLDER
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return _PLACEHOLDER
    if not data or len(data) > _SHARE_EMBED_MAX_BYTES:
        return _PLACEHOLDER
    if not _check_image_magic(data, mime_type):
        return _PLACEHOLDER
    b64 = base64.b64encode(data).decode("ascii")
    return (
        f'<img src="data:{mime_type};base64,{b64}"'
        f' class="msg-media-img" alt="image" loading="lazy">'
    )


def _embed_share_media(text: str, *, allowed_roots: tuple[Path, ...] = ()) -> str:
    """Find local MEDIA: references and replace them with inline <img> tags.

    Only paths that name a regular file inside at least one of
    *allowed_roots* are honoured.  Absolute paths outside the roots,
    ``file://`` URIs, ``..`` components, ANY symlinked component below a
    root, non-image MIME types, and files larger than
    ``_SHARE_EMBED_MAX_BYTES`` are all replaced with a static placeholder —
    no file content leaves the server.

    The file is opened by walking from an opened allowed-root directory
    descriptor one component at a time with ``O_NOFOLLOW``; its type, size
    and bytes all come from that SAME descriptor, so a concurrent writer
    inside a root cannot swap a directory or the file between validation and
    read (#6126).  Platforms without that primitive fail closed.

    This runs BEFORE :func:`_redact_share_paths` so the concrete file path
    is still available for the allowed-roots check.
    """
    if not isinstance(text, str) or not text:
        return text

    allowed = tuple(Path(r).resolve() for r in allowed_roots if r)
    # Lexical spellings of each root (resolved + as configured) so an absolute
    # ref written against either spelling maps onto that root's descriptor.
    # The root itself is trusted configuration; only names BELOW it are
    # attacker-influenced, and those are never followed.
    root_spellings: list[tuple[Path, list[str]]] = []
    for configured, root in zip((r for r in allowed_roots if r), allowed, strict=True):
        for spelling in (str(root), os.path.abspath(str(configured))):
            spelling_parts = _share_path_components(spelling)
            if spelling_parts is None:
                continue  # never widen a root to "/" on an odd spelling
            spelled = (root, spelling_parts)
            if spelled not in root_spellings:
                root_spellings.append(spelled)

    def _candidates_under_roots(raw: str) -> list[tuple[Path, list[str]]]:
        """Map *raw* to ``(allowed root, component names)`` pairs, lexically.

        No filesystem access happens here; the walk itself is done on
        descriptors by :func:`_read_share_media_nofollow`.

        - ``file://`` is always rejected (absolute, un-scoped).
        - Absolute paths (``/…``, ``~…``) must sit lexically under a root.
        - Relative paths are anchored at each allowed root in turn so they
          don't silently anchor to the server's process CWD.
        - ``..`` refuses the whole ref; empty and ``.`` components are dropped.
        """
        if raw.startswith("file://"):
            return []

        # Defensive length / newline guard (#7949). A real local attachment
        # path is short; an over-long or newline-bearing token (e.g. a
        # data:...;base64,<blob> URI that slipped past the caller) is never a
        # valid file and must never be handed to the filesystem (an
        # over-length name raises OSError ENAMETOOLONG, errno 36).
        # 4096 comfortably exceeds any real PATH_MAX-bounded attachment path.
        if len(raw) > 4096 or "\n" in raw or "\x00" in raw:
            return []

        # --- Absolute paths: lexical allow-list check against root spellings
        if raw.startswith("/") or raw.startswith("~"):
            try:
                # ``~user`` expansion looks the name up in the password
                # database; a malformed name (e.g. a lone surrogate) raises
                # UnicodeEncodeError, which must become the placeholder, not
                # fail the whole share snapshot (#6126 round 1).
                expanded = os.path.expanduser(raw)
            except (OSError, ValueError, RuntimeError):
                return []
            parts = _share_path_components(expanded) if os.path.isabs(expanded) else None
            if not parts:
                return []
            candidates = []
            for root, root_parts in root_spellings:
                depth = len(root_parts)
                if len(parts) > depth and parts[:depth] == root_parts:
                    candidates.append((root, parts[depth:]))
            return candidates

        # --- Relative paths: try each allowed root as the anchor -------------
        parts = _share_path_components(raw)
        if not parts:
            return []
        return [(root, parts) for root in allowed]

    def _replace_ref(m: re.Match) -> str:
        parts = split_media_token_ref(text, m)
        if not parts:
            return m.group(0)
        raw, suffix = parts
        raw = raw.strip()
        if not raw:
            return m.group(0)

        # --- Inline data: URI — validate in memory, never resolve as a path ---
        if raw[:5].lower() == "data:":
            return _embed_share_data_uri(raw) + suffix

        # --- Resolve and validate against allowed roots -----------------------
        candidates = _candidates_under_roots(raw)
        if not candidates:
            return _PLACEHOLDER + suffix
        first_root, first_names = candidates[0]
        name = first_names[-1]

        # --- MIME allow-list (images only) ------------------------------------
        mime_type, _ = mimetypes.guess_type(os.path.join(str(first_root), *first_names))
        if not mime_type or mime_type not in _SHARE_ALLOWED_MIME_TYPES:
            return _PLACEHOLDER + suffix

        # --- Size guard + bounded read from ONE no-follow descriptor (#6126) --
        data = _read_share_media_nofollow(candidates, _SHARE_EMBED_MAX_BYTES)
        if data is None:
            return _PLACEHOLDER + suffix

        # --- Embed as base64 <img> -------------------------------------------
        try:
            # Content-based MIME validation: verify the actual file header
            # matches the claimed MIME type — catches extension-spoofed files
            # (e.g. a script renamed to .png).
            if not _check_image_magic(data, mime_type):
                return _PLACEHOLDER + suffix
            # Sanitise SVG content before embedding — SVG can carry
            # <script> elements and on* event handlers that could leak
            # credentials in the context of a public share page.
            if mime_type == "image/svg+xml":
                data = _sanitize_svg_bytes(data)
            b64 = base64.b64encode(data).decode("ascii")
            # HTML-escape the filename so a crafted name like
            # '"><script>alert(1)</script>' cannot break out of the
            # attribute and inject script into the share page.
            safe_name = html.escape(name, quote=True)
            return (
                f'<img src="data:{mime_type};base64,{b64}"'
                f' class="msg-media-img" alt="{safe_name}"'
                f' loading="lazy">'
            ) + suffix
        except (OSError, MemoryError):
            return _PLACEHOLDER + suffix

    return _SHARE_MEDIA_RE.sub(_replace_ref, text)


def _share_path_components(path: str) -> list[str] | None:
    """Split a POSIX path into plain names for a no-follow descriptor walk.

    Empty and ``.`` components are dropped.  Any ``..`` refuses the whole
    path (``None``): it is never resolved, lexically or by the kernel (#6126).
    """
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if ".." in parts:
        return None
    return parts


def _share_nofollow_open_supported() -> bool:
    """True when a rooted, descriptor-relative, no-follow open is available.

    The public-share embedder refuses every local file when this is False
    instead of falling back to a racy pathname read (#6126).  Evaluated per
    call (it is cheap) so it always reflects the live ``os`` module.
    """
    return (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in getattr(os, "supports_dir_fd", ())
    )


class _ShareMediaRefused(Exception):
    """Terminal refusal of a share media ref: emit the placeholder and do NOT
    try the ref under another allowed root (#6126 round 1)."""


def _share_dir_open_flags() -> int:
    """Open flags for the directory descriptors of the no-follow walk.

    On Linux these are ``O_PATH`` descriptors: they need only SEARCH
    permission on the parent, exactly like a pathname lookup, so an image
    beneath an execute-only (``0111``) directory or root still embeds.  They
    can only serve as ``dir_fd`` anchors; the final file is still opened with
    read flags.  Elsewhere (no ``O_PATH``) the directory must be readable
    (#6126 round 1).
    """
    import sys

    flags = os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    if sys.platform.startswith("linux") and hasattr(os, "O_PATH"):
        return flags | os.O_PATH
    return flags | os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)


def _share_openat_dir(dir_fd: int, name: str, dir_flags: int) -> int:
    """Open directory *name* relative to *dir_fd* and return the new descriptor.

    *dir_fd* is closed only once the new descriptor exists, so on an exception
    the caller still owns (and closes) exactly one descriptor.
    """
    next_fd = os.open(name, dir_flags, dir_fd=dir_fd)
    try:
        os.close(dir_fd)
    except OSError:
        pass
    return next_fd


def _open_share_file_nofollow(root: Path, names: list[str], dir_flags: int, file_flags: int) -> int | None:
    """Open ``root/names...`` without following any symlink at or below *root*.

    *root* is the already-resolved (realpath) allowed root.  It is NOT opened
    by pathname: the walk starts at ``/`` and opens every component of the
    resolved root relative to the previous directory descriptor with
    ``O_NOFOLLOW``, so an ancestor of a nested root (an attachments root inside
    a writable workspace) swapped for a symlink after resolution fails the
    walk instead of redirecting the root descriptor (#6126 round 1).  A root
    configured through a symlink still works because its realpath has none.
    The names below the root are then walked the same way.  Returns the final
    descriptor (caller closes it) or ``None``.
    """
    root_text = str(root)
    root_names = _share_path_components(root_text)
    if root_names is None or not root_text.startswith("/"):
        return None
    try:
        dir_fd = os.open("/", dir_flags)
    except (OSError, ValueError):
        return None
    try:
        try:
            for name in root_names:
                dir_fd = _share_openat_dir(dir_fd, name, dir_flags)
        except (OSError, ValueError):
            return None  # root missing, unreachable or redirected: a miss
        try:
            for name in names[:-1]:
                dir_fd = _share_openat_dir(dir_fd, name, dir_flags)
            return os.open(names[-1], file_flags, dir_fd=dir_fd)
        except PermissionError:
            # EACCES/EPERM below the root: something is there but cannot be
            # read.  Terminal, so a namesake in a later root is never
            # substituted for it (#6126 round 1).
            raise _ShareMediaRefused from None
        except (OSError, ValueError):
            return None
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


def _read_share_media_nofollow(candidates, limit: int) -> bytes | None:
    """Return the bytes of the first regular file among *candidates*.

    Each candidate is ``(allowed root, component names)``.  The file is opened
    with :func:`_open_share_file_nofollow`; type and size come from ``fstat``
    on that descriptor and at most ``limit + 1`` bytes are read from it, so a
    file that grows after ``fstat`` is refused rather than embedded (#6126).
    Like the old ``is_file()`` probe, an entry that cannot be opened (missing,
    symlinked component) or is not a regular file falls through to the next
    root.  Permission denied below a root is terminal (never substitute a
    later root's namesake); an unsupported platform, an oversize file or a
    read error also return ``None`` and the caller emits the placeholder.
    """
    if not _share_nofollow_open_supported():
        return None
    import stat as _stat

    common = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    dir_flags = _share_dir_open_flags()
    file_flags = common | getattr(os, "O_NOCTTY", 0)  # O_NONBLOCK: never block on a FIFO
    for root, names in candidates:
        if not names:
            continue
        try:
            fd = _open_share_file_nofollow(root, names, dir_flags, file_flags)
        except _ShareMediaRefused:
            return None
        if fd is None:
            continue
        try:
            st = os.fstat(fd)
            if not _stat.S_ISREG(st.st_mode):
                continue
            if st.st_size > limit:
                return None
            chunks = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            return data if len(data) <= limit else None
        except (OSError, MemoryError):
            return None
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    return None


def _sanitize_message(message: dict, *, redact_paths=(), allowed_roots: tuple[Path, ...] = ()) -> dict | None:
    if not isinstance(message, dict):
        return None
    role = str(message.get("role") or "").strip().lower()
    if role not in {"user", "assistant"}:
        return None
    # Public boundary: hidden internal rows (delegation_wakeup handoffs) are
    # private model plumbing and must never appear in a share
    # (#quiet-delegation gate review).
    from api.process_event_utils import is_hidden_transcript_row

    if is_hidden_transcript_row(message):
        return None
    text = _share_message_text(message)
    if not text:
        return None
    # ALWAYS-ON hardening for the public boundary, independent of any setting:
    # (1) force credential redaction, (2) embed allowed local media,
    # (3) strip known local paths.
    text = _force_redact_credentials(text)
    # Embed local media BEFORE path redaction so the concrete path is still
    # available for file reads.  MEDIA: references become self-contained data
    # URIs — or a static placeholder if the path is outside the allowed roots.
    text = _embed_share_media(text, allowed_roots=allowed_roots)
    text = _redact_share_paths(text, redact_paths)
    if not text.strip():
        return None
    sanitized = {
        "role": role,
        "content": text,
    }
    ts = message.get("timestamp")
    if isinstance(ts, (int, float)):
        sanitized["timestamp"] = ts
    return sanitized


def _public_share_payload(payload: dict) -> dict:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []
    public = {
        "title": str(payload.get("title") or "Untitled"),
        "messages": messages,
        "message_count": int(payload.get("message_count") or len(messages)),
    }
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    if isinstance(created_at, (int, float)):
        public["created_at"] = created_at
    if isinstance(updated_at, (int, float)):
        public["updated_at"] = updated_at
    return public


def build_share_snapshot(session) -> dict:
    raw_dict = getattr(session, "__dict__", {}) or {}
    # redact_session_data respects the api_redact_enabled setting; keep it as a
    # first pass, but the per-message sanitizer below applies ALWAYS-ON credential
    # + path redaction that does NOT depend on that setting (the public boundary
    # must hold even if the operator disabled api_redact_enabled).
    safe_session = redact_session_data(raw_dict)
    # Concrete local paths to scrub from any message prose / title.
    redact_paths = []
    for key in ("workspace", "worktree_path", "worktree_repo_root"):
        val = raw_dict.get(key)
        if val:
            redact_paths.append(str(val))
    try:
        from api.profiles import get_active_hermes_home
        redact_paths.append(str(get_active_hermes_home()))
    except Exception:
        pass
    try:
        redact_paths.append(str(Path.home()))
    except Exception:
        pass
    # Collect allowed roots for _embed_share_media: only files inside the
    # session workspace or the attachments root may be embedded.  This is
    # the hard security boundary that prevents arbitrary file reads through
    # crafted MEDIA: references in message text.
    _allowed_roots: list[Path] = []
    _ws = raw_dict.get("workspace")
    if _ws and isinstance(_ws, str) and _ws.strip():
        _allowed_roots.append(Path(_ws.strip()))
    try:
        from api.upload import _attachment_root
        _allowed_roots.append(_attachment_root())
    except Exception:
        pass
    _allowed_roots_tuple: tuple[Path, ...] = tuple(_allowed_roots)
    safe_messages = []
    for raw in safe_session.get("messages") or []:
        sanitized = _sanitize_message(
            raw, redact_paths=redact_paths, allowed_roots=_allowed_roots_tuple,
        )
        if sanitized:
            safe_messages.append(sanitized)
    if not safe_messages:
        raise ValueError("This conversation has no shareable messages yet.")
    # Only accept a genuine string title — a dict-valued title (possible via
    # /api/session/import) must not be str()'d into the public snapshot.
    _raw_title = safe_session.get("title")
    _raw_title = _raw_title if isinstance(_raw_title, str) else "Untitled"
    title = _force_redact_credentials(_raw_title or "Untitled")
    title = _redact_share_paths(title, redact_paths) or "Untitled"
    return {
        "title": title,
        "messages": safe_messages,
        "message_count": len(safe_messages),
    }


def create_or_refresh_share(session) -> dict:
    snapshot = build_share_snapshot(session)
    with _SHARE_LOCK:
        _ensure_share_dir()
        existing_token = str(getattr(session, "share_token", "") or "").strip()
        token = existing_token or secrets.token_urlsafe(18)
        now = time.time()
        payload = {
            "token": token,
            "source_session_id": str(getattr(session, "session_id", "") or ""),
            "title": snapshot["title"],
            "messages": snapshot["messages"],
            "message_count": snapshot["message_count"],
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
        }
        path = _share_path(token)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    payload["created_at"] = existing.get("created_at") or now
            except Exception:
                logger.debug("Ignoring malformed share snapshot at %s", path, exc_info=True)
        _write_json_atomic(path, payload)
    return {
        "share_token": token,
        "share_title": payload["title"],
        "share_message_count": payload["message_count"],
        "share_created_at": payload["created_at"],
        "share_updated_at": payload["updated_at"],
    }


def load_share(token: str) -> dict | None:
    try:
        path = _share_path(token)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to read share snapshot %s", path, exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("revoked_at"):
        return None
    return _public_share_payload(payload)


def revoke_share(session) -> bool:
    token = str(getattr(session, "share_token", "") or "").strip()
    if not token:
        return False
    with _SHARE_LOCK:
        try:
            path = _share_path(token)
        except ValueError:
            return False
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload["revoked_at"] = time.time()
            _write_json_atomic(path, payload)
    return True
