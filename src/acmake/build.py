"""Assemble a resolved build: properties, sources, includes, recipes."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from acmake.build_options_json import write_build_options_json
from acmake.cache_invalidate import (
    _build_opt_fingerprint,
    core_version_for_cache,
    maybe_refresh_object_cache,
    sdk_config_fingerprint,
)
from acmake.config import ArduinoPaths
from acmake.discovery import resolve_all_runtime_tools, resolve_platform_root
from acmake.fqbn import FQBN, path_component_from_label
from acmake.libraries import (
    Library,
    LibraryResolveNote,
    _iter_transitive_include_enqueues,
    _platform_header_stems,
    ancestor_library_container_dirs,
    ensure_build_libraries_stub_dirs,
    header_stem_index_for_linked_libraries,
    platform_header_scan_roots,
    resolve_libraries_for_sketch,
)
from acmake.properties import (
    collapse_duplicate_path_slashes_in_properties,
    default_menu_options_for_board,
    escape_dquoted_defines_in_properties,
    expand_properties,
    expand_template,
    inject_build_paths,
    load_boards,
    load_platform,
    merge_board_properties,
    merge_platform_and_board,
    merged_with_build_properties,
    normalize_compiler_warning_level,
)
from acmake.hooks import PRE_SKETCH_HOOK_PHASES, run_hook_phases
from acmake.response_files import ensure_build_response_stubs
from acmake.sketch import (
    build_sketch_cpp_body,
    list_sketch_inos,
    preprocess_sketch,
    sketch_build_project_name,
)
from acmake.sources import (
    collect_core_sources,
    collect_library_sources,
    collect_sketch_extra_sources,
    collect_variant_sources,
)


@dataclass
class SourceObject:
    source: Path
    object_path: Path
    dep_path: Path
    kind: str  # core | variant | lib | sketch
    # Set for ``kind == "lib"`` so cached compiles can use a TU-local ``-I`` list (see ``includes_string_for_cached_lib_compile``).
    lib: Library | None = None


@dataclass
class BuildPlan:
    fqbn: FQBN
    platform_root: Path
    sketch_dir: Path
    build_dir: Path
    expanded: dict[str, str]
    sources: list[SourceObject] = field(default_factory=list)
    archive_path: Path | None = None
    elf_path: Path | None = None
    hex_path: Path | None = None
    eep_path: Path | None = None
    bin_path: Path | None = None
    libraries: list[Library] = field(default_factory=list)
    library_resolution_notes: list[LibraryResolveNote] = field(default_factory=list)
    # ``<temp>/acmake_objcache/<object_cache_key>/`` when object cache is on (see prepare_build).
    # *object_cache_key* folds in ``build_opt.h`` bytes so different flags use separate trees.
    object_cache_dir: Path | None = None
    # Stems of headers under core/variant (see ``_platform_header_stems``): used when building
    # per-library ``-I`` strings for shared-cache compiles so tokens match the sketch resolver.
    platform_header_stems: frozenset[str] = field(default_factory=frozenset)


def _build_properties_cache_tag(specs: list[str] | None) -> str | None:
    """Stable short tag for ``--build-property`` list (object cache partitioning)."""
    if not specs:
        return None
    norm = sorted(s.strip() for s in specs if s and str(s).strip())
    if not norm:
        return None
    h = hashlib.sha256("\0".join(norm).encode("utf-8")).hexdigest()
    return h[:24]


def _object_cache_root_for_fqbn(
    fqbn: FQBN,
    core_version: str,
    compiler_warnings: str | None = None,
    build_property_tag: str | None = None,
    *,
    build_dir: Path | None = None,
    sdk_fingerprint: str | None = None,
) -> Path:
    """Shared cache root for FQBN + core version + ``build_opt.h`` content + SDK config (+ optional warnings / props).

    *sdk_fingerprint* (see ``sdk_config_fingerprint``) scopes the tree to the platform
    SDK's ``sdkconfig`` / ``flags`` so a regenerated SDK with different Kconfig never
    reuses ``.o`` files built against the old config, even when the core version is
    unchanged.
    """
    bo_fp: str | None = None
    if build_dir is not None:
        bo_fp = _build_opt_fingerprint(build_dir)
    return Path(tempfile.gettempdir()) / "acmake_objcache" / fqbn.object_cache_key(
        core_version=core_version,
        compiler_warnings=compiler_warnings,
        build_property_tag=build_property_tag,
        build_opt_fingerprint=bo_fp,
        sdk_fingerprint=sdk_fingerprint,
    )


def _library_bundle_dir(cache_root: Path, lib: Library) -> Path:
    """Per-library subtree under ``…/lib/`` (name + short hash of install path)."""
    h = hashlib.sha1(str(lib.root.resolve()).encode()).hexdigest()[:12]
    lab = path_component_from_label(lib.name)
    return cache_root / "lib" / f"{lab}_{h}"


_WIN_BAD_OBJECT_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_object_path_component(name: str) -> str:
    return _WIN_BAD_OBJECT_NAME.sub("_", name.replace("\\", "/"))


def _object_basename_for_translation_unit(
    src: Path,
    kind: str,
    *,
    core_path: Path,
    variant_path: Path,
    sketch_dir: Path,
    sketch_build: Path,
    lib: Library | None,
) -> str:
    """``.o`` filename derived from the source path (unique per core/variant/lib/sketch tree)."""
    s = src.resolve()
    rel: Path
    if kind == "core":
        try:
            rel = s.relative_to(core_path.resolve())
        except ValueError:
            rel = Path(s.name)
    elif kind == "variant":
        try:
            rel = s.relative_to(variant_path.resolve())
        except ValueError:
            rel = Path(s.name)
    elif kind == "lib":
        if lib is None:
            raise ValueError("lib= required for kind 'lib'")
        try:
            rel = s.relative_to(lib.root.resolve())
        except ValueError:
            rel = Path(s.name)
    elif kind == "sketch":
        rel = Path(s.name)
        for anchor in (sketch_dir.resolve(), sketch_build.resolve()):
            try:
                rel = s.relative_to(anchor)
                break
            except ValueError:
                continue
    else:
        rel = Path(s.name)
    parts = [_sanitize_object_path_component(p) for p in rel.parts if p not in (".",)]
    key = "__".join(parts) if parts else "_source"
    if not key.strip("_"):
        key = "_source"
    return f"{key}.o"


def _object_cache_enabled(use_object_cache: bool) -> bool:
    if not use_object_cache:
        return False
    v = os.environ.get("ACMAKE_OBJECT_CACHE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _build_includes(
    expanded: dict[str, str], lib_include_roots: list[Path]
) -> str:
    parts: list[str] = []
    core_p = expanded.get("build.core.path", "").rstrip("/")
    if core_p:
        parts.append(core_p)
    var_p = expanded.get("build.variant.path", "").rstrip("/")
    if var_p and var_p != core_p:
        parts.append(var_p)
    for lib in lib_include_roots:
        parts.append(str(lib))
    return " ".join(f'-I"{p}"' for p in parts)


def _lib_transitive_include_roots(
    libraries: list[Library],
    root_lib: Library,
    platform_header_stems: frozenset[str],
) -> list[Path]:
    """Include roots for *root_lib* plus linked deps: ``depends=`` and ``#include`` tokens among *libraries*."""
    stem_to_lib = header_stem_index_for_linked_libraries(libraries, platform_header_stems)
    by_name: dict[str, Library] = {}
    for lib in libraries:
        by_name[lib.name.lower()] = lib
        by_name[lib.root.name.lower()] = lib

    collected: dict[str, Library] = {}
    q: deque[Library] = deque()

    def add(lib: Library) -> None:
        k = lib.name.lower()
        if k in collected:
            return
        collected[k] = lib
        q.append(lib)

    add(root_lib)
    while q:
        cur = q.popleft()
        for dep in sorted(cur.depends, key=str.lower):
            child = by_name.get(dep.lower())
            if child is not None:
                add(child)
        for stem, _ in _iter_transitive_include_enqueues(cur):
            dep = stem_to_lib.get(stem.lower())
            if dep is not None:
                add(dep)

    out: list[Path] = []
    seen_path: set[Path] = set()
    for lib in sorted(collected.values(), key=lambda L: L.name.lower()):
        for r in lib.include_roots:
            try:
                rp = r.resolve()
            except OSError:
                rp = r
            if rp not in seen_path:
                seen_path.add(rp)
                out.append(rp)
    return out


