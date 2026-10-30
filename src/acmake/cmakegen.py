"""Generate CMakeLists.txt that runs Arduino recipe commands via Ninja."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

import acmake.cache_compile as _cache_compile_mod
from acmake.cache_compile import should_skip_cached_compile

from acmake.build import (
    BuildPlan,
    expand_recipe_for_source,
    includes_string_for_cached_lib_compile,
)
from acmake.command import split_recipe
from acmake.hooks import (
    collect_ordered_hook_shell_commands,
    objcopy_post_recipe_hook_phases,
    objcopy_pre_recipe_hook_phases,
)
from acmake.response_files import (
    argv_text_for_depfile_detection,
    collect_at_file_dep_paths,
    resolve_build_dir_at_file_argv,
)
from acmake.sketch import sketch_build_project_name

# Run as ``python /path/to/cache_compile.py`` so the build does not need ``acmake`` on PYTHONPATH.
_CACHE_COMPILE_SCRIPT = str(Path(_cache_compile_mod.__file__).resolve())


def _q(p: Path | str) -> str:
    s = str(p).replace("\\", "/")
    return '"' + s.replace('"', '\\"') + '"'


def _cmake_bracket_argument(s: str) -> str:
    """Bracket argument (CMake ≥3) — no raw ``"`` characters (required for ``COMMAND`` + ``VERBATIM`` on CMake 4.x)."""
    t = str(s).replace("\\", "/")
    for n in range(64):
        end = "]" + ("=" * n) + "]"
        if end not in t:
            beg = "[" + ("=" * n) + "["
            return beg + t + end
    raise ValueError("cannot bracket-quote string for CMake COMMAND")


def _argv_to_cmake_command(argv: list[str]) -> str:
    """``COMMAND`` / ``VERBATIM`` argv lines: one bracket-quoted token per argv element."""
    lines = ["  COMMAND"]
    for a in argv:
        lines.append(f"    {_cmake_bracket_argument(str(a))}")
    return "\n".join(lines)


def _strip_sketch_source_path_from_compile_recipe(plan: BuildPlan, cmd: str) -> str:
    """Remove ``-I`` of the sketch directory from a compile recipe string.

    ESP32 ``platform.txt`` sets ``compiler.cpreprocessor.flags`` with ``-I{build.source.path}``.
    That path is different for every example, so it was embedded in cached core/variant/lib
    Ninja rules and forced recompilation when a **new** sketch ``build/`` was generated even
    though the shared ``.o`` and FQBN were unchanged. Sketch TUs still need the real ``-I``;
    library/core sources do not include the user's ``.ino`` tree for normal Arduino layouts.
    """
    if not cmd.strip():
        return cmd
    paths: list[str] = []
    try:
        paths.append(str(plan.sketch_dir.resolve()).replace("\\", "/"))
    except OSError:
        pass
    try:
        paths.append(str(os.path.realpath(plan.sketch_dir)).replace("\\", "/"))
    except OSError:
        pass
    bsp = (plan.expanded.get("build.source.path") or "").strip().replace("\\", "/")
    if bsp:
        paths.append(bsp.rstrip("/"))
        paths.append(bsp.rstrip("/") + "/")
    seen: set[str] = set()
    uniq: list[str] = []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    out = cmd.replace("\\", "/")
    for p in uniq:
        variants = {p, p.rstrip("/")}
        variants.add(p.rstrip("/") + "/")
        if not p.endswith("/"):
            variants.add(p + "/")
        for pv in variants:
            for q in (f'-I"{pv}"', f"-I'{pv}'", f'"-I{pv}"', f"'-I{pv}'"):
                out = out.replace(q, " ")
    return " ".join(out.split())


