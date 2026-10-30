"""Omit Ninja compile rules for shared-cache .o files that cache_compile would skip."""

from __future__ import annotations

import os
from pathlib import Path

from acmake.build import BuildPlan, SourceObject
from acmake.cmakegen import write_cmake
from acmake.fqbn import FQBN


def _so(
    kind: str, name: str, src_dir: Path, obj_dir: Path, *, ext: str = ".cpp"
) -> SourceObject:
    src = src_dir / f"{name}{ext}"
    obj = obj_dir / f"{name}{ext}.o"
    dep = obj_dir / f"{name}{ext}.o.d"
    return SourceObject(source=src, object_path=obj, dep_path=dep, kind=kind)


def _write_src(path: Path, body: str = "//\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _write_obj(path: Path, src: Path, *, newer: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"obj")
    t_old = 1_700_000_000
    t_new = t_old + 100
    if newer:
        os.utime(src, (t_old, t_old))
        os.utime(path, (t_new, t_new))
    else:
        os.utime(path, (t_old, t_old))
        os.utime(src, (t_new, t_new))


def _plan(
    tmp_path: Path,
    sources: list[SourceObject],
    *,
    object_cache: bool = True,
    archive: bool = True,
) -> BuildPlan:
    bd = tmp_path / "build"
    bd.mkdir(parents=True, exist_ok=True)
    elf = bd / "out.elf"
    archive_path = tmp_path / "cache" / "core" / "core.a" if archive else None
    return BuildPlan(
        fqbn=FQBN.parse("esp32:esp32:esp32"),
        platform_root=tmp_path / "p",
        sketch_dir=tmp_path / "sk",
        build_dir=bd,
        expanded={
            "runtime.os": "linux",
            "build.path": str(bd),
            "includes": "",
            "compiler.path": "",
            "compiler.ar.cmd": "ar",
            "recipe.cpp.o.pattern": 'g++ -c "{source_file}" -o "{object_file}"',
            "recipe.c.combine.pattern": 'g++ -o "{build.path}/out.elf" {object_files}',
        },
        sources=sources,
        archive_path=archive_path,
        elf_path=elf,
        object_cache_dir=(tmp_path / "cache") if object_cache else None,
    )


def _output_quoted(cmake: str, path: Path) -> bool:
    q = str(path.resolve() if path.exists() else path).replace("\\", "/")
    alt = str(path).replace("\\", "/")
    return f'OUTPUT "{q}"' in cmake or f'OUTPUT "{alt}"' in cmake


def test_omits_ninja_rule_for_fresh_core_but_keeps_archive_and_link_deps(
    tmp_path: Path,
) -> None:
    src_dir = tmp_path / "core"
    obj_dir = tmp_path / "cache" / "core" / "o"
    sketch_dir = tmp_path / "sk"
    core = _so("core", "Esp", src_dir, obj_dir)
    sketch = _so("sketch", "sketch", sketch_dir, tmp_path / "build")
    _write_src(core.source)
    _write_src(sketch.source)
    _write_obj(core.object_path, core.source, newer=True)

    cmake = write_cmake(_plan(tmp_path, [core, sketch])).read_text(encoding="utf-8")
    assert "objcache fresh; no Ninja rule" in cmake
    assert not _output_quoted(cmake, core.object_path)
    assert _output_quoted(cmake, sketch.object_path)
    core_o = str(core.object_path).replace("\\", "/")
    assert core_o in cmake
    assert "core archive" in cmake
    assert "out.elf" in cmake


def test_emits_ninja_rule_when_source_newer_or_object_missing(tmp_path: Path) -> None:
    src_dir = tmp_path / "core"
    obj_dir = tmp_path / "cache" / "core" / "o"
    stale = _so("core", "stale", src_dir, obj_dir)
    missing = _so("core", "missing", src_dir, obj_dir)
    _write_src(stale.source)
    _write_src(missing.source)
    _write_obj(stale.object_path, stale.source, newer=False)

    cmake = write_cmake(_plan(tmp_path, [stale, missing], archive=False)).read_text(
        encoding="utf-8"
    )
    assert _output_quoted(cmake, stale.object_path)
    assert _output_quoted(cmake, missing.object_path)
    assert "objcache fresh" not in cmake


def test_omits_fresh_variant_and_lib_keeps_sketch_rule(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    variant = _so("variant", "pins", tmp_path / "variant", cache / "variant" / "o")
    lib = _so("lib", "WiFi", tmp_path / "lib", cache / "lib" / "WiFi" / "o")
    sketch = _so("sketch", "sketch", tmp_path / "sk", tmp_path / "build")
    for so in (variant, lib, sketch):
        _write_src(so.source)
    _write_obj(variant.object_path, variant.source, newer=True)
    _write_obj(lib.object_path, lib.source, newer=True)

    cmake = write_cmake(
        _plan(tmp_path, [variant, lib, sketch], archive=False)
    ).read_text(encoding="utf-8")
    assert not _output_quoted(cmake, variant.object_path)
    assert not _output_quoted(cmake, lib.object_path)
    assert _output_quoted(cmake, sketch.object_path)
    assert str(variant.object_path).replace("\\", "/") in cmake
    assert str(lib.object_path).replace("\\", "/") in cmake


def test_no_object_cache_always_emits_compile_rule(tmp_path: Path) -> None:
    src_dir = tmp_path / "core"
    obj_dir = tmp_path / "o"
    core = _so("core", "Esp", src_dir, obj_dir)
    _write_src(core.source)
    _write_obj(core.object_path, core.source, newer=True)

    cmake = write_cmake(
        _plan(tmp_path, [core], object_cache=False, archive=False)
    ).read_text(encoding="utf-8")
    assert _output_quoted(cmake, core.object_path)
    assert "objcache fresh" not in cmake


def test_equal_mtimes_omits_rule(tmp_path: Path) -> None:
    core = _so("core", "Esp", tmp_path / "core", tmp_path / "cache" / "o")
    _write_src(core.source)
    core.object_path.parent.mkdir(parents=True, exist_ok=True)
    core.object_path.write_bytes(b"obj")
    t = 1_700_000_000
    os.utime(core.source, (t, t))
    os.utime(core.object_path, (t, t))

    cmake = write_cmake(_plan(tmp_path, [core], archive=False)).read_text(
        encoding="utf-8"
    )
    assert not _output_quoted(cmake, core.object_path)
    assert "objcache fresh" in cmake
