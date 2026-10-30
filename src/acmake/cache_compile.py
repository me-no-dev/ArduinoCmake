"""Compile wrapper for shared object cache: run the compiler only if source is newer than the .o."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _strip_redundant_outer_double_quotes(s: str) -> str:
    """One layer of outer ``"…"`` when the interior has no ``"`` (see ``acmake.command``).

    Copied here so this file runs as ``python /path/to/cache_compile.py`` from the
    portable cache without ``acmake`` on ``PYTHONPATH`` (same rule as
    ``acmake.command._strip_redundant_outer_double_quotes`` — keep in sync).
    """
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        inner = s[1:-1]
        if '"' not in inner:
            return inner
    return s


def _mtime_ns(path: Path) -> int:
    st = path.stat()
    ns = getattr(st, "st_mtime_ns", None)
    if ns is not None:
        return int(ns)
    return int(st.st_mtime * 1_000_000_000)


def should_skip_cached_compile(obj: Path, src: Path) -> bool:
    """True when *obj* exists and is not older than *src*.

    Same rule Ninja uses to omit a compile custom command, and the same check
    ``main`` uses before invoking the compiler. Missing files, unequal paths,
    or stat errors must compile (or fail) rather than treat the object as fresh.
    """
    try:
        if not obj.is_file() or not src.is_file():
            return False
        return _mtime_ns(src) <= _mtime_ns(obj)
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    """``python -m acmake.cache_compile <object.o> <source> -- <compiler> [args...]``"""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4 or "--" not in args:
        print(
            "usage: python -m acmake.cache_compile <object.o> <source> "
            "-- <compiler> [args...]",
            file=sys.stderr,
        )
        return 2
    sep = args.index("--")
    obj = Path(args[0])
    src = Path(args[1])
    compile_argv = args[sep + 1 :]
    if not compile_argv:
        print("acmake.cache_compile: missing compiler argv after --", file=sys.stderr)
        return 2
    # Ninja + ``cmd.exe /C`` can leave grouped tokens as ``"@path"`` / ``"-I..."``;
    # GCC then treats them as literal filenames (``@`` response-file semantics are lost).
    compile_argv = [_strip_redundant_outer_double_quotes(a) for a in compile_argv]
    if should_skip_cached_compile(obj, src):
        return 0
    r = subprocess.run(compile_argv)
    return int(r.returncode) if r.returncode is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