def _recipe_for_source(plan: BuildPlan, so) -> str:
    ext = so.source.suffix.lower()
    if ext == ".c":
        key = "recipe.c.o.pattern"
    elif ext in (".cc", ".cpp", ".cxx"):
        key = "recipe.cpp.o.pattern"
    elif ext in (".s", ".S"):
        key = "recipe.S.o.pattern"
    else:
        return ""
    inc = plan.expanded.get("includes", "")
    if (
        plan.object_cache_dir is not None
        and so.kind == "lib"
        and so.lib is not None
    ):
        inc = includes_string_for_cached_lib_compile(
            plan.expanded,
            plan.libraries,
            so.lib,
            plan.platform_header_stems,
        )
    cmd = expand_recipe_for_source(
        plan.expanded, key, so.source, so.object_path, inc
    )
    if plan.object_cache_dir is not None and so.kind != "sketch" and cmd.strip():
        cmd = _strip_sketch_source_path_from_compile_recipe(plan, cmd)
    return cmd


def _link_combine_template(expanded: dict[str, str]) -> str:
    """Resolve link recipe text (Arduino uses OS-specific and/or numbered keys)."""

    def _strip(s: str) -> str:
        return s.strip() if s else ""

    def _first(*keys: str) -> str:
        for k in keys:
            v = _strip(expanded.get(k, ""))
            if v:
                return v
        return ""

    ros = _strip(expanded.get("runtime.os", ""))

    # OS-specific full pattern (e.g. recipe.c.combine.pattern.macosx)
    if ros:
        v = _first(f"recipe.c.combine.pattern.{ros}")
        if v:
            return v
        v = _first(f"recipe.cpp.combine.pattern.{ros}")
        if v:
            return v

    v = _first("recipe.c.combine.pattern", "recipe.cpp.combine.pattern")
    if v:
        return v

    # Numbered fragments: recipe.c.combine.pattern.1, .2, … (optionally OS-prefixed)
    def _numbered(prefix: str) -> str:
        parts: list[str] = []
        for i in range(1, 64):
            p = _strip(expanded.get(f"{prefix}.{i}", ""))
            if not p:
                break
            parts.append(p)
        return " ".join(parts) if parts else ""

    if ros:
        s = _numbered(f"recipe.c.combine.pattern.{ros}")
        if s:
            return s
    s = _numbered("recipe.c.combine.pattern")
    if s:
        return s

    # Any non-hook recipe key that looks like a combine/link pattern
    hits: list[tuple[str, str]] = []
    for k, v in sorted(expanded.items(), key=lambda kv: kv[0]):
        if not v or not str(v).strip():
            continue
        kl = k.lower()
        if "hooks" in kl:
            continue
        if not kl.startswith("recipe."):
            continue
        if "combine" not in kl or "pattern" not in kl:
            continue
        hits.append((k, str(v).strip()))
    if hits:
        hits.sort(key=lambda x: x[0])
        return hits[0][1]

    return ""


_LINK_ORDER = {"sketch": 0, "lib": 1, "variant": 2}


def link_combine_object_paths(plan: BuildPlan) -> list[Path]:
    """Non-core object paths ordered for the link line (sketch -> libraries -> variant).

    Arduino-cli uses this order in ``recipe.c.combine.pattern``'s ``{object_files}``.
    """
    objs = [so for so in plan.sources if so.kind != "core"]
    objs.sort(key=lambda so: _LINK_ORDER.get(so.kind, 99))
    return [so.object_path for so in objs]


def _expand_combine(plan: BuildPlan, other_objs: list[Path]) -> str:
    tmpl = _link_combine_template(plan.expanded)
    if not tmpl:
        return ""
    objs = " ".join(_q(o) for o in other_objs)
    from acmake.properties import expand_template

    ctx = dict(plan.expanded)
    ctx["object_files"] = objs
    if plan.archive_path:
        ctx["archive_file_path"] = str(plan.archive_path)
        ctx["archive_file"] = plan.archive_path.name
    return expand_template(tmpl, ctx)


