"""Build-stage hook placement: milestone targets + staged CLI orchestration."""

from __future__ import annotations

from pathlib import Path

import pytest

from acmake.build import BuildPlan, SourceObject
from acmake.cmakegen import write_cmake
from acmake.fqbn import FQBN


def _so(kind: str, name: str, root: Path) -> SourceObject:
    src = root / f"{name}.c"
    return SourceObject(
        source=src,
        object_path=root / "o" / f"{name}.o",
        dep_path=root / "o" / f"{name}.o.d",
        kind=kind,
    )


def _plan(tmp_path: Path) -> BuildPlan:
    bd = tmp_path / "build"
    bd.mkdir(parents=True, exist_ok=True)
    sources = [
        _so("sketch", "sk", bd),
        _so("lib", "libx", bd),
        _so("core", "corex", bd),
        _so("variant", "varx", bd),
    ]
    for so in sources:
        so.source.parent.mkdir(parents=True, exist_ok=True)
        so.source.write_text("//\n", encoding="utf-8")
    return BuildPlan(
        fqbn=FQBN.parse("vendor:arch:board"),
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
            "recipe.objcopy.hex.pattern": 'objcopy -O ihex "{build.path}/out.elf" "{build.path}/out.hex"',
        },
        sources=sources,
        archive_path=tmp_path / "cache" / "core.a",
        elf_path=bd / "out.elf",
        hex_path=bd / "out.hex",
    )


def test_cmake_declares_one_milestone_target_per_stage(tmp_path: Path) -> None:
    cmake = write_cmake(_plan(tmp_path)).read_text(encoding="utf-8")
    for target in (
        "acmake_sketch_objects",
        "acmake_libraries_objects",
        "acmake_core_objects",
        "acmake_link",
        "acmake_objcopy",
        "acmake_sketch",
    ):
        assert f"add_custom_target({target}" in cmake
    # Core milestone includes both core and variant objects plus core.a.
    core_line = next(
        ln
        for ln in cmake.splitlines()
        if ln.startswith("add_custom_target(acmake_core_objects")
    )
    assert "corex.o" in core_line
    assert "varx.o" in core_line
    sketch_line = next(
        ln
        for ln in cmake.splitlines()
        if ln.startswith("add_custom_target(acmake_sketch_objects")
    )
    assert "sk.o" in sketch_line
    assert "libx.o" not in sketch_line


def test_link_keeps_prelink_generated_response_file(tmp_path: Path) -> None:
    """ESP32 PR: ``link_libs.rsp`` is created by ``linking.prelink``; keep ``@path`` in the link argv."""
    plan = _plan(tmp_path)
    plan.expanded["compiler.c.elf.libs"] = '"@{build.path}/link_libs.rsp"'
    plan.expanded["recipe.c.combine.pattern"] = (
        'g++ -o "{build.path}/out.elf" {object_files} {compiler.c.elf.libs}'
    )
    cmake = write_cmake(plan).read_text(encoding="utf-8")
    rsp = str((plan.build_dir / "link_libs.rsp").resolve()).replace("\\", "/")
    assert f"@{rsp}" in cmake


def _stage_plan(tmp_path: Path, expanded: dict[str, str]) -> BuildPlan:
    plan = _plan(tmp_path)
    plan.expanded.update(expanded)
    return plan


def test_staged_build_runs_hooks_around_matching_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import acmake.cli as cli

    events: list[str] = []
    monkeypatch.setattr(
        cli, "_cmake_configure_if_needed", lambda *_a, **_k: events.append("configure")
    )

    def fake_build(_bd, _verbose, *, target=None):
        events.append(f"build:{target}")

    def fake_hooks(_expanded, _cwd, phases, **_kwargs):
        events.extend(f"hook:{p}" for p in phases)

    monkeypatch.setattr(cli, "_cmake_build", fake_build)
    monkeypatch.setattr(cli, "run_hook_phases", fake_hooks)

    plan = _stage_plan(
        tmp_path,
        {
            "recipe.hooks.sketch.prebuild.1.pattern": "true",
            "recipe.hooks.sketch.postbuild.1.pattern": "true",
            "recipe.hooks.libraries.prebuild.1.pattern": "true",
            "recipe.hooks.core.postbuild.1.pattern": "true",
            "recipe.hooks.linking.prelink.1.pattern": "true",
            "recipe.hooks.linking.postlink.1.pattern": "true",
        },
    )
    cli._run_build_with_hooks(plan, False)
    assert events == [
        "configure",
        "hook:sketch.prebuild",
        "build:acmake_sketch_objects",
        "hook:sketch.postbuild",
        "hook:libraries.prebuild",
        "build:acmake_libraries_objects",
        "hook:libraries.postbuild",
        "hook:core.prebuild",
        "build:acmake_core_objects",
        "hook:core.postbuild",
        "hook:linking.prelink",
        "build:acmake_link",
        "hook:linking.postlink",
        "build:acmake_objcopy",
    ]


def test_no_stage_hooks_keeps_single_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import acmake.cli as cli

    events: list[str] = []
    monkeypatch.setattr(
        cli, "_cmake_configure_if_needed", lambda *_a, **_k: events.append("configure")
    )
    monkeypatch.setattr(
        cli,
        "_cmake_build",
        lambda _bd, _v, *, target=None: events.append(f"build:{target}"),
    )
    monkeypatch.setattr(cli, "run_hook_phases", lambda *_a, **_k: events.append("hook"))
    plan = _stage_plan(tmp_path, {"recipe.hooks.postbuild.1.pattern": "true"})
    cli._run_build_with_hooks(plan, False)
    assert events == ["configure", "build:None"]


def test_export_binaries_copies_prefixed_artifacts_between_hooks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import acmake.cli as cli

    plan = _plan(tmp_path)
    plan.expanded["build.project_name"] = "Blink.ino"
    bd = plan.build_dir
    (bd / "Blink.ino.hex").write_text("hex", encoding="utf-8")
    (bd / "Blink.ino.elf").write_text("elf", encoding="utf-8")
    (bd / "unrelated.txt").write_text("x", encoding="utf-8")
    out = tmp_path / "export"

    events: list[str] = []

    def fake_hooks(_expanded, _cwd, phases, **_kwargs):
        events.extend(phases)
        if phases == ("savehex.presavehex",):
            (bd / "Blink.ino.lst").write_text("list", encoding="utf-8")

    monkeypatch.setattr(cli, "run_hook_phases", fake_hooks)
    cli.export_binaries(plan, out)

    assert events == ["savehex.presavehex", "savehex.postsavehex"]
    assert sorted(p.name for p in out.iterdir()) == [
        "Blink.ino.elf",
        "Blink.ino.hex",
        "Blink.ino.lst",
    ]
