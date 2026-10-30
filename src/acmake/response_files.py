"""Expand GCC ``@response-file`` arguments into argv tokens (avoids Ninja/CMake ``@`` quirks)."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

_HEADER_LINE = re.compile(
    r"^\s*#\s*(define|include|pragma|ifndef|endif|else|if)\b", re.IGNORECASE
)


def read_gcc_response_file_tokens(rsp: Path) -> list[str]:
    """Read a GCC response file and return argv fragments (comments stripped).

    If *rsp* looks like a C/C++ header (``#define`` / ``#include`` / …), return
    ``-include`` and the absolute path so the compiler sees the same semantics
    as ``@`` for that case without inlining ``#`` lines as bogus arguments.
    """
    try:
        raw = rsp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return []

    if _HEADER_LINE.match(lines[0]):
        return ["-include", str(rsp.resolve())]

    out: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # GCC rsp lines use POSIX-style quoting; ``posix=False`` (Windows host) splits
        # quoted ``-I"C:/a b/c"`` into broken fragments and drops variant include paths.
        out.extend(shlex.split(s, posix=True))
    return out


def resolve_build_dir_at_file_argv(
    build_dir: Path, argv: list[str], *, inline: bool = True
) -> tuple[list[str], list[str]]:
    """Rewrite ``@path`` when *path* is under *build_dir* (acmake output).

    Empty stubs (e.g. ``file_opts``) are dropped. Header-like files become
    ``-include <relpath>`` so the compile line does not use ``@`` against generated
    paths (avoids Ninja/gcc and missing-file issues). Other response files under
    *build_dir* are inlined as argv tokens.

    ``@`` paths outside *build_dir* are left unchanged.

    With ``inline=False``, ``@`` paths under *build_dir* are left as ``@path`` so the
    tool reads them **at execution time**. Use this for link/objcopy commands: response
    files created or refreshed by ``recipe.hooks.linking.prelink`` (e.g. ESP32
    ``link_libs.rsp``) do not exist when CMakeLists.txt is written, and inlining an
    older copy would freeze stale contents into the link line.

    Missing ``@`` files are always preserved, never silently dropped: a later build
    step (a hook, or the tool itself) may create them.

    Returns ``(new_argv, extra_dep_paths)`` for paths consumed from under *build_dir*.
    """
    bd = build_dir.resolve()
    out: list[str] = []
    extra_deps: list[str] = []

    for a in argv:
        if not a.startswith("@") or len(a) < 2:
            out.append(a)
            continue
        path_str = a[1:].strip().strip('"')
        p = Path(path_str)
        try:
            rp = p.resolve()
        except OSError:
            out.append(a)
            continue
        try:
            rp.relative_to(bd)
        except ValueError:
            out.append(a)
            continue

        if not p.is_file():
            # Generated later (e.g. prelink response file): keep ``@path`` intact.
            out.append(a)
            continue

        extra_deps.append(str(rp))
        if not inline:
            out.append(a)
            continue
        toks = read_gcc_response_file_tokens(p)
        if not toks:
            # Empty ``build_opt.h`` is valid (Arduino build flags); still force ``-include``.
            if p.name.lower() == "build_opt.h":
                rel = os.path.relpath(rp, bd).replace("\\", "/")
                out.extend(["-include", rel])
            continue
        if len(toks) == 2 and toks[0] == "-include":
            inc = Path(toks[1]).resolve()
            try:
                rel = os.path.relpath(inc, bd).replace("\\", "/")
            except ValueError:
                out.extend(toks)
                continue
            out.append("-include")
            out.append(rel)
            continue
        out.extend(toks)

    return out, extra_deps


def collect_at_file_dep_paths(argv: list[str]) -> list[str]:
    """Paths referenced by ``@file`` arguments (for Ninja ``DEPENDS``)."""
    deps: list[str] = []
    for a in argv:
        if not a.startswith("@") or len(a) < 2:
            continue
        path_str = a[1:].strip().strip('"')
        p = Path(path_str)
        if p.is_file():
            deps.append(str(p.resolve()))
    return deps


def argv_text_for_depfile_detection(argv: list[str]) -> str:
    """Join argv plus a one-level peek into non-header ``@`` response files (for ``-MMD`` / ``-MF``)."""
    parts = [a.replace("\\", "/") for a in argv]
    for a in argv:
        if not a.startswith("@") or len(a) < 2:
            continue
        p = Path(a[1:].strip().strip('"'))
        if not p.is_file():
            continue
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if lines and _HEADER_LINE.match(lines[0]):
            continue
        parts.append(raw.replace("\n", " ").replace("\\", "/"))
    return " ".join(parts)


def expand_at_file_arguments(
    argv: list[str],
    *,
    max_rounds: int = 24,
) -> tuple[list[str], list[str]]:
    """Replace each ``@path`` argument with tokens from that file.

    Returns ``(new_argv, extra_dep_paths)`` for Ninja/Cmake ``DEPENDS``.
    """
    deps: list[str] = []
    cur = list(argv)
    for _ in range(max_rounds):
        nxt: list[str] = []
        changed = False
        for a in cur:
            if not a.startswith("@") or len(a) < 2:
                nxt.append(a)
                continue
            path_str = a[1:].strip().strip('"')
            p = Path(path_str)
            if not p.is_file():
                nxt.append(a)
                continue
            deps.append(str(p.resolve()))
            frag = read_gcc_response_file_tokens(p)
            if frag:
                nxt.extend(frag)
            # Omit empty/unreadable @files (empty GCC rsp still adds a bogus argv slot).
            changed = True
        if not changed:
            break
        cur = nxt
    return cur, deps


def _copy_file_if_contents_differ(src: Path, dst: Path) -> None:
    """Copy *src* to *dst* only when missing or bytes differ (preserve *dst* mtime otherwise).

    Unconditional ``copy2`` on every configure invalidates Ninja deps (e.g. ESP32
    ``-include``/``@`` on ``build_opt.h``), forcing core/library object rebuilds and
    defeating the shared object cache.
    """
    import shutil

    try:
        if dst.is_file() and dst.read_bytes() == src.read_bytes():
            return
    except OSError:
        pass
    shutil.copy2(src, dst)


def ensure_build_response_stubs(build_dir: Path, sketch_dir: Path) -> None:
    """Create ``build_opt.h`` / ``file_opts`` like ESP32 ``recipe.hooks.prebuild.*``."""
    build_dir.mkdir(parents=True, exist_ok=True)
    opt = build_dir / "build_opt.h"
    sketch_opt = sketch_dir / "build_opt.h"
    if sketch_opt.is_file():
        _copy_file_if_contents_differ(sketch_opt, opt)
    elif not opt.is_file() or opt.stat().st_size != 0:
        opt.write_bytes(b"")
    fo = build_dir / "file_opts"
    if not fo.is_file():
        fo.write_bytes(b"")