def _expand_objcopy(plan: BuildPlan, key: str) -> str:
    tmpl = plan.expanded.get(key, "")
    if not tmpl:
        return ""
    from acmake.properties import expand_template

    return expand_template(tmpl, dict(plan.expanded))


_OBJCOPY_PLAIN = re.compile(
    r"^recipe\.objcopy\.(?P<stem>.+)\.pattern$", re.IGNORECASE
)
_OBJCOPY_OS = re.compile(
    r"^recipe\.objcopy\.(?P<stem>.+)\.pattern\.(?P<ros>macosx|linux|windows)$",
    re.IGNORECASE,
)


def _objcopy_stem_sort_key(stem: str) -> tuple[int, str]:
    """Prefer ESP32-style ``partitions.bin`` before ``bin``, and ``hex`` before ``eep`` (AVR)."""
    pri = {
        "partitions.bin": 0,
        "bootloader.bin": 5,
        "hex": 20,
        "eep": 25,
        "bin": 40,
    }
    s = stem.lower()
    return (pri.get(s, 1000), s)


def _objcopy_keys_by_stem(expanded: dict[str, str]) -> dict[str, str]:
    """Map objcopy stem → property key (``recipe.objcopy.<stem>.pattern.<runtime.os>`` wins)."""
    ros = (expanded.get("runtime.os") or "").strip().lower()
    by_stem: dict[str, str] = {}
    for k, v in sorted(expanded.items(), key=lambda kv: kv[0]):
        if not str(v).strip():
            continue
        m = _OBJCOPY_OS.match(k)
        if m and m.group("ros").lower() == ros:
            by_stem[m.group("stem")] = k
    for k, v in sorted(expanded.items(), key=lambda kv: kv[0]):
        if not str(v).strip():
            continue
        m = _OBJCOPY_PLAIN.match(k)
        if not m:
            continue
        stem = m.group("stem")
        if stem not in by_stem:
            by_stem[stem] = k
    return by_stem


def ordered_objcopy_recipe_keys(expanded: dict[str, str]) -> list[str]:
    """All non-empty ``recipe.objcopy.*.pattern`` keys, deduped by stem, in build order."""
    by_stem = _objcopy_keys_by_stem(expanded)
    stems = sorted(by_stem, key=_objcopy_stem_sort_key)
    return [by_stem[s] for s in stems]


def stem_from_objcopy_recipe_key(key: str) -> str:
    m = _OBJCOPY_OS.match(key) or _OBJCOPY_PLAIN.match(key)
    return m.group("stem") if m else ""


def _infer_objcopy_output_from_argv(argv: list[str], bd: Path) -> Path | None:
    """Best-effort output path for unknown ``recipe.objcopy.<stem>.pattern`` names."""
    bd = bd.resolve()
    for i, a in enumerate(argv):
        if a in ("-o", "--output") and i + 1 < len(argv):
            p = Path(argv[i + 1])
            return (p if p.is_absolute() else (bd / p)).resolve()
    for a in reversed(argv):
        if len(a) >= 2 and a.startswith("-") and a[1] not in "0123456789":
            continue
        p = Path(a)
        out = (p if p.is_absolute() else (bd / p)).resolve()
        low = str(out).lower()
        if low.endswith(".elf") or low.endswith(".csv"):
            continue
        try:
            out.relative_to(bd)
        except ValueError:
            if p.is_absolute() and p.suffix:
                return out
            continue
        if out.suffix.lower() in (".o", ".a"):
            continue
        if not out.suffix:
            continue
        return out
    return None


def infer_objcopy_output_path(plan: BuildPlan, stem: str, argv: list[str]) -> Path:
    """Ninja ``OUTPUT`` path for one objcopy recipe."""
    bd = plan.build_dir.resolve()
    name = plan.expanded.get("build.project_name") or sketch_build_project_name(
        plan.sketch_dir
    )
    sl = stem.lower()
    if sl == "hex":
        return plan.hex_path or (bd / f"{name}.hex")
    if sl == "eep":
        return plan.eep_path or (bd / f"{name}.eep")
    if sl == "bin":
        return plan.bin_path or (bd / f"{name}.bin")
    if sl == "partitions.bin":
        return bd / f"{name}.partitions.bin"
    guessed = _infer_objcopy_output_from_argv(argv, bd)
    if guessed is not None:
        return guessed
    return bd / f"{name}.{stem}"


