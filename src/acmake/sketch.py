"""Sketch preprocessing: .ino ordering, concatenation, optional arduino-preprocessor."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from acmake.discovery import find_arduino_preprocessor

_SKIP_PROTO_NAMES = frozenset({"setup", "loop"})
# Names that must never get a forward declaration from this pass.
_REJECT_FN_NAMES = _SKIP_PROTO_NAMES | frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "else",
        "do",
        "return",
        "try",
        "case",
        "new",
        "delete",
    }
)

# Global-ish function definition on one physical line: … name ( … ) [const] { …
# Body may continue on the same line (``{ return x; }``). Greedy ``(.+)`` so
# ``unsigned long name`` yields function name ``name``, not ``long``.
# Leading ``static`` / ``inline`` / … are captured so forward decls keep linkage.
_SKETCH_ONE_LINE_FN = re.compile(
    r"^\s*((?:static\s+|inline\s+|constexpr\s+|virtual\s+)*)"
    r"(.+)\s+(\w+)\s*\(([^)]*)\)\s*(const\s*)?"
    r"\{.*$"
)


def _strip_c_comments_and_strings_for_scan(src: str) -> str:
    """Replace comments and string/char literals with whitespace (keep newlines)."""
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            out.append("\n")
            if j < 0:
                break
            i = j + 1
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                out.append(" " * (n - i))
                break
            out.append("\n" * src.count("\n", i, j + 2))
            i = j + 2
            continue
        ch = src[i]
        if ch in "\"'":
            quote = ch
            if quote == '"' and re.search(r"\bextern\s*$", src[:i]):
                j = i + 1
                escaped = False
                content_chars: list[str] = []
                while j < n:
                    if escaped:
                        escaped = False
                        content_chars.append(src[j])
                    elif src[j] == "\\":
                        escaped = True
                    elif src[j] == quote:
                        content = "".join(content_chars)
                        if content in ("C", "C++"):
                            out.append(src[i : j + 1])
                        else:
                            out.append(" " * (j - i + 1))
                        i = j + 1
                        break
                    else:
                        content_chars.append(src[j])
                    j += 1
                else:
                    out.append(" " * (n - i))
                    break
                continue
            i += 1
            escaped = False
            while i < n:
                if escaped:
                    escaped = False
                elif src[i] == "\\":
                    escaped = True
                elif src[i] == quote:
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _merge_split_brace_lines(lines: list[str]) -> list[str]:
    """Join ``)\\n {`` into a single logical line for scanning."""
    merged: list[str] = []
    i = 0
    while i < len(lines):
        s = lines[i].rstrip()
        if ")" in s and "{" not in s and i + 1 < len(lines):
            nxt = lines[i + 1].lstrip()
            if nxt.startswith("{"):
                merged.append(s + " " + nxt)
                i += 2
                continue
        merged.append(lines[i])
        i += 1
    return merged


_ARDUINO_H_INCLUDE_RE = re.compile(
    r'^\s*#\s*include\s*[<"]Arduino\.h[>"]\s*$',
    flags=re.MULTILINE | re.IGNORECASE,
)

# ``#if __has_include("h")`` must not be treated as ``#include``.
_PREPROCESSOR_INCLUDE_RE = re.compile(
    r"^\s*#\s*(?:include|import)\b",
    flags=re.IGNORECASE,
)


def _split_param_list_top_level_commas(params: str) -> list[str]:
    """Split a parameter list on commas not nested inside ``()`` ``[]`` ``{}``."""
    params = params.strip()
    if not params:
        return []
    parts: list[str] = []
    start = 0
    depth = 0
    for i, c in enumerate(params):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth > 0:
                depth -= 1
        elif c == "," and depth == 0:
            parts.append(params[start:i].strip())
            start = i + 1
    parts.append(params[start:].strip())
    return [p for p in parts if p]


def _strip_trailing_default_from_one_param(param: str) -> str:
    """Remove a single top-level ``= default`` from one parameter (forward decl only)."""
    s = param.strip()
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth > 0:
                depth -= 1
        elif c == "=" and depth == 0:
            if i + 1 < n and s[i + 1] == "=":
                i += 2
                continue
            if i > 0 and s[i - 1] in "+-*/%&|^~":
                i += 1
                continue
            if i > 0 and s[i - 1] in "<>!":
                i += 1
                continue
            return s[:i].rstrip()
        i += 1
    return s


def _strip_default_arguments_from_param_list(params: str) -> str:
    """Drop default arguments so prototypes do not duplicate the definition's defaults."""
    parts = _split_param_list_top_level_commas(params)
    if not parts:
        return params.strip()
    return ", ".join(_strip_trailing_default_from_one_param(p) for p in parts)


def _parse_sketch_fn_forward_decl(line: str) -> tuple[str, str] | None:
    """If *line* is a one-line sketch function definition, return ``(name, decl);`` else ``None``."""
    raw = line.rstrip()
    # C++ ctor / dtor initializer lists: ``Type(...) : member(...) {`` — not a sketch free function.
    if re.search(r"\)\s*:", raw):
        return None
    m = _SKETCH_ONE_LINE_FN.match(raw)
    if not m:
        return None
    spec = m.group(1).strip()
    retish, name = m.group(2).strip(), m.group(3)
    if name in _REJECT_FN_NAMES:
        return None
    if not retish:
        return None
    if not re.match(r"^[A-Za-z_]\w*$", name):
        return None
    lead = retish.split()
    if lead and lead[0] in (
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "else",
        "do",
        "return",
    ):
        return None
    plist = _strip_default_arguments_from_param_list(m.group(4))
    core = f"{retish} {name}({plist})"
    if m.group(5) and m.group(5).strip():
        core += " const"
    sig = f"{spec} {core}".strip() if spec else core
    return name, f"{sig};"


def _global_brace_depth_before_each_line(lines: list[str]) -> list[int]:
    """Brace nesting depth **before** each line (0 = global / sketch file scope).

    Comments and string literals are ignored so braces inside them do not count.
    """
    depths: list[int] = []
    depth = 0
    for line in lines:
        depths.append(depth)
        sl = _strip_c_comments_and_strings_for_scan(line)
        depth += sl.count("{") - sl.count("}")
        if depth < 0:
            depth = 0
    return depths


def extract_sketch_function_forward_declarations(body: str) -> list[str]:
    """Return ``void name(...);`` lines for sketch functions except ``setup`` / ``loop``."""
    stripped_lines = _strip_c_comments_and_strings_for_scan(body).splitlines()
    depths = _global_brace_depth_before_each_line(stripped_lines)
    seen: set[str] = set()
    decls: list[str] = []
    for phys_ln, chunk in _merge_split_brace_lines_indexed(stripped_lines):
        if depths[phys_ln - 1] != 0:
            continue
        got = _parse_sketch_fn_forward_decl(chunk)
        if not got:
            continue
        name, decl = got
        if name in seen:
            continue
        seen.add(name)
        decls.append(decl)
    return decls


def _merge_split_brace_lines_indexed(lines: list[str]) -> list[tuple[int, str]]:
    """Like :func:`_merge_split_brace_lines` but each entry is ``(1-based start line, text)``."""
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        s = lines[i].rstrip()
        if ")" in s and "{" not in s and i + 1 < len(lines):
            nxt = lines[i + 1].lstrip()
            if nxt.startswith("{"):
                out.append((i + 1, s + " " + nxt))
                i += 2
                continue
        out.append((i + 1, lines[i]))
        i += 1
    return out


def extract_sketch_forward_declaration_entries(
    inos: list[Path],
) -> list[tuple[str, int, str]]:
    """``(abs_path_for_line, impl_line_1based, decl)`` for prototypes (Arduino-CLI ``--preprocess`` style)."""
    out: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    for p in inos:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        stripped_for_depth = [
            _strip_c_comments_and_strings_for_scan(ln) for ln in lines
        ]
        depths = _global_brace_depth_before_each_line(stripped_for_depth)
        disp = _line_directive_path(p)
        for phys_ln, chunk in _merge_split_brace_lines_indexed(lines):
            if depths[phys_ln - 1] != 0:
                continue
            sl = _strip_c_comments_and_strings_for_scan(chunk)
            got = _parse_sketch_fn_forward_decl(sl)
            if not got:
                continue
            name, decl = got
            if name in seen:
                continue
            seen.add(name)
            out.append((disp, phys_ln, decl))
    return out


def _ensure_arduino_h_first(body: str) -> str:
    """Prepend ``#include \"Arduino.h\"`` only if the sketch does not already include it.

    Matches Arduino-CLI: do not add a duplicate when ``#include <Arduino.h>`` / ``\"Arduino.h\"``
    is already present.
    """
    if _ARDUINO_H_INCLUDE_RE.search(body):
        return body
    return '#include "Arduino.h"\n' + body.lstrip("\n")


def _line_is_line_directive(stripped: str) -> bool:
    """True for ``#line`` / ``# line`` (maps diagnostics to a source file)."""
    return bool(re.match(r"^\s*#\s*line\b", stripped, flags=re.IGNORECASE))


def _peel_first_line_directive_from_rest(rest_lines: list[str]) -> list[str]:
    """Drop the first ``#line`` in *rest_lines* (replaced by a computed reset after protos)."""
    for idx, line in enumerate(rest_lines):
        if _line_is_line_directive(line.strip()):
            return rest_lines[:idx] + rest_lines[idx + 1 :]
    return rest_lines


def _line_directive_path(path: Path) -> str:
    """Absolute path for ``#line`` ``\"...\"`` (backslashes and quotes escaped)."""
    return path.resolve().as_posix().replace("\\", "\\\\").replace('"', '\\"')


def _line_reset_for_rest_peeled(
    rest_peeled: list[str], inos: list[Path]
) -> tuple[str, int]:
    """``#line`` after protos: file + 1-based line of the first ``rest_peeled`` row (matches .ino text)."""
    if not inos:
        return ("", 1)
    idx = 0
    while idx < len(rest_peeled):
        s = rest_peeled[idx].strip()
        if s == "":
            idx += 1
            continue
        if _line_is_line_directive(s):
            idx += 1
            continue
        break
    if idx >= len(rest_peeled):
        return (_line_directive_path(inos[0]), 1)
    first = rest_peeled[idx].rstrip("\r\n")
    first_st = first.strip()
    for p in inos:
        raw_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for ln, raw in enumerate(raw_lines, start=1):
            if raw.rstrip("\r\n") == first:
                return (_line_directive_path(p), ln)
        for ln, raw in enumerate(raw_lines, start=1):
            if raw.strip() == first_st:
                return (_line_directive_path(p), ln)
    return (_line_directive_path(inos[0]), 1)


def concatenate_inos(inos: list[Path]) -> str:
    if not inos:
        return ""
    parts: list[str] = []
    for p in inos:
        q = _line_directive_path(p)
        parts.append(f'#line 1 "{q}"\n')
        parts.append(p.read_text(encoding="utf-8", errors="replace"))
        if parts[-1] and not parts[-1].endswith("\n"):
            parts.append("\n")
    return "".join(parts)


def _forward_decl_split_index(lines: list[str]) -> int:
    """Index after the last ``#include`` / ``#import`` in source order (includes are never reordered)."""
    last_after_include = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s == "" or s.startswith("//"):
            continue
        if _line_is_line_directive(s):
            continue
        if _PREPROCESSOR_INCLUDE_RE.match(s):
            last_after_include = i + 1
    return last_after_include


def _has_non_preprocessor_global_text_between(
    lines: list[str], start: int, end: int
) -> bool:
    """True if ``[start, end)`` has any depth-0 line that is not only a ``#`` directive / comment / blank.

    When only ``#define`` / ``#if`` / etc. appear before the first function, prototypes stay
    right after the last ``#include`` (Zigbee-style: before ``#define`` that follows an include).
    """
    stripped_for_depth = [
        _strip_c_comments_and_strings_for_scan(ln) for ln in lines
    ]
    depths = _global_brace_depth_before_each_line(stripped_for_depth)
    for i in range(max(0, start), min(end, len(lines))):
        if depths[i] != 0:
            continue
        s = lines[i].strip()
        if not s or s.startswith("//"):
            continue
        if _line_is_line_directive(s):
            continue
        if s.startswith("#"):
            continue
        return True
    return False


def _first_global_function_definition_start_line_index(lines: list[str]) -> int | None:
    """0-based line index of the first global (``depth==0``) function definition.

    Used to insert sketch prototypes *after* ``#include``\\ s and ``#define`` / ``typedef``
    blocks that precede the first function (e.g. Zigbee sketches with ``typedef enum``).
    ``setup`` / ``loop`` count as functions here so prototypes stay before them when
    they are the first definition after includes.
    """
    stripped_for_depth = [
        _strip_c_comments_and_strings_for_scan(ln) for ln in lines
    ]
    depths = _global_brace_depth_before_each_line(stripped_for_depth)
    for phys_ln, chunk in _merge_split_brace_lines_indexed(lines):
        if depths[phys_ln - 1] != 0:
            continue
        raw = chunk.rstrip()
        if re.search(r"\)\s*:", raw):
            continue
        sl = _strip_c_comments_and_strings_for_scan(raw)
        if not _SKETCH_ONE_LINE_FN.match(sl):
            continue
        return phys_ln - 1
    return None


def insert_sketch_forward_declarations_after_includes(
    body: str,
    inos: list[Path],
) -> str:
    """Insert forward declarations after includes (and after preceding globals / typedefs).

    Prototypes are placed immediately **before** the first global function definition,
    but never before the last ``#include`` / ``#import`` — so ``#define`` / ``typedef`` /
    global variables that appear before any function remain visible to the prototypes.
    """
    entries = extract_sketch_forward_declaration_entries(inos)
    if not entries:
        return body
    proto_lines: list[str] = []
    for disp, impl_ln, decl in entries:
        proto_lines.append(f'#line {impl_ln} "{disp}"\n{decl}\n')
    proto_block = (
        "// acmake: forward declarations for sketch functions\n"
        + "".join(proto_lines)
        + "\n"
    )
    lines = body.splitlines(keepends=True)
    base = _forward_decl_split_index(lines)
    fn0 = _first_global_function_definition_start_line_index(lines)
    if (
        fn0 is not None
        and fn0 > base
        and _has_non_preprocessor_global_text_between(lines, base, fn0)
    ):
        i = fn0
    else:
        i = base
    rest_lines = lines[i:]
    rest_peeled = _peel_first_line_directive_from_rest(rest_lines)
    reset_disp, reset_ln = _line_reset_for_rest_peeled(rest_peeled, inos)
    line_reset = f'#line {reset_ln} "{reset_disp}"\n'
    block = proto_block + line_reset
    return "".join(lines[:i]) + block + "".join(rest_peeled)


def list_sketch_inos(sketch_dir: Path) -> list[Path]:
    """Primary .ino = folder name, then other .ino files alphabetically."""
    name = sketch_dir.name
    primary = sketch_dir / f"{name}.ino"
    all_inos = sorted(sketch_dir.glob("*.ino"))
    if primary in all_inos:
        rest = [p for p in all_inos if p != primary]
        return [primary] + rest
    return all_inos


def sketch_build_project_name(sketch_dir: Path) -> str:
    """Value for ``build.project_name`` (primary ``.ino`` basename, e.g. ``WiFiClient.ino``).

    Matches Arduino IDE / ``arduino-cli`` for library examples: artifacts are
    ``{build.project_name}.bin`` (``WiFiClient.ino.bin``), not the bare folder name.
    """
    inos = list_sketch_inos(sketch_dir)
    if inos:
        return inos[0].name
    return sketch_dir.name


def build_sketch_cpp_body(inos: list[Path]) -> str:
    body = concatenate_inos(inos)
    body = _ensure_arduino_h_first(body)
    body = insert_sketch_forward_declarations_after_includes(body, inos)
    return body


def _write_text_if_bytes_differ(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write *text* only when missing or different (preserve mtime for unchanged sketches)."""
    data = text.encode(encoding)
    if path.is_file() and path.read_bytes() == data:
        return
    path.write_bytes(data)


def _replace_with_bytes_if_differ(path: Path, new_data: bytes) -> None:
    """Set *path* to *new_data* only when missing or different."""
    if path.is_file() and path.read_bytes() == new_data:
        return
    path.write_bytes(new_data)


def run_arduino_preprocessor(
    cpp_path: Path,
    output_path: Path,
    *,
    verbose: bool = False,
) -> None:
    """Run arduino-preprocessor if available; else copy input to output."""
    exe = find_arduino_preprocessor()
    if exe is None:
        _write_text_if_bytes_differ(
            output_path, cpp_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        return
    tmp = output_path.with_name(output_path.name + ".acmake_preproc_tmp")
    tmp.unlink(missing_ok=True)
    attempts: list[list[str]] = [
        [str(exe), str(cpp_path), str(tmp)],
        [str(exe), str(cpp_path), "-o", str(tmp)],
        [str(exe), str(cpp_path), str(tmp), "--"],
    ]
    last_err: Exception | None = None
    for cmd in attempts:
        if verbose:
            print(" ".join(cmd))
        tmp.unlink(missing_ok=True)
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            last_err = e
            continue
        if not tmp.is_file():
            continue
        new_data = tmp.read_bytes()
        _replace_with_bytes_if_differ(output_path, new_data)
        tmp.unlink(missing_ok=True)
        return
    tmp.unlink(missing_ok=True)
    if last_err:
        _write_text_if_bytes_differ(
            output_path, cpp_path.read_text(encoding="utf-8"), encoding="utf-8"
        )


def preprocess_sketch(
    sketch_dir: Path,
    build_dir: Path,
    *,
    verbose: bool = False,
) -> Path:
    """Write sketch.cpp into build_dir (with prototypes if preprocessor present)."""
    build_dir.mkdir(parents=True, exist_ok=True)
    inos = list_sketch_inos(sketch_dir)
    if not inos:
        raise FileNotFoundError(f"no .ino files in {sketch_dir}")
    raw_cpp = build_sketch_cpp_body(inos)
    raw_path = build_dir / "_sketch_raw.cpp"
    if (
        not raw_path.is_file()
        or raw_path.read_text(encoding="utf-8") != raw_cpp
    ):
        raw_path.write_text(raw_cpp, encoding="utf-8")
    out = build_dir / "sketch.cpp"
    run_arduino_preprocessor(raw_path, out, verbose=verbose)
    return out