def includes_string_for_cached_lib_compile(
    expanded: dict[str, str],
    libraries: list[Library],
    lib: Library,
    platform_header_stems: frozenset[str],
) -> str:
    """``-I`` string for one library TU when using the shared object cache.

    The sketch-wide ``includes`` string lists **every** linked library, so two
    sketches that both use WiFi but differ in other libraries would otherwise get
    different compiler argv for ``WiFi.cpp`` and Ninja would re-run the cached rule
    even though WiFi sources and core/variant are unchanged. This string is the
    closure of *lib* over ``depends`` and ``#include`` tokens that resolve to other
    **linked** libraries (same rules as ``resolve_libraries_for_sketch`` stem matching).
    """
    roots = _lib_transitive_include_roots(libraries, lib, platform_header_stems)
    return _build_includes(expanded, roots)


def prepare_build(
    fqbn: FQBN,
    sketch_dir: Path,
    build_dir: Path,
    paths: ArduinoPaths,
    *,
    runtime_ide_version: str = "10607",
    use_preprocessor: bool = True,
    verbose: bool = False,
    use_object_cache: bool = True,
    compiler_warnings: str | None = None,
    build_properties: list[str] | None = None,
) -> BuildPlan:
    sketch_dir = sketch_dir.resolve()
    build_dir = build_dir.resolve()
    build_dir.mkdir(parents=True, exist_ok=True)

    platform_root = resolve_platform_root(
        paths.packages_dir,
        fqbn.vendor,
        fqbn.arch,
        sketch_dir=sketch_dir,
        user_dir=paths.user_dir,
    )
    platform_txt = load_platform(platform_root)
    boards_raw = load_boards(platform_root)
    menu_defaults = default_menu_options_for_board(fqbn.board_id, boards_raw)
    board_options = {**menu_defaults, **fqbn.options}
    board_props = merge_board_properties(fqbn.board_id, boards_raw, board_options)
    merged = merge_platform_and_board(platform_txt, board_props)
    merged = merged_with_build_properties(merged, build_properties)

    warn_for_cache: str | None = None
    if compiler_warnings:
        warn_for_cache = normalize_compiler_warning_level(compiler_warnings)
        wkey = f"compiler.warning_flags.{warn_for_cache}"
        if wkey in merged:
            merged["compiler.warning_flags"] = (merged[wkey] or "").strip()

    build_proj = sketch_build_project_name(sketch_dir)
    sketch_build = build_dir / "sketch"
    sketch_build.mkdir(parents=True, exist_ok=True)

    expanded_base = expand_properties(merged)
    expanded_inj = inject_build_paths(
        expanded_base,
        runtime_platform_path=str(platform_root) + "/",
        build_path=str(build_dir) + "/",
        build_project_name=build_proj,
        sketch_path=str(sketch_dir) + "/",
        runtime_ide_version=runtime_ide_version,
        fqbn_string=fqbn.to_string(),
        fqbn_arch=fqbn.arch,
    )
    rt_paths = resolve_all_runtime_tools(
        paths.packages_dir, expanded_inj, platform_root=platform_root
    )
    cur_early = expand_properties({**expanded_inj, **rt_paths})
    for _ in range(8):
        nxt = expand_properties(
            {
                **cur_early,
                **resolve_all_runtime_tools(
                    paths.packages_dir, cur_early, platform_root=platform_root
                ),
            }
        )
        if nxt == cur_early:
            break
        cur_early = nxt

    ensure_build_response_stubs(build_dir, sketch_dir)
    run_hook_phases(cur_early, build_dir, PRE_SKETCH_HOOK_PHASES, verbose=verbose)

    if use_preprocessor:
        sketch_cpp = preprocess_sketch(
            sketch_dir, sketch_build, verbose=verbose
        )
        sketch_body = sketch_cpp.read_text(encoding="utf-8")
    else:
        inos = list_sketch_inos(sketch_dir)
        sketch_body = build_sketch_cpp_body(inos)
        sketch_cpp = sketch_build / "sketch.cpp"
        if (
            not sketch_cpp.is_file()
            or sketch_cpp.read_text(encoding="utf-8") != sketch_body
        ):
            sketch_cpp.write_text(sketch_body, encoding="utf-8")

    lib_dirs = [
        *ancestor_library_container_dirs(sketch_dir),
        paths.user_libraries_dir,
        platform_root / "libraries",
        sketch_dir / "libraries",
    ]
    platform_hdr_roots = platform_header_scan_roots(
        Path(cur_early["build.core.path"]),
        Path(cur_early["build.variant.path"]),
        platform_root,
    )
    platform_header_stems = _platform_header_stems(list(platform_hdr_roots))
    resolution_notes: list[LibraryResolveNote] = []
    libraries = resolve_libraries_for_sketch(
        sketch_dir,
        sketch_body,
        fqbn.fqbn_arch,
        lib_dirs,
        platform_include_roots=platform_hdr_roots,
        resolution_notes=resolution_notes,
    )
    ensure_build_libraries_stub_dirs(build_dir, libraries)
    lib_roots: list[Path] = []
    for lib in libraries:
        lib_roots.extend(lib.include_roots)

    cur = cur_early
    includes = _build_includes(cur, lib_roots)
    expanded = expand_properties({**cur, "includes": includes})
    expanded = escape_dquoted_defines_in_properties(expanded)
    expanded = collapse_duplicate_path_slashes_in_properties(expanded)
    # ``includes`` must use the same collapsed core/variant paths as the rest of *expanded*.
    expanded["includes"] = _build_includes(expanded, lib_roots)

    obj_dir = build_dir / "objects"
    obj_dir.mkdir(parents=True, exist_ok=True)
    dep_dir = build_dir / ".acmake_deps"
    dep_dir.mkdir(parents=True, exist_ok=True)

    object_cache_dir: Path | None = None
    cache_root: Path | None = None
    if _object_cache_enabled(use_object_cache):
        core_ver = core_version_for_cache(expanded, platform_root)
        prop_tag = _build_properties_cache_tag(build_properties)
        sdk_fp = sdk_config_fingerprint(expanded)
        cache_root = _object_cache_root_for_fqbn(
            fqbn,
            core_ver,
            warn_for_cache,
            prop_tag,
            build_dir=build_dir,
            sdk_fingerprint=sdk_fp,
        )
        object_cache_dir = cache_root
        cache_root.mkdir(parents=True, exist_ok=True)
        maybe_refresh_object_cache(
            cache_root,
            build_dir=build_dir,
            core_path=Path(expanded["build.core.path"]),
            variant_path=Path(expanded["build.variant.path"]),
            libraries=libraries,
        )

    sources: list[SourceObject] = []
    core_path = Path(expanded["build.core.path"])
    variant_path = Path(expanded["build.variant.path"])

    def add_src(src: Path, kind: str, *, lib: Library | None = None) -> None:
        obj_fn = _object_basename_for_translation_unit(
            src,
            kind,
            core_path=core_path,
            variant_path=variant_path,
            sketch_dir=sketch_dir,
            sketch_build=sketch_build,
            lib=lib,
        )
        if kind == "sketch" or cache_root is None:
            base = obj_dir
        elif kind == "core":
            base = cache_root / "core" / "o"
            base.mkdir(parents=True, exist_ok=True)
        elif kind == "variant":
            base = cache_root / "variant" / "o"
            base.mkdir(parents=True, exist_ok=True)
        elif kind == "lib":
            if lib is None:
                raise ValueError("lib= required for kind 'lib'")
            base = _library_bundle_dir(cache_root, lib) / "o"
            base.mkdir(parents=True, exist_ok=True)
        else:
            base = obj_dir
        op = base / obj_fn
        # Canonicalize cached .o paths so compile argv matches across runs (e.g. /var vs /private/var).
        if cache_root is not None and kind != "sketch":
            try:
                op = Path(os.path.realpath(op))
            except OSError:
                op = op.resolve()
        # Keep .d files under the sketch build dir (CMake/Ninja DEPFILE must stay inside
        # ``CMAKE_BINARY_DIR``). Cached TU compile lines use ``-MF`` relative to this dir
        # (see ``cmakegen._argv_set_depfile_mf``) so the rule text is stable across sketches.
        dp = dep_dir / f"{obj_fn}.d"
        sources.append(
            SourceObject(
                source=src.resolve(),
                object_path=op,
                dep_path=dp,
                kind=kind,
                lib=lib if kind == "lib" else None,
            )
        )

    for s in collect_core_sources(core_path):
        add_src(s, "core")
    for s in collect_variant_sources(variant_path):
        add_src(s, "variant")
    for lib in libraries:
        for s in collect_library_sources(lib.root):
            add_src(s, "lib", lib=lib)
    add_src(sketch_cpp.resolve(), "sketch")
    for s in collect_sketch_extra_sources(sketch_dir):
        if s.resolve() != sketch_cpp.resolve():
            add_src(s.resolve(), "sketch")

    has_core = any(so.kind == "core" for so in sources)
    if object_cache_dir is not None and has_core:
        core_bundle = object_cache_dir / "core"
        core_bundle.mkdir(parents=True, exist_ok=True)
        try:
            archive_path = Path(os.path.realpath(core_bundle / "core.a"))
        except OSError:
            archive_path = (core_bundle / "core.a").resolve()
    else:
        archive_path = build_dir / "core.a"
    # Keep ``.elf`` / ``.bin`` / bootloader basenames aligned with ``{build.project_name}``
    # in platform recipes (typically ``SketchName.ino`` for ESP32 / library examples).
    product = expanded.get("build.project_name") or sketch_build_project_name(sketch_dir)
    expanded["build.project_name"] = product
    elf_path = build_dir / f"{product}.elf"
    hex_path = build_dir / f"{product}.hex"
    eep_path = build_dir / f"{product}.eep"
    bin_path = build_dir / f"{product}.bin"

    write_build_options_json(
        build_dir=build_dir,
        sketch_dir=sketch_dir,
        paths=paths,
        fqbn=fqbn,
        expanded=expanded,
        build_properties=build_properties,
        compiler_warnings=compiler_warnings,
    )

    return BuildPlan(
        fqbn=fqbn,
        platform_root=platform_root,
        sketch_dir=sketch_dir,
        build_dir=build_dir,
        expanded=expanded,
        sources=sources,
        archive_path=archive_path,
        elf_path=elf_path,
        hex_path=hex_path,
        eep_path=eep_path,
        bin_path=bin_path,
        libraries=libraries,
        library_resolution_notes=resolution_notes,
        object_cache_dir=object_cache_dir,
        platform_header_stems=platform_header_stems,
    )