def _write_hook_runner(build_dir: Path, name: str, commands: list[str]) -> Path:
    """Shell (POSIX) or batch (Windows) script; one hook command per line, ``set -e`` / ``if errorlevel``."""
    if os.name == "nt":
        bat = build_dir / f".acmake_hooks_{name}.bat"
        lines = ["@echo off", "setlocal"]
        for c in commands:
            lines.append(c)
            lines.append("if errorlevel 1 exit /b 1")
        text = "\r\n".join(lines) + "\r\n"
        if not (bat.is_file() and bat.read_text(encoding="utf-8", errors="replace") == text):
            bat.write_text(text, encoding="utf-8")
        return bat
    sh = build_dir / f".acmake_hooks_{name}.sh"
    text = "\n".join(["#!/bin/sh", "set -e", *commands]) + "\n"
    if not (sh.is_file() and sh.read_text(encoding="utf-8", errors="replace") == text):
        sh.write_text(text, encoding="utf-8")
    mode = sh.stat().st_mode
    if not (mode & 0o111):
        sh.chmod(mode | 0o111)
    return sh


def _emit_objcopy_hook_stamp(
    lines: list[str],
    *,
    bd: Path,
    stamp: Path,
    depends_quoted: list[str],
    script: Path,
    cmake_comment: str,
) -> None:
    """``add_custom_command`` that runs hook script then updates *stamp* (Ninja ordering)."""
    lines.append(f"# {cmake_comment}")
    lines.append("add_custom_command(")
    lines.append(f"  OUTPUT {_q(stamp)}")
    if os.name == "nt":
        lines.extend(_argv_to_cmake_command(["cmd", "/c", str(script)]).splitlines())
    else:
        lines.extend(_argv_to_cmake_command(["/bin/sh", str(script)]).splitlines())
    lines.append(
        f"  COMMAND ${{CMAKE_COMMAND}} -E touch {_cmake_bracket_argument(str(stamp))}"
    )
    deps = list(depends_quoted)
    deps.append(_q(script))
    lines.append("  DEPENDS " + " ".join(deps))
    lines.append(f"  WORKING_DIRECTORY {_q(bd)}")
    lines.append("  VERBATIM")
    lines.append(")")
    lines.append("")


def _argv_set_depfile_mf(
    argv: list[str],
    dep_path: Path,
    *,
    mf_relative_to: Path | None = None,
) -> list[str]:
    """Force ``-MF`` (and combined ``-MFpath``) so GCC writes the depfile under the build tree.

    For shared-cache compiles (core/variant/lib), *mf_relative_to* is the sketch ``build_dir``:
    the argument is a **path relative to that directory** so the Ninja rule string does not
    embed a per-sketch absolute path (which would otherwise change the rule identity for the
    same cached ``.o`` when another example uses a different ``build/``).
    """
    dep_abs = dep_path.resolve()
    if mf_relative_to is not None:
        root = mf_relative_to.resolve()
        try:
            dp = os.path.relpath(dep_abs, root).replace("\\", "/")
        except ValueError:
            dp = str(dep_abs)
    else:
        dp = str(dep_abs)
    out = list(argv)
    i = 0
    while i < len(out):
        tok = out[i]
        if tok == "-MF":
            if i + 1 < len(out):
                out[i + 1] = dp
            i += 2
            continue
        if tok.startswith("-MF") and len(tok) > 3:
            out[i] = "-MF" + dp
            i += 1
            continue
        i += 1
    return out


