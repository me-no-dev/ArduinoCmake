"""Arduino platform ``recipe.hooks.*.pattern`` shell hooks (platform specification)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from acmake.properties import expand_template

logger = logging.getLogger(__name__)

# ``recipe.hooks.<phase>.<N>.pattern`` — prefix must match exactly (see Arduino platform spec).
# Run after ``prepare_build`` path expansion, before sketch preprocessing / library discovery.
PRE_SKETCH_HOOK_PHASES: tuple[str, ...] = ("prebuild",)

# Build-stage hooks, in Arduino build order. Each row is
# ``(pre-build phase, CMake target for the stage, post-build phase)``.
#
# :func:`acmake.cmakegen.write_cmake` emits one milestone target per build stage
# (``acmake_sketch_objects``, ``acmake_libraries_objects``, ``acmake_core_objects``,
# ``acmake_link``), and :func:`acmake.cli.cmd_compile` runs the per-stage hooks around
# the matching ``cmake --build --target`` call. This keeps every hook at its exact
# boundary (sketch → libraries → core → link) instead of batching all "pre" hooks
# before the whole build and all "post" hooks after it.
BUILD_STAGE_HOOKS: tuple[tuple[str, str, str], ...] = (
    ("sketch.prebuild", "acmake_sketch_objects", "sketch.postbuild"),
    ("libraries.prebuild", "acmake_libraries_objects", "libraries.postbuild"),
    ("core.prebuild", "acmake_core_objects", "core.postbuild"),
    ("linking.prelink", "acmake_link", "linking.postlink"),
)

# ``recipe.hooks.objcopy.*`` are **not** listed here: they are emitted into CMake/Ninja by
# :func:`acmake.cmakegen.write_cmake` so ``preobjcopy`` runs after link (and after
# ``recipe.hooks.linking.postlink``) and before ``recipe.objcopy.*``, and ``postobjcopy``
# runs after all ``recipe.objcopy.*`` outputs.
#
# ``recipe.hooks.savehex.*`` bracket the export of built artifacts to the sketch
# directory and are run by :func:`acmake.cli.export_binaries`.
POST_BUILD_HOOK_PHASES: tuple[str, ...] = ("postbuild",)
SAVEHEX_HOOK_PHASES: tuple[str, ...] = (
    "savehex.presavehex",
    "savehex.postsavehex",
)

HOOK_PHASE_PREFIXES: dict[str, str] = {
    "prebuild": "recipe.hooks.prebuild.",
    "postbuild": "recipe.hooks.postbuild.",
    "sketch.prebuild": "recipe.hooks.sketch.prebuild.",
    "sketch.postbuild": "recipe.hooks.sketch.postbuild.",
    "libraries.prebuild": "recipe.hooks.libraries.prebuild.",
    "libraries.postbuild": "recipe.hooks.libraries.postbuild.",
    "core.prebuild": "recipe.hooks.core.prebuild.",
    "core.postbuild": "recipe.hooks.core.postbuild.",
    "linking.prelink": "recipe.hooks.linking.prelink.",
    "linking.postlink": "recipe.hooks.linking.postlink.",
    "objcopy.preobjcopy": "recipe.hooks.objcopy.preobjcopy.",
    "objcopy.postobjcopy": "recipe.hooks.objcopy.postobjcopy.",
    # Any other ``recipe.hooks.objcopy.<subphase>.*`` is resolved dynamically in
    # :func:`hook_prefix_for_phase`.
    "savehex.presavehex": "recipe.hooks.savehex.presavehex.",
    "savehex.postsavehex": "recipe.hooks.savehex.postsavehex.",
}


def hook_prefix_for_phase(phase: str) -> str | None:
    """Property key prefix for *phase*, or ``None`` if unknown."""
    p = HOOK_PHASE_PREFIXES.get(phase)
    if p is not None:
        return p
    if phase.startswith("objcopy.") and len(phase) > len("objcopy."):
        sub = phase[len("objcopy.") :]
        return f"recipe.hooks.objcopy.{sub}."
    return None


def ordered_objcopy_hook_phases(expanded: dict[str, str]) -> tuple[str, ...]:
    """Every ``recipe.hooks.objcopy.<subphase>.*`` that has patterns, with ``postobjcopy`` last."""
    base = "recipe.hooks.objcopy."
    bl = base.lower()
    subs: set[str] = set()
    for k, v in expanded.items():
        if not str(v).strip():
            continue
        kl = k.lower()
        if not kl.startswith(bl) or not kl.endswith(".pattern"):
            continue
        rel = k[len(base) :]
        parts = rel.split(".")
        if len(parts) < 3 or parts[-1].lower() != "pattern":
            continue
        if not parts[1].isdigit():
            continue
        subs.add(parts[0].lower())
    if not subs:
        return ()

    def _non_post_key(s: str) -> tuple[int, str]:
        pri = {"preobjcopy": 0}
        return (pri.get(s, 50), s)

    non_post = sorted((s for s in subs if s != "postobjcopy"), key=_non_post_key)
    out = [f"objcopy.{s}" for s in non_post]
    if "postobjcopy" in subs:
        out.append("objcopy.postobjcopy")
    return tuple(out)


def has_build_stage_hooks(expanded: dict[str, str]) -> bool:
    """True when any sketch/libraries/core/linking hook needs a staged build.

    Without such hooks the CLI keeps the fast single-invocation ``cmake --build``
    (all phases in parallel); with them it builds one milestone target per phase so
    the hooks land at the exact Arduino boundaries.
    """
    for pre, _target, post in BUILD_STAGE_HOOKS:
        if collect_hook_commands(expanded, pre) or collect_hook_commands(expanded, post):
            return True
    return False


def objcopy_pre_recipe_hook_phases(expanded: dict[str, str]) -> tuple[str, ...]:
    """``recipe.hooks.objcopy`` subphases to run **before** ``recipe.objcopy.*`` (``postobjcopy`` excluded)."""
    return tuple(p for p in ordered_objcopy_hook_phases(expanded) if p != "objcopy.postobjcopy")


def objcopy_post_recipe_hook_phases(expanded: dict[str, str]) -> tuple[str, ...]:
    """``postobjcopy`` hooks only — run **after** all ``recipe.objcopy.*`` steps."""
    if "objcopy.postobjcopy" in ordered_objcopy_hook_phases(expanded):
        return ("objcopy.postobjcopy",)
    return tuple()


def collect_ordered_hook_shell_commands(
    expanded: dict[str, str], phases: tuple[str, ...]
) -> list[str]:
    """Flatten ``collect_hook_commands`` for *phases* in order."""
    out: list[str] = []
    for phase in phases:
        out.extend(cmd for _k, cmd in collect_hook_commands(expanded, phase))
    return out


_HOOK_INDEX = re.compile(r"\.(\d+)\.pattern\s*$", re.IGNORECASE)


def _hook_sort_key(full_key: str) -> tuple[int, str]:
    m = _HOOK_INDEX.search(full_key)
    n = int(m.group(1)) if m else 0
    return (n, full_key.lower())


def collect_hook_commands(expanded: dict[str, str], phase: str) -> list[tuple[str, str]]:
    """Return ``(property_key, expanded_shell_command)`` for *phase*, in Arduino order."""
    prefix = hook_prefix_for_phase(phase)
    if not prefix:
        return []
    pl = prefix.lower()
    hits: list[tuple[str, str]] = []
    for k, v in expanded.items():
        if not v or not str(v).strip():
            continue
        kl = k.lower()
        if not kl.startswith(pl) or not kl.endswith(".pattern"):
            continue
        cmd = expand_template(str(v).strip(), expanded).strip()
        if not cmd:
            continue
        hits.append((k, cmd))
    hits.sort(key=lambda t: _hook_sort_key(t[0]))
    return hits


def _run_hook_command(cmd: str, cwd: Path) -> None:
    """Run one expanded hook line (POSIX ``sh`` / ``bash``, or Windows ``cmd`` / ``powershell``).

    Windows ``cmd /c``: do **not** pass ``["cmd", "/c", long_line]`` — ``subprocess`` uses
    ``list2cmdline``, which escapes embedded ``"`` and breaks ESP32 ``COPY "src" "dst"``
    lines (symptoms: leading ``\\`` on paths, stray ``\\*``). Run the payload after ``/c``
    with ``shell=True`` so ``cmd`` parses the recipe exactly once, like the interactive shell.
    """
    if os.name != "nt":
        subprocess.run(["/bin/sh", "-c", cmd], cwd=cwd, check=True)
        return
    s = cmd.strip()
    parts = s.split(None, 2)
    if len(parts) >= 3:
        p0 = parts[0].lower()
        if p0 in ("cmd", "cmd.exe") and parts[1].lower() == "/c":
            subprocess.run(parts[2], shell=True, cwd=cwd, check=True)
            return
        p0n = p0.replace(".exe", "")
        if p0n == "powershell" and parts[1].lower() in ("-command", "-c"):
            subprocess.run([parts[0], parts[1], parts[2]], cwd=cwd, check=True)
            return
    bash = shutil.which("bash")
    if bash:
        subprocess.run([bash, "-c", cmd], cwd=cwd, check=True)
    else:
        subprocess.run(cmd, shell=True, cwd=cwd, check=True)


def run_hook_phases(
    expanded: dict[str, str],
    cwd: Path,
    phases: tuple[str, ...],
    *,
    verbose: bool = False,
) -> None:
    """Run each non-empty hook command for the given phases, in order."""
    cwd = cwd.resolve()
    for phase in phases:
        for key, cmd in collect_hook_commands(expanded, phase):
            if verbose:
                logger.info("hook %s: %s", key, cmd)
            try:
                _run_hook_command(cmd, cwd)
            except (OSError, subprocess.CalledProcessError) as e:
                raise RuntimeError(f"platform hook failed ({key}): {cmd!r}") from e
