"""Invalidate shared object cache when platform or library inputs change (per cache subtree)."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Mapping
from pathlib import Path

from acmake.fqbn import path_component_from_label
from acmake.libraries import (
    Library,
    _LIB_SOURCE_EXTS,
    _is_excluded_lib_scan_path,
)
from acmake.parse_txt import load_properties_file

_HEADER_SUFFIX = frozenset({".h", ".hh", ".hpp", ".hxx", ".H"})


def core_version_for_cache(expanded: dict[str, str], platform_root: Path) -> str:
    """Board package / core version string for cache partitioning."""
    for k in ("version", "runtime.platform.version", "platform.version"):
        v = (expanded.get(k) or "").strip()
        if v:
            return v
    raw = load_properties_file(platform_root / "platform.txt")
    v = (raw.get("version") or "").strip()
    if v:
        return v
    return (raw.get("name") or "unknown").strip() or "unknown"


def _fingerprint_headers(paths: list[Path]) -> str:
    """Stable hash from sorted header paths + mtime + size."""
    h = hashlib.sha256()
    for p in paths:
        try:
            st = p.stat()
            h.update(str(p.resolve()).encode("utf-8"))
            h.update(b"\0")
            h.update(str(st.st_mtime_ns).encode("ascii"))
            h.update(b"\0")
            h.update(str(st.st_size).encode("ascii"))
            h.update(b"\n")
        except OSError:
            continue
    return h.hexdigest()


def _platform_header_paths(core_path: Path, variant_path: Path) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for root in (core_path, variant_path):
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix not in _HEADER_SUFFIX:
                continue
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(p)
    out.sort(key=lambda x: str(x.resolve()).lower())
    return out


def _library_cache_input_paths(lib: Library) -> list[Path]:
    """Source and header paths under the library install (same coverage as compile inputs)."""
    roots: set[Path] = {lib.root.resolve()}
    for ir in lib.include_roots:
        roots.add(ir.resolve())
    seen: set[Path] = set()
    out: list[Path] = []
    for root in sorted(roots, key=lambda x: str(x).lower()):
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if _is_excluded_lib_scan_path(p):
                continue
            suf = p.suffix.lower()
            if suf not in _LIB_SOURCE_EXTS:
                continue
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(p)
    out.sort(key=lambda x: str(x.resolve()).lower())
    return out


def _fingerprint_library_cache_inputs(lib: Library) -> str:
    """Hash install root, ``library.properties``, and **contents** of library sources/headers.

    Previously the stamp used header mtimes, so indexing tools or a new ``git clone``
    touching mtimes wiped cached ``.o`` files even when bytes were unchanged. Invalidation
    now follows source/header **content** plus metadata in ``library.properties`` (version,
    ``depends=``, ``includes=``, …) and the resolved install path (FQBN cache already scopes
    the board package / core version).
    """
    h = hashlib.sha256()
    h.update(str(lib.root.resolve()).encode("utf-8"))
    h.update(b"\0")
    prop = lib.root / "library.properties"
    if prop.is_file():
        try:
            h.update(prop.read_bytes())
        except OSError:
            pass
    h.update(b"\n")
    lib_root = lib.root.resolve()
    for p in _library_cache_input_paths(lib):
        try:
            rel = str(p.resolve().relative_to(lib_root)).replace("\\", "/").lower()
        except ValueError:
            rel = p.name.lower()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            pass
        h.update(b"\n")
    return h.hexdigest()


def _sdk_config_input_paths(sdk_path: Path) -> list[Path]:
    """Config-affecting files under an ESP-style SDK (``compiler.sdk.path``).

    The generated ``sdkconfig`` plus every file under ``flags/`` (``defines``,
    ``includes``, ``c_flags``, ``cpp_flags``, ``S_flags``, …). The recipe bakes these
    straight onto every compile line via ``@flags/defines`` / ``@flags/*_flags`` and the
    ``sdkconfig.h`` regenerated from ``sdkconfig`` is ``#include``-d by the core and
    bundled libraries, so their bytes fully determine preprocessor config. They are small
    (tens of KB total) and change whenever the SDK is regenerated with a different Kconfig.
    The large precompiled ``lib/`` and ``ld/`` trees are intentionally excluded: they only
    affect the (uncached) final link, not the ``.o`` files this cache stores.
    """
    out: list[Path] = []
    sc = sdk_path / "sdkconfig"
    if sc.is_file():
        out.append(sc)
    flags = sdk_path / "flags"
    if flags.is_dir():
        for p in sorted(flags.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file():
                out.append(p)
    return out


def sdk_config_fingerprint(expanded: Mapping[str, str]) -> str:
    """SHA-256 hex digest of the platform SDK config inputs, or ``""`` when there is none.

    Resolves ``compiler.sdk.path`` from *expanded* properties and hashes the **contents**
    of the config files under it (see ``_sdk_config_input_paths``), keyed by filename only
    so the digest is stable across install locations. Toolchains that do not define
    ``compiler.sdk.path`` (or whose SDK tree is absent) return ``""``, which leaves
    ``FQBN.object_cache_key`` unchanged for them.
    """
    raw = (expanded.get("compiler.sdk.path") or "").strip()
    if not raw:
        return ""
    sdk_path = Path(raw)
    h = hashlib.sha256()
    any_file = False
    for p in _sdk_config_input_paths(sdk_path):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        any_file = True
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\n")
    if not any_file:
        return ""
    return h.hexdigest()


def _build_opt_bytes(build_dir: Path) -> bytes:
    """Raw ``build_opt.h`` bytes from *build_dir* (missing file is treated like empty).

    Only **contents** matter for fingerprints; mtimes and path are ignored, so two
    sketches with separate ``build/`` copies but identical bytes get the same digest.
    """
    p = build_dir / "build_opt.h"
    if not p.is_file():
        return b""
    try:
        return p.read_bytes()
    except OSError:
        return b""


def _platform_header_fingerprint(core_path: Path, variant_path: Path) -> str:
    """Fingerprint of core + variant headers only (shared across sketches for one FQBN)."""
    hp = _fingerprint_headers(_platform_header_paths(core_path, variant_path))
    return hashlib.sha256(hp.encode("ascii")).hexdigest()


def _build_opt_fingerprint(build_dir: Path) -> str:
    """SHA-256 hex digest of ``build_opt.h`` **contents** under *build_dir*.

    Uses ``read_bytes()`` only (no mtime). Two different sketch build directories with
    byte-identical ``build_opt.h`` produce the same fingerprint.
    """
    return hashlib.sha256(_build_opt_bytes(build_dir)).hexdigest()


def _wipe_object_dir(o_dir: Path) -> None:
    if not o_dir.is_dir():
        return
    for p in o_dir.iterdir():
        if p.is_file():
            p.unlink(missing_ok=True)


def _wipe_core_variant_and_all_lib_objects(cache_root: Path) -> None:
    """Remove cached ``.o`` / ``core.a`` for core, variant, and every library bundle."""
    _wipe_object_dir(cache_root / "core" / "o")
    ca = cache_root / "core" / "core.a"
    ca.unlink(missing_ok=True)
    _wipe_object_dir(cache_root / "variant" / "o")
    lib_top = cache_root / "lib"
    if lib_top.is_dir():
        for bundle in lib_top.iterdir():
            if bundle.is_dir():
                _wipe_object_dir(bundle / "o")
                (bundle / ".acmake_hdr_fp").unlink(missing_ok=True)


def _library_bundle_path(cache_root: Path, lib: Library) -> Path:
    """Match ``acmake.build._library_bundle_dir`` (avoid import cycle)."""
    h = hashlib.sha1(str(lib.root.resolve()).encode()).hexdigest()[:12]
    lab = path_component_from_label(lib.name)
    return cache_root / "lib" / f"{lab}_{h}"


def _library_bundle_cache_stamp(lib: Library, build_dir: Path) -> str:
    """Value stored in ``bundle/.acmake_hdr_fp``: library inputs plus ``build_opt`` **content** digest.

    The build-opt part is ``_build_opt_fingerprint(build_dir)`` (bytes only). Examples with
    the same flags text therefore share one stamp even though each sketch has its own path;
    mtime-only churn on ``build_opt.h`` does not change the digest. Different flag **bytes**
    still invalidate cached library ``.o`` files as intended.
    """
    lib_fp = _fingerprint_library_cache_inputs(lib)
    bo_fp = _build_opt_fingerprint(build_dir)
    return hashlib.sha256(f"{lib_fp}\0{bo_fp}".encode("ascii")).hexdigest()


def maybe_refresh_object_cache(
    cache_root: Path,
    *,
    build_dir: Path,
    core_path: Path,
    variant_path: Path,
    libraries: list[Library],
) -> None:
    """Drop stale cached ``.o`` / ``core.a`` when platform or library inputs change.

    **Platform headers** (core + variant) are fingerprinted under *cache_root* only —
    they do not depend on which sketch is being built.

    **``build_opt.h``** and the **platform SDK config** do **not** trigger a global wipe
    here: ``prepare_build`` chooses *cache_root* using
    ``FQBN.object_cache_key(..., build_opt_fingerprint=…, sdk_fingerprint=…)``, so different
    ``build_opt.h`` **bytes** or a regenerated SDK (``sdkconfig`` / ``flags`` — see
    ``sdk_config_fingerprint``) already live under different
    ``<temp>/acmake_objcache/<32 hex>/`` subtrees (same inputs → same key, including across
    sketch ``build/`` paths). This function only reconciles platform-header drift and
    per-library source digests within that subtree.

    **Per-library** stamps (``.acmake_hdr_fp`` under each ``lib/…`` bundle) combine the
    library input digest with ``_build_opt_fingerprint(build_dir)`` (``build_opt.h`` **bytes**
    only) so callers that reuse one *cache_root* for several ``build_dir`` values (e.g. tests)
    still invalidate library ``.o`` files when build flags differ. Mtimes-only churn on
    library sources or on ``build_opt.h`` does not invalidate by itself.
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    stamps = cache_root / ".acmake_stamps"
    stamps.mkdir(parents=True, exist_ok=True)
    hdr_path = stamps / "platform_hdr_fp"
    cur_hdr = _platform_header_fingerprint(core_path, variant_path)
    old_hdr = hdr_path.read_text(encoding="utf-8").strip() if hdr_path.is_file() else None

    need_wipe = False
    if old_hdr is not None and cur_hdr != old_hdr:
        need_wipe = True

    if need_wipe:
        _wipe_core_variant_and_all_lib_objects(cache_root)

    hdr_path.write_text(cur_hdr + "\n", encoding="utf-8")

    for lib in libraries:
        bundle = _library_bundle_path(cache_root, lib)
        if not bundle.is_dir():
            continue
        cur_stamp = _library_bundle_cache_stamp(lib, build_dir)
        lib_stamp = bundle / ".acmake_hdr_fp"
        if not lib_stamp.is_file():
            lib_stamp.write_text(cur_stamp + "\n", encoding="utf-8")
            continue
        old_stamp = lib_stamp.read_text(encoding="utf-8").strip()
        if cur_stamp != old_stamp:
            _wipe_object_dir(bundle / "o")
            lib_stamp.write_text(cur_stamp + "\n", encoding="utf-8")


def clear_entire_object_cache(temp_parent: Path | None = None) -> Path:
    """Remove ``<temp>/acmake_objcache``. Returns the path removed or that would be removed."""
    import tempfile

    base = Path(temp_parent or tempfile.gettempdir()) / "acmake_objcache"
    if base.is_dir():
        shutil.rmtree(base)
    return base