def _mtime_skip_cache_compile(plan: BuildPlan, so) -> bool:
    """True for shared-cache TUs: compile only when source is newer than the .o (see ``cache_compile``)."""
    return plan.object_cache_dir is not None and so.kind in (
        "core",
        "variant",
        "lib",
    )


def _ccache_enabled() -> bool:
    """True when ``ccache`` is on PATH and ``ACMAKE_CCACHE`` is not a disable token."""
    v = os.environ.get("ACMAKE_CCACHE", "1").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return shutil.which("ccache") is not None


def _prefix_ccache(argv: list[str]) -> list[str]:
    """Prefix *argv* with ``ccache`` so custom compile commands go through the compiler cache.

    Arduino recipes are ``add_custom_command``s, not CMake ``C/CXX`` language compiles, so
    ``CMAKE_C_COMPILER_LAUNCHER`` does not apply. Sketch, core, variant, and library TUs
    all take this path. No-op when ccache is missing, disabled, or already the first token.

    Do not set ``CCACHE_BASEDIR`` to the source tree when Ninja's working
    directory is outside it (ACmake uses the sketch ``build_dir``). ccache then
    rewrites ``-iprefix`` / ``@flags/includes`` relative to that CWD and
    ``__has_include`` can fail, leaving library TUs like ``PPP.cpp`` empty.
    """
    if not argv or not _ccache_enabled():
        return list(argv)
    head = Path(argv[0]).name.lower()
    if head in ("ccache", "ccache.exe"):
        return list(argv)
    ccache = shutil.which("ccache")
    if not ccache:
        return list(argv)
    return [ccache, *argv]


def _strip_sketch_build_deps_for_shared_cache(
    plan: BuildPlan, so, rsp_deps: list[str]
) -> list[str]:
    """Drop paths under *plan.build_dir* from Ninja ``DEPENDS`` for cached TUs.

    Cached ``.o`` files live under the FQBN object cache and are shared across sketches.
    Response files under the sketch ``build_dir`` (e.g. ``build_opt.h``, ``file_opts``)
    become per-sketch absolute paths in ``DEPENDS``. A **new** example’s copies then look
    like new inputs to Ninja, so every linked core / variant / library TU is re-run even
    when ``maybe_refresh_object_cache`` sees unchanged ``build_opt`` bytes and headers.

    Shared-cache sources must not depend on sketch-owned paths for correctness beyond
    those stubs; real ``build_opt`` / header changes are handled by cache invalidation
    (content-keyed subtree + stamps). Sketch TUs keep the full DEPENDS list.
    """
    if plan.object_cache_dir is None or so.kind not in ("core", "variant", "lib"):
        return rsp_deps
    bd = plan.build_dir.resolve()
    out: list[str] = []
    for s in rsp_deps:
        p = Path(s)
        try:
            rp = p.resolve()
            rp.relative_to(bd)
        except (ValueError, OSError):
            out.append(s)
            continue
    return out


def _strip_sketch_build_deps_for_shared_lib(
    plan: BuildPlan, so, rsp_deps: list[str]
) -> list[str]:
    """Alias for :func:`_strip_sketch_build_deps_for_shared_cache` (core/variant/lib)."""
    return _strip_sketch_build_deps_for_shared_cache(plan, so, rsp_deps)


def _emit_depfile(dep_path: Path, argv: list[str]) -> bool:
    """True if Ninja should use DEPFILE for this compile (matches gcc -MF target)."""
    j = argv_text_for_depfile_detection(argv)
    if "-MMD" not in j and "-MD" not in j:
        return False
    if "-MF" not in j:
        return False
    dp = str(dep_path.resolve()).replace("\\", "/")
    return dp in j


