"""Shared-cache library compile: Ninja DEPENDS must not pin sketch-local build paths."""

from pathlib import Path

from acmake.build import BuildPlan, SourceObject
from acmake.cmakegen import (
    _strip_sketch_build_deps_for_shared_cache,
    _strip_sketch_build_deps_for_shared_lib,
)
from acmake.fqbn import FQBN


def _lib_so(tmp_path: Path) -> SourceObject:
    d = tmp_path / "deps"
    d.mkdir()
    return SourceObject(
        source=tmp_path / "Lib.cpp",
        object_path=tmp_path / "Lib.cpp.o",
        dep_path=d / "Lib.cpp.o.d",
        kind="lib",
    )


def test_strip_removes_build_dir_paths_for_cached_lib(tmp_path: Path) -> None:
    sketch_build = tmp_path / "ex" / "build"
    sketch_build.mkdir(parents=True)
    opt = sketch_build / "build_opt.h"
    opt.write_bytes(b"")
    other = tmp_path / "sys" / "toolchain.h"
    other.parent.mkdir()
    other.write_bytes(b"")

    fqbn = FQBN("espressif", "esp32", "devkit")
    plan = BuildPlan(
        fqbn=fqbn,
        platform_root=tmp_path / "p",
        sketch_dir=tmp_path / "sk",
        build_dir=sketch_build,
        expanded={},
        object_cache_dir=tmp_path / "cache",
    )
    so = _lib_so(tmp_path)
    stripped = _strip_sketch_build_deps_for_shared_cache(
        plan,
        so,
        [str(opt.resolve()), str(other.resolve())],
    )
    assert stripped == [str(other.resolve())]
    assert _strip_sketch_build_deps_for_shared_lib(
        plan, so, [str(opt.resolve()), str(other.resolve())]
    ) == stripped


def test_strip_noop_without_object_cache(tmp_path: Path) -> None:
    sketch_build = tmp_path / "build"
    sketch_build.mkdir()
    opt = sketch_build / "build_opt.h"
    opt.write_bytes(b"")

    fqbn = FQBN("espressif", "esp32", "devkit")
    plan = BuildPlan(
        fqbn=fqbn,
        platform_root=tmp_path / "p",
        sketch_dir=tmp_path / "sk",
        build_dir=sketch_build,
        expanded={},
        object_cache_dir=None,
    )
    so = _lib_so(tmp_path)
    deps = [str(opt.resolve())]
    assert _strip_sketch_build_deps_for_shared_lib(plan, so, deps) == deps


def _cached_so(tmp_path: Path, kind: str, name: str) -> SourceObject:
    d = tmp_path / "deps"
    d.mkdir(exist_ok=True)
    return SourceObject(
        source=tmp_path / f"{name}.cpp",
        object_path=tmp_path / f"{name}.cpp.o",
        dep_path=d / f"{name}.cpp.o.d",
        kind=kind,
    )


def test_strip_removes_build_dir_paths_for_cached_core_and_variant(
    tmp_path: Path,
) -> None:
    sketch_build = tmp_path / "build"
    sketch_build.mkdir()
    opt = sketch_build / "build_opt.h"
    opt.write_bytes(b"")
    other = tmp_path / "sys" / "sdk.h"
    other.parent.mkdir()
    other.write_bytes(b"")

    fqbn = FQBN("espressif", "esp32", "devkit")
    plan = BuildPlan(
        fqbn=fqbn,
        platform_root=tmp_path / "p",
        sketch_dir=tmp_path / "sk",
        build_dir=sketch_build,
        expanded={},
        object_cache_dir=tmp_path / "cache",
    )
    deps = [str(opt.resolve()), str(other.resolve())]
    for kind, name in (("core", "wiring"), ("variant", "pins")):
        stripped = _strip_sketch_build_deps_for_shared_cache(
            plan, _cached_so(tmp_path, kind, name), deps
        )
        assert stripped == [str(other.resolve())], kind


def test_strip_noop_for_sketch_even_with_cache(tmp_path: Path) -> None:
    sketch_build = tmp_path / "build"
    sketch_build.mkdir()
    opt = sketch_build / "build_opt.h"
    opt.write_bytes(b"")

    fqbn = FQBN("espressif", "esp32", "devkit")
    plan = BuildPlan(
        fqbn=fqbn,
        platform_root=tmp_path / "p",
        sketch_dir=tmp_path / "sk",
        build_dir=sketch_build,
        expanded={},
        object_cache_dir=tmp_path / "cache",
    )
    so = SourceObject(
        source=tmp_path / "sketch.cpp",
        object_path=tmp_path / "sketch.cpp.o",
        dep_path=tmp_path / "sketch.cpp.o.d",
        kind="sketch",
    )
    deps = [str(opt.resolve())]
    assert _strip_sketch_build_deps_for_shared_cache(plan, so, deps) == deps
