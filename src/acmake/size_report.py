"""Arduino ``recipe.size`` / ``recipe.advanced_size`` memory report after build."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess

from acmake.build import BuildPlan
from acmake.command import split_recipe
from acmake.properties import expand_template

logger = logging.getLogger(__name__)


def _first_nonempty_expanded(expanded: dict[str, str], *keys: str) -> str:
    for k in keys:
        raw = expanded.get(k, "")
        if not str(raw).strip():
            continue
        return expand_template(str(raw).strip(), expanded).strip()
    return ""


def resolve_size_command(expanded: dict[str, str]) -> tuple[str, str]:
    """Return ``(expanded_command, kind)`` where *kind* is ``"advanced"`` or ``"basic"``."""
    ros = (expanded.get("runtime.os") or "").strip().lower()
    adv = _first_nonempty_expanded(
        expanded,
        f"recipe.advanced_size.pattern.{ros}",
        "recipe.advanced_size.pattern",
    )
    if adv:
        return adv, "advanced"
    basic = _first_nonempty_expanded(
        expanded,
        f"recipe.size.pattern.{ros}",
        "recipe.size.pattern",
    )
    if not basic:
        return "", "none"
    return basic, "basic"


def _try_print_advanced_json(stdout: str) -> tuple[bool, bool]:
    """Return ``(handled, fits)``; *fits* is False when the recipe reports ``"severity": "error"``."""
    s = stdout.strip()
    if not s.startswith("{"):
        return False, True
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return False, True
    out = obj.get("output")
    if isinstance(out, str) and out.strip():
        print(out.rstrip())
        return True, str(obj.get("severity", "")).lower() != "error"
    return False, True


def _sum_regex_matches(pattern: str, text: str) -> int | None:
    if not pattern.strip():
        return None
    try:
        rx = re.compile(pattern)
    except re.error:
        logger.warning("invalid recipe.size.regex: %r", pattern)
        return None
    total = 0
    found = False
    for line in text.splitlines():
        m = rx.search(line)
        if m:
            found = True
            try:
                total += int(m.group(1))
            except (IndexError, ValueError):
                continue
    return total if found else None


def _basic_summary_lines(expanded: dict[str, str], stdout: str) -> list[str]:
    """Arduino-IDE-style one-liners from ``recipe.size.regex*`` over captured tool stdout (no raw dump)."""
    rx_p = (expanded.get("recipe.size.regex") or "").strip()
    rx_d = (expanded.get("recipe.size.regex.data") or "").strip()
    rx_e = (expanded.get("recipe.size.regex.eeprom") or "").strip()
    prog = _sum_regex_matches(rx_p, stdout) if rx_p else None
    data = _sum_regex_matches(rx_d, stdout) if rx_d else None
    eep = _sum_regex_matches(rx_e, stdout) if rx_e else None
    max_p = (expanded.get("upload.maximum_size") or "").strip()
    max_d = (expanded.get("upload.maximum_data_size") or "").strip()
    max_e = (expanded.get("upload.maximum_eeprom_size") or "").strip()

    def pct(used: int, mx: str) -> str:
        try:
            mxi = int(mx)
            if mxi <= 0:
                return ""
            return f" ({int(round(100 * used / mxi))}%)"
        except ValueError:
            return ""

    lines: list[str] = []
    if prog is not None and max_p:
        lines.append(
            f"Sketch uses {prog} bytes{pct(prog, max_p)} of program storage space. Maximum is {max_p} bytes."
        )
    elif prog is not None:
        lines.append(f"Program storage: {prog} bytes (from recipe.size.regex).")
    if data is not None and max_d:
        try:
            rem = max(0, int(max_d) - data)
        except ValueError:
            rem = "?"
        lines.append(
            f"Global variables use {data} bytes{pct(data, max_d)} of dynamic memory, leaving {rem} bytes for local variables. Maximum is {max_d} bytes."
        )
    elif data is not None:
        lines.append(f"Dynamic memory: {data} bytes (from recipe.size.regex.data).")
    if eep is not None and max_e:
        lines.append(
            f"EEPROM: {eep} bytes{pct(eep, max_e)}. Maximum is {max_e} bytes."
        )
    return lines


def _int_or_none(s: str) -> int | None:
    try:
        v = int(s.strip())
    except ValueError:
        return None
    return v if v > 0 else None


def _basic_overflow_errors(expanded: dict[str, str], stdout: str) -> list[str]:
    """Same limits as arduino-cli: program over ``upload.maximum_size`` or data over ``upload.maximum_data_size``."""
    errors: list[str] = []
    rx_p = (expanded.get("recipe.size.regex") or "").strip()
    rx_d = (expanded.get("recipe.size.regex.data") or "").strip()
    prog = _sum_regex_matches(rx_p, stdout) if rx_p else None
    data = _sum_regex_matches(rx_d, stdout) if rx_d else None
    max_p = _int_or_none(expanded.get("upload.maximum_size") or "")
    max_d = _int_or_none(expanded.get("upload.maximum_data_size") or "")
    if prog is not None and max_p is not None and prog > max_p:
        errors.append(f"Sketch too big: {prog} bytes exceeds the maximum of {max_p} bytes.")
    if data is not None and max_d is not None and data > max_d:
        errors.append(f"Not enough memory: {data} bytes of dynamic memory exceeds the maximum of {max_d} bytes.")
    return errors


def print_size_report(plan: BuildPlan) -> bool:
    """Run platform size recipe and print Arduino-style output (advanced JSON or basic regex summary only).

    Returns False when the sketch does not fit the board limits.
    """
    elf = plan.elf_path
    if not elf or not elf.is_file():
        print("Memory usage: (ELF not found; size report skipped)")
        return True

    print("--- Memory usage ---")
    cmd, kind = resolve_size_command(plan.expanded)
    if not cmd:
        print("Memory usage: (no recipe.size.pattern / recipe.advanced_size.pattern for this platform)")
        return True

    argv = split_recipe(cmd)
    if not argv:
        print("Memory usage: (size recipe expanded to empty command)")
        return True

    bd = plan.build_dir.resolve()
    try:
        if os.name == "nt":
            p = subprocess.run(
                ["cmd", "/c", cmd],
                cwd=str(bd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        else:
            p = subprocess.run(
                ["/bin/sh", "-c", cmd],
                cwd=str(bd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
    except OSError as e:
        logger.warning("size recipe failed to run: %s", e)
        print(f"Memory usage: (could not run size recipe: {e})")
        return True

    out = (p.stdout or "").rstrip()
    err = (p.stderr or "").rstrip()
    if kind == "advanced":
        handled, fits = _try_print_advanced_json(p.stdout or "")
        if handled:
            if err:
                print(err)
            if p.returncode != 0:
                logger.warning("advanced size recipe exited with code %s", p.returncode)
            return fits

    if kind == "basic":
        lines = _basic_summary_lines(plan.expanded, p.stdout or "")
        if lines:
            print()
            for ln in lines:
                print(ln)
        else:
            print(
                "Memory usage: no summary lines (platform needs recipe.size.regex / "
                "recipe.size.regex.data matching the size tool output, and upload.maximum_* for percentages)."
            )
        if err and p.returncode != 0:
            print(err)
        if p.returncode != 0:
            logger.warning("size recipe exited with code %s", p.returncode)
        errors = _basic_overflow_errors(plan.expanded, p.stdout or "")
        for e in errors:
            print(e)
        return not errors

    if out:
        print(out)
    if err:
        print(err)
    if p.returncode != 0:
        logger.warning("size recipe exited with code %s", p.returncode)
    return True


def print_library_report(plan: BuildPlan) -> None:
    """Print resolved libraries and ``version=`` from each ``library.properties``."""
    libs = list(plan.libraries)
    print()
    if not libs:
        print("Used libraries: (none)")
        return
    print("Used libraries:")
    for lib in sorted(libs, key=lambda L: (L.name.lower(), str(L.root))):
        ver = f" @ {lib.version}" if lib.version else ""
        print(f"  - {lib.name}{ver}")
        print(f"      {lib.root}")