def write_cmake(plan: BuildPlan, cmake_lists_path: Path | None = None) -> Path:
    """Write CMakeLists.txt into the build directory."""
    if cmake_lists_path is None:
        cmake_lists_path = plan.build_dir / "CMakeLists.txt"
    cmake_lists_path.parent.mkdir(parents=True, exist_ok=True)

    bd = plan.build_dir.resolve()
    core_objs = [so.object_path for so in plan.sources if so.kind == "core"]
    other_objs = link_combine_object_paths(plan)

    py_exe = str(Path(sys.executable).resolve())
    lines: list[str] = [
        "cmake_minimum_required(VERSION 3.20)",
        "project(acmake_sketch NONE)",
        "set(CMAKE_EXPORT_COMPILE_COMMANDS ON)",
        f"set(ACMAKE_BUILD_DIR {_q(bd)})",
        "",
    ]

    for so in plan.sources:
        cmd = _recipe_for_source(plan, so)
        if not cmd.strip():
            continue
        mtime_cache = _mtime_skip_cache_compile(plan, so)
        # Fresh shared-cache objects are still link/archive inputs; do not give
        # Ninja a custom command that would only spawn cache_compile.py to no-op.
        if mtime_cache and should_skip_cached_compile(so.object_path, so.source):
            lines.append(
                f"# compile {so.kind}: {so.source.name} (objcache fresh; no Ninja rule)"
            )
            lines.append("")
            continue
        argv = split_recipe(cmd)
        if not argv:
            continue
        argv, stub_deps = resolve_build_dir_at_file_argv(bd, argv)
        argv = _argv_set_depfile_mf(
            argv, so.dep_path, mf_relative_to=bd if mtime_cache else None
        )
        rsp_deps = stub_deps + collect_at_file_dep_paths(argv)
        rsp_deps = _strip_sketch_build_deps_for_shared_cache(plan, so, rsp_deps)
        rsp_deps_sorted = sorted(dict.fromkeys(rsp_deps), key=lambda p: str(p).replace("\\", "/"))
        if so.kind == "core":
            argv.insert(1, "-DARDUINO_CORE_BUILD")
        argv = _prefix_ccache(argv)
        dep_rel = os.path.relpath(so.dep_path, bd).replace("\\", "/")
        if mtime_cache:
            argv = [
                py_exe,
                _CACHE_COMPILE_SCRIPT,
                str(so.object_path),
                str(so.source),
                "--",
                *argv,
            ]
        lines.append(f"# compile {so.kind}: {so.source.name}")
        lines.append("add_custom_command(")
        lines.append(f"  OUTPUT {_q(so.object_path)}")
        lines.extend(_argv_to_cmake_command(argv).splitlines())
        dep_extra = " ".join(_q(Path(d)) for d in rsp_deps_sorted)
        deps = f"{_q(so.source)}"
        if dep_extra:
            deps += " " + dep_extra
        lines.append(f"  DEPENDS {deps}")
        if _emit_depfile(so.dep_path, argv) and not mtime_cache:
            lines.append(f"  DEPFILE {_q(dep_rel)}")
        lines.append(f"  WORKING_DIRECTORY {_q(bd)}")
        lines.append("  VERBATIM")
        lines.append(")")
        lines.append("")

    archive_path = plan.archive_path
    archive_out = ""
    if archive_path and core_objs:
        ar_path = (
            plan.expanded.get("compiler.path", "")
            + plan.expanded.get("compiler.ar.cmd", "ar")
        )
        from acmake.properties import expand_template

        ar_exe = expand_template(ar_path, plan.expanded).strip().strip('"')
        argv = [ar_exe, "rcs", str(archive_path)] + [str(o) for o in core_objs]
        lines.append("# core archive (ar rcs)")
        lines.append("add_custom_command(")
        lines.append(f"  OUTPUT {_q(archive_path)}")
        lines.extend(_argv_to_cmake_command(argv).splitlines())
        lines.append(
            "  DEPENDS "
            + " ".join(_q(o) for o in sorted(core_objs, key=lambda p: str(p).replace("\\", "/")))
        )
        lines.append(f"  WORKING_DIRECTORY {_q(bd)}")
        lines.append("  VERBATIM")
        lines.append(")")
        lines.append("")

    link_cmd = _expand_combine(plan, other_objs)
    if not link_cmd.strip():
        recipe_keys = sorted(
            k for k in plan.expanded if k.lower().startswith("recipe.")
        )
        hint = (
            "No link recipe found (tried recipe.c.combine.pattern[.<os>], "
            "numbered .1/.2…, recipe.cpp.combine.pattern, and other recipe.*combine*pattern keys)."
        )
        raise ValueError(
            f"{hint} Known recipe.* keys on this platform (sample): "
            f"{recipe_keys[:40]!r}{'…' if len(recipe_keys) > 40 else ''}"
        )
    link_argv = split_recipe(link_cmd)
    # Link-time response files (e.g. ESP32 ``link_libs.rsp`` refreshed by
    # ``recipe.hooks.linking.prelink``) must be read by the linker, not frozen into the
    # generated argv when CMakeLists.txt is written.
    link_argv, link_stub = resolve_build_dir_at_file_argv(bd, link_argv, inline=False)
    link_rsp = link_stub + collect_at_file_dep_paths(link_argv)
    elf_path = plan.elf_path
    assert elf_path is not None
    link_deps = list(other_objs)
    if archive_path and core_objs:
        link_deps.append(archive_path)
    link_dep_strs = [_q(p) for p in link_deps if p is not None]
    link_dep_strs.extend(_q(Path(d)) for d in link_rsp)
    link_dep_strs.sort()

    lines.append("# link")
    lines.append("add_custom_command(")
    lines.append(f"  OUTPUT {_q(elf_path)}")
    lines.extend(_argv_to_cmake_command(link_argv).splitlines())
    lines.append("  DEPENDS " + " ".join(link_dep_strs))
    lines.append(f"  WORKING_DIRECTORY {_q(bd)}")
    lines.append("  VERBATIM")
    lines.append(")")
    lines.append("")

    pre_stamp_path: Path | None = None
    pre_cmds = collect_ordered_hook_shell_commands(
        plan.expanded, objcopy_pre_recipe_hook_phases(plan.expanded)
    )
    if pre_cmds:
        pre_stamp_path = bd / ".acmake_stamp_preobjcopy"
        pre_script = _write_hook_runner(bd, "preobjcopy", pre_cmds)
        _emit_objcopy_hook_stamp(
            lines,
            bd=bd,
            stamp=pre_stamp_path,
            depends_quoted=[_q(elf_path)],
            script=pre_script,
            cmake_comment="recipe.hooks.objcopy (before recipe.objcopy.*)",
        )

    final: list[str] = [_q(elf_path)]
    objcopy_outputs: list[str] = []
    objcopy_out_paths: list[Path] = []

    for recipe_key in ordered_objcopy_recipe_keys(plan.expanded):
        cmd = _expand_objcopy(plan, recipe_key)
        if not cmd.strip():
            continue
        argv = split_recipe(cmd)
        if not argv:
            continue
        stem = stem_from_objcopy_recipe_key(recipe_key)
        argv, stub = resolve_build_dir_at_file_argv(bd, argv, inline=False)
        rsp = stub + collect_at_file_dep_paths(argv)
        recipe_out = infer_objcopy_output_path(plan, stem, argv)
        recipe_out_r = recipe_out.resolve()
        dep_parts = [_q(elf_path)] + [
            _q(Path(d)) for d in sorted(dict.fromkeys(rsp), key=str)
        ]
        if pre_stamp_path is not None:
            dep_parts.append(_q(pre_stamp_path))
        seen: set[str] = set(dep_parts)
        for tok in argv:
            p = Path(tok)
            cand = (p if p.is_absolute() else (bd / p)).resolve()
            try:
                cand.relative_to(bd)
            except ValueError:
                continue
            if cand.resolve() == recipe_out_r:
                # OUTPUT is listed in argv (e.g. gen_esp32part destination); do not
                # DEPENDS on it or Ninja sees ``partitions.bin -> partitions.bin``.
                continue
            if cand.is_file():
                s = _q(cand)
                if s not in seen:
                    seen.add(s)
                    dep_parts.append(s)

        lines.append(f"# objcopy {stem} ({recipe_key})")
        lines.append("add_custom_command(")
        lines.append(f"  OUTPUT {_q(recipe_out)}")
        lines.extend(_argv_to_cmake_command(argv).splitlines())
        lines.append("  DEPENDS " + " ".join(sorted(dep_parts)))
        lines.append(f"  WORKING_DIRECTORY {_q(bd)}")
        lines.append("  VERBATIM")
        lines.append(")")
        lines.append("")
        objcopy_outputs.append(_q(recipe_out))
        objcopy_out_paths.append(recipe_out)

    post_stamp_path: Path | None = None
    post_cmds = collect_ordered_hook_shell_commands(
        plan.expanded, objcopy_post_recipe_hook_phases(plan.expanded)
    )
    if post_cmds:
        post_stamp_path = bd / ".acmake_stamp_postobjcopy"
        post_script = _write_hook_runner(bd, "postobjcopy", post_cmds)
        post_deps = (
            [_q(p) for p in objcopy_out_paths]
            if objcopy_out_paths
            else [_q(elf_path)]
        )
        _emit_objcopy_hook_stamp(
            lines,
            bd=bd,
            stamp=post_stamp_path,
            depends_quoted=post_deps,
            script=post_script,
            cmake_comment="recipe.hooks.objcopy.postobjcopy (after recipe.objcopy.*)",
        )

    if post_stamp_path is not None:
        final = [_q(post_stamp_path)]
        final_paths = [post_stamp_path]
    elif objcopy_outputs:
        final = objcopy_outputs
        final_paths = list(objcopy_out_paths)
    elif pre_stamp_path is not None:
        final = [_q(pre_stamp_path)]
        final_paths = [pre_stamp_path]
    else:
        final = [_q(elf_path)]
        final_paths = [elf_path]

    # Per-stage milestone targets consumed by the CLI when build-stage hooks exist,
    # so ``recipe.hooks.sketch|libraries|core|linking.*`` run at their exact boundary
    # (see ``acmake.hooks.BUILD_STAGE_HOOKS``).
    sketch_objs = [so.object_path for so in plan.sources if so.kind == "sketch"]
    library_objs = [so.object_path for so in plan.sources if so.kind == "lib"]
    core_objs_all = [
        so.object_path for so in plan.sources if so.kind in ("core", "variant")
    ]
    core_stage: list[Path] = list(core_objs_all)
    if archive_path and core_objs:
        core_stage.append(archive_path)

    def _emit_milestone(name: str, deps: list[Path]) -> None:
        uniq = list(dict.fromkeys(deps))
        if uniq:
            lines.append(
                f"add_custom_target({name} DEPENDS "
                + " ".join(_q(p) for p in uniq)
                + ")"
            )
        else:
            lines.append(f"add_custom_target({name})")
        lines.append("")

    _emit_milestone("acmake_sketch_objects", sketch_objs)
    _emit_milestone("acmake_libraries_objects", library_objs)
    _emit_milestone("acmake_core_objects", core_stage)
    _emit_milestone("acmake_link", [elf_path])
    _emit_milestone("acmake_objcopy", final_paths)

    lines.append(
        "add_custom_target(acmake_sketch ALL DEPENDS " + " ".join(final) + ")"
    )
    lines.append("")

    content = "\n".join(lines) + "\n"
    blob = content.encode("utf-8")
    if cmake_lists_path.is_file():
        try:
            if cmake_lists_path.read_bytes() == blob:
                return cmake_lists_path
        except OSError:
            pass
    cmake_lists_path.write_bytes(blob)
    return cmake_lists_path
