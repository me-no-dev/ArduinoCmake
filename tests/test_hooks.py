"""Tests for Arduino ``recipe.hooks.*.pattern`` execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from acmake.hooks import (
    BUILD_STAGE_HOOKS,
    POST_BUILD_HOOK_PHASES,
    SAVEHEX_HOOK_PHASES,
    collect_hook_commands,
    collect_ordered_hook_shell_commands,
    has_build_stage_hooks,
    hook_prefix_for_phase,
    objcopy_post_recipe_hook_phases,
    objcopy_pre_recipe_hook_phases,
    ordered_objcopy_hook_phases,
    run_hook_phases,
)


def test_collect_hook_commands_numeric_order() -> None:
    expanded = {
        "recipe.hooks.prebuild.11.pattern": "third",
        "recipe.hooks.prebuild.1.pattern": "first",
        "recipe.hooks.prebuild.3.pattern": "second",
    }
    cmds = collect_hook_commands(expanded, "prebuild")
    assert [c[1] for c in cmds] == ["first", "second", "third"]


def test_objcopy_pre_post_split_for_ninja_embedding() -> None:
    expanded = {
        "recipe.hooks.objcopy.preobjcopy.1.pattern": "echo pre",
        "recipe.hooks.objcopy.postobjcopy.1.pattern": "echo post",
    }
    assert objcopy_pre_recipe_hook_phases(expanded) == ("objcopy.preobjcopy",)
    assert objcopy_post_recipe_hook_phases(expanded) == ("objcopy.postobjcopy",)
    cmds = collect_ordered_hook_shell_commands(
        expanded, objcopy_pre_recipe_hook_phases(expanded)
    )
    assert cmds == ["echo pre"]


def test_postobjcopy_runs_after_other_objcopy_hooks() -> None:
    expanded = {
        "recipe.hooks.objcopy.postobjcopy.1.pattern": "echo post",
        "recipe.hooks.objcopy.preobjcopy.1.pattern": "echo pre",
        "recipe.hooks.objcopy.custom.1.pattern": "echo mid",
    }
    assert ordered_objcopy_hook_phases(expanded) == (
        "objcopy.preobjcopy",
        "objcopy.custom",
        "objcopy.postobjcopy",
    )


def test_collect_hook_commands_dynamic_objcopy_subphase() -> None:
    expanded = {
        "recipe.hooks.objcopy.custom.2.pattern": "second",
        "recipe.hooks.objcopy.custom.1.pattern": "first",
    }
    cmds = collect_hook_commands(expanded, "objcopy.custom")
    assert [c[1] for c in cmds] == ["first", "second"]


ALL_SPEC_HOOK_PHASES = (
    "prebuild",
    "sketch.prebuild",
    "sketch.postbuild",
    "libraries.prebuild",
    "libraries.postbuild",
    "core.prebuild",
    "core.postbuild",
    "linking.prelink",
    "linking.postlink",
    "objcopy.preobjcopy",
    "objcopy.postobjcopy",
    "savehex.presavehex",
    "savehex.postsavehex",
)


def test_every_platform_spec_hook_phase_is_recognized() -> None:
    for phase in ALL_SPEC_HOOK_PHASES:
        prefix = hook_prefix_for_phase(phase)
        assert prefix is not None, phase
        assert prefix == f"recipe.hooks.{phase}.", phase
        key = f"{prefix}1.pattern"
        assert [c[1] for c in collect_hook_commands({key: "true"}, phase)] == ["true"]


def test_has_build_stage_hooks_detects_each_stage() -> None:
    assert not has_build_stage_hooks({})
    assert not has_build_stage_hooks({"recipe.hooks.prebuild.1.pattern": "true"})
    for pre, _target, post in BUILD_STAGE_HOOKS:
        assert has_build_stage_hooks({f"recipe.hooks.{pre}.1.pattern": "true"})
        assert has_build_stage_hooks({f"recipe.hooks.{post}.1.pattern": "true"})


def test_build_stage_order_matches_arduino() -> None:
    assert BUILD_STAGE_HOOKS == (
        ("sketch.prebuild", "acmake_sketch_objects", "sketch.postbuild"),
        ("libraries.prebuild", "acmake_libraries_objects", "libraries.postbuild"),
        ("core.prebuild", "acmake_core_objects", "core.postbuild"),
        ("linking.prelink", "acmake_link", "linking.postlink"),
    )


def test_post_and_savehex_phases_separate() -> None:
    assert POST_BUILD_HOOK_PHASES == ("postbuild",)
    assert SAVEHEX_HOOK_PHASES == ("savehex.presavehex", "savehex.postsavehex")


def test_run_hook_phases_prebuild(tmp_path: Path) -> None:
    marker = tmp_path / "hook_ran"
    expanded = {"recipe.hooks.prebuild.1.pattern": f"touch {marker}"}
    run_hook_phases(expanded, tmp_path, ("prebuild",))
    assert marker.is_file()


def test_hook_shell_argv_uses_bash_on_windows_when_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from subprocess import CompletedProcess

    import acmake.hooks as hooks

    recorded: list[tuple[tuple, dict]] = []

    def fake_run(*args, **kwargs):
        recorded.append((args, kwargs))
        return CompletedProcess(args, 0)

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    monkeypatch.setattr(hooks.shutil, "which", lambda name: "/fake/bash" if name == "bash" else None)
    monkeypatch.setattr(hooks.os, "name", "nt")
    run_hook_phases(
        {
            "recipe.hooks.prebuild.1.pattern": (
                '/usr/bin/env bash -c "[ ! -f \\"/p/a\\" ] || echo ok"'
            )
        },
        tmp_path,
        ("prebuild",),
    )
    assert len(recorded) == 1
    argv0 = recorded[0][0][0]
    assert isinstance(argv0, list)
    assert argv0[:2] == ["/fake/bash", "-c"]


def test_cmd_hook_runs_payload_with_shell_true_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``cmd /c`` payload must use ``shell=True`` so ``list2cmdline`` does not corrupt quotes."""
    from subprocess import CompletedProcess

    import acmake.hooks as hooks

    recorded: list[tuple[tuple, dict]] = []

    def fake_run(*args, **kwargs):
        recorded.append((args, kwargs))
        return CompletedProcess(args, 0)

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    monkeypatch.setattr(hooks.os, "name", "nt")
    monkeypatch.setattr(hooks.shutil, "which", lambda _n: "/bin/bash")
    run_hook_phases(
        {"recipe.hooks.prebuild.1.pattern": 'cmd /c if exist "C:\\a\\b" echo ok'},
        tmp_path,
        ("prebuild",),
    )
    assert len(recorded) == 1
    assert recorded[0][1].get("shell") is True
    assert recorded[0][0][0].startswith("if exist")


def test_hook_shell_argv_falls_back_to_cmd_on_windows_without_bash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from subprocess import CompletedProcess

    import acmake.hooks as hooks

    recorded: list[tuple[tuple, dict]] = []

    def fake_run(*args, **kwargs):
        recorded.append((args, kwargs))
        return CompletedProcess(args, 0)

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    monkeypatch.setattr(hooks.shutil, "which", lambda _name: None)
    monkeypatch.setattr(hooks.os, "name", "nt")
    run_hook_phases({"recipe.hooks.prebuild.1.pattern": "echo x"}, tmp_path, ("prebuild",))
    assert recorded[0][1].get("shell") is True
    assert recorded[0][0] == ("echo x",)