def expand_recipe_for_source(
    expanded: dict[str, str], recipe_key: str, src: Path, obj: Path, includes: str
) -> str:
    """Fill per-file placeholders in a recipe pattern."""
    tmpl = expanded.get(recipe_key, "")
    if not tmpl:
        return ""
    ctx = dict(expanded)
    ctx["source_file"] = str(src)
    ctx["object_file"] = str(obj)
    ctx["includes"] = includes
    return expand_template(tmpl, ctx)


def _has_coverage_flags(expanded: dict[str, str]) -> bool:
    """True when ``--coverage`` appears in any compiler extra-flags property."""
    for key in ("compiler.c.extra_flags", "compiler.cpp.extra_flags"):
        if "--coverage" in expanded.get(key, ""):
            return True
    return False


def collect_gcov_notes(plan: BuildPlan) -> list[Path]:
    """Copy ``.gcno`` files from the object cache into ``<build_dir>/gcov/``.

    GCC places ``.gcno`` notes next to each ``.o`` file.  When the shared
    object cache is active, core / variant / library objects live in a temp
    directory outside the build tree.

    This helper copies every ``.gcno`` produced for the current build into
    ``<build_dir>/gcov/<kind>/`` where *kind* is ``core``, ``variant``,
    ``lib``, or ``sketch``.  Grouping by kind prevents name collisions when
    different source trees contain files with the same basename (e.g. both
    the core and a library ship ``IPAddress.cpp``).

    Returns the list of destination paths (empty when coverage is not enabled
    or no ``.gcno`` files exist).
    """
    if not _has_coverage_flags(plan.expanded):
        return []

    log = logging.getLogger("acmake")
    gcov_dir = plan.build_dir / "gcov"

    if gcov_dir.exists():
        shutil.rmtree(gcov_dir)

    copied: list[Path] = []

    for so in plan.sources:
        obj_name = so.object_path.name
        if not obj_name.endswith(".o"):
            continue
        gcno = so.object_path.with_name(obj_name[:-2] + ".gcno")
        if not gcno.is_file():
            continue
        kind_dir = gcov_dir / so.kind
        kind_dir.mkdir(parents=True, exist_ok=True)
        dest = kind_dir / gcno.name
        try:
            shutil.copy2(gcno, dest)
            copied.append(dest)
        except OSError as exc:
            log.warning("Failed to copy %s -> %s: %s", gcno, dest, exc)

    if copied:
        log.info("Copied %d .gcno files to %s", len(copied), gcov_dir)
    return copied
