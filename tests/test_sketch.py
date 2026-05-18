"""Sketch → C++ concatenation and forward declarations."""

from pathlib import Path

import pytest

from acmake.sketch import (
    _line_directive_path,
    build_sketch_cpp_body,
    extract_sketch_function_forward_declarations,
    extract_sketch_forward_declaration_entries,
    list_sketch_inos,
    run_arduino_preprocessor,
    sketch_build_project_name,
)


def test_prototypes_before_first_function_after_typedefs(tmp_path: Path) -> None:
    """Prototypes must follow ``typedef``/globals when they precede the first function."""
    ino = tmp_path / "Z" / "Z.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text(
        '#include "Arduino.h"\n'
        "#define X 1\n"
        "typedef enum { A, B } MyEnum;\n"
        "typedef struct { int x; MyEnum e; } MyData;\n"
        "static void handle(MyData *p) {}\n"
        "void setup() { handle(nullptr); }\n"
        "void loop() {}\n",
        encoding="utf-8",
    )
    out = build_sketch_cpp_body([ino])
    proto = out.index("// acmake: forward declarations")
    handle_impl = out.index("static void handle(MyData")
    typedef_struct = out.index("typedef struct")
    assert typedef_struct < proto < handle_impl
    assert "static void handle(MyData *p);" in out


def test_forward_decl_strips_default_arguments(tmp_path: Path) -> None:
    """Prototypes must not repeat default args already on the .ino definition."""
    ino = tmp_path / "S" / "S.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text(
        "void ledcAnalogWrite(uint8_t pin, uint32_t value, uint32_t valueMax = 255) {}\n"
        "void setup() { ledcAnalogWrite(1, 2); }\n"
        "void loop() {}\n",
        encoding="utf-8",
    )
    decls = [d for _, _, d in extract_sketch_forward_declaration_entries([ino])]
    ledc = [d for d in decls if "ledcAnalogWrite" in d]
    assert len(ledc) == 1
    assert "= 255" not in ledc[0]
    assert "valueMax);" in ledc[0].replace(" ", "")


def test_forward_decl_skips_class_members_and_constructors(tmp_path: Path) -> None:
    """Do not emit global prototypes for class methods or ctor initializer lines."""
    ino = tmp_path / "S" / "S.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text(
        '#include "BLEDevice.h"\n'
        "class C : public BLEClientCallbacks {\n"
        "public:\n"
        "  C(int i) : x(i) {}\n"
        "  int x;\n"
        "  void onConnect(BLEClient *c) { (void)c; }\n"
        "};\n"
        "bool connectToServer(int i) { return true; }\n"
        "void setup() { connectToServer(1); }\n"
        "void loop() {}\n",
        encoding="utf-8",
    )
    decls = [d for _, _, d in extract_sketch_forward_declaration_entries([ino])]
    assert any("connectToServer" in d for d in decls)
    assert not any("onConnect" in d for d in decls)
    assert not any("C(int" in d for d in decls)


def test_forward_decl_preserves_extern_c_linkage(tmp_path: Path) -> None:
    """``extern "C"`` on a sketch function must appear on the generated prototype."""
    ino = tmp_path / "S" / "S.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text(
        'extern "C" void c_api(int x) { (void)x; }\n'
        "void setup() { c_api(1); }\n"
        "void loop() {}\n",
        encoding="utf-8",
    )
    decls = [d for _, _, d in extract_sketch_forward_declaration_entries([ino])]
    assert any(d.strip() == 'extern "C" void c_api(int x);' for d in decls)
    out = build_sketch_cpp_body([ino])
    assert 'extern "C" void c_api(int x);' in out


def test_forward_decl_preserves_static_specifier(tmp_path: Path) -> None:
    """``static`` (and similar leading specifiers) must appear on the prototype line."""
    ino = tmp_path / "S" / "S.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text(
        "static void measureAndSleep() { }\n"
        "void setup() { measureAndSleep(); }\n"
        "void loop() {}\n",
        encoding="utf-8",
    )
    entries = extract_sketch_forward_declaration_entries([ino])
    decls = [d for _, _, d in entries]
    assert any(d.strip() == "static void measureAndSleep();" for d in decls)
    out = build_sketch_cpp_body([ino])
    assert "static void measureAndSleep();" in out


def test_forward_declarations_skip_setup_and_loop():
    body = """
void setup() {
  helper(1);
}
void loop() {}
int helper(int x) { return x; }
"""
    decls = extract_sketch_function_forward_declarations(body)
    assert "helper" in "".join(decls)
    assert "setup" not in "".join(decls)
    assert "loop" not in "".join(decls)
    assert any("int helper(int x);" in d for d in decls)


def test_forward_decls_after_all_includes_when_code_precedes_include(tmp_path: Path) -> None:
    """Includes are never reordered; an ``#include`` after sketch code keeps protos after that include."""
    ino = tmp_path / "S" / "S.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text(
        "#ifndef CFG\n#define CFG 1\n#endif\n"
        "void early() {}\n"
        "#include <SPI.h>\n"
        "void setup() { early(); }\n"
        "void loop() {}\n",
        encoding="utf-8",
    )
    out = build_sketch_cpp_body([ino])
    spi = out.index("#include <SPI.h>")
    proto = out.index("// acmake: forward declarations")
    assert spi < proto
    assert "void early();" in out[spi:]


def test_forward_decls_after_last_include_before_define(tmp_path: Path) -> None:
    """Protos go after the final ``#include`` and before ``#define`` (Zigbee-style); ``#line`` matches that row."""
    ino = tmp_path / "S" / "S.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text(
        '#include "Zigbee.h"\n'
        "#define ANALOG 1\n"
        "void setup() {}\n"
        "void loop() {}\n"
        "void myAnalog() {}\n",
        encoding="utf-8",
    )
    out = build_sketch_cpp_body([ino])
    assert out.index('#include "Zigbee.h"') < out.index("void myAnalog();")
    assert out.index("void myAnalog();") < out.index("#define ANALOG")
    disp = _line_directive_path(ino)
    assert f'#line 2 "{disp}"' in out.split("#define ANALOG", maxsplit=1)[0]


def test_hoist_does_not_pull_include_past_ifndef(tmp_path: Path) -> None:
    """Includes after ``#if``/``#ifndef`` must stay below the guard (Zigbee-style sketches)."""
    ino = tmp_path / "S" / "S.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text(
        '#include <A.h>\n'
        "#ifndef X\n#error fail\n#endif\n"
        '#include <B.h>\n'
        "void setup() {}\nvoid loop() {}\n",
        encoding="utf-8",
    )
    out = build_sketch_cpp_body([ino])
    assert out.index("#include <B.h>") > out.index("#endif")
    assert out.index("#include <A.h>") < out.index("#ifndef")


def test_forward_decl_inserted_after_all_includes(tmp_path: Path):
    ino = tmp_path / "S" / "S.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text(
        '#include <SPI.h>\n'
        "void setup() { tick(); }\n"
        "void loop() {}\n"
        "void tick() {}\n",
        encoding="utf-8",
    )
    out = build_sketch_cpp_body([ino])
    assert out.lstrip().startswith('#include "Arduino.h"')
    ah = out.index('#include "Arduino.h"')
    spi = out.index("#include <SPI.h>")
    tickp = out.index("void tick();")
    setup = out.index("void setup()")
    assert ah < spi < tickp < setup


def test_arduino_h_not_duplicated_when_present_in_sketch(tmp_path: Path):
    """Arduino-CLI leaves an existing ``Arduino.h`` include in place (no strip + prepend)."""
    ino = tmp_path / "S" / "S.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text(
        '#include <SPI.h>\n#include "Arduino.h"\nvoid setup(){}void loop(){}\n',
        encoding="utf-8",
    )
    out = build_sketch_cpp_body([ino])
    lines = [ln for ln in out.splitlines() if ln.strip().startswith("#include")]
    assert lines[0].strip() == "#include <SPI.h>"
    assert any(ln.strip() == '#include "Arduino.h"' for ln in lines)
    assert sum(1 for ln in lines if "Arduino.h" in ln) == 1


def test_split_brace_line_merged_for_proto():
    body = "int foo(int x)\n{\n  return x;\n}\n"
    decls = extract_sketch_function_forward_declarations(body)
    assert any("int foo(int x);" in d for d in decls)


def test_if_while_not_taken_as_functions():
    body = """
void setup() {
  if (true) { }
  while (0) { }
}
void loop() {}
"""
    decls = extract_sketch_function_forward_declarations(body)
    assert not any("if(" in d or "while(" in d for d in decls)


def test_line_reset_after_forward_declarations(tmp_path: Path) -> None:
    """After prototypes, ``#line`` uses the first post-include source line (not always 1)."""
    ino = tmp_path / "S" / "S.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text(
        "#include <SPI.h>\n"
        "void setup() { h(); }\n"
        "void loop() {}\n"
        "void h() {}\n",
        encoding="utf-8",
    )
    out = build_sketch_cpp_body([ino])
    assert "void h();" in out
    proto = out.index("// acmake: forward declarations")
    disp = _line_directive_path(ino)
    reset = f'#line 2 "{disp}"'
    r1 = out.index(reset, proto)
    r2 = out.index("void setup()", r1)
    assert r1 < r2


def test_forward_decl_has_line_matching_implementation(tmp_path: Path) -> None:
    ino = tmp_path / "S" / "S.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text(
        "#include <SPI.h>\n"
        "\n"
        "void setup() { tick(); }\n"
        "void loop() {}\n"
        "void tick() {}\n",
        encoding="utf-8",
    )
    out = build_sketch_cpp_body([ino])
    disp = _line_directive_path(ino)
    assert f'#line 5 "{disp}"\nvoid tick();' in out


def test_user_include_order_never_rearranged(tmp_path: Path) -> None:
    """Only ``Arduino.h`` may be prepended; other includes keep sketch order."""
    p = tmp_path / "Sketch"
    p.mkdir()
    (p / "Sketch.ino").write_text(
        '#include <WiFi.h>\n#include <HTTPClient.h>\nvoid setup() {}\nvoid loop() {}\n',
        encoding="utf-8",
    )
    out = build_sketch_cpp_body(list_sketch_inos(p))
    ah = out.index('#include "Arduino.h"')
    w = out.index("#include <WiFi.h>")
    h = out.index("#include <HTTPClient.h>")
    assert ah < w < h


def test_forward_decls_after_includes_from_secondary_ino(tmp_path: Path):
    """Primary .ino may omit includes; a second .ino often ``#include``\\ s after ``loop``."""
    p = tmp_path / "P"
    p.mkdir()
    (p / "P.ino").write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
    (p / "B.ino").write_text(
        '#include <SPI.h>\nvoid tick() {}\n', encoding="utf-8"
    )
    out = build_sketch_cpp_body(list_sketch_inos(p))
    assert out.index("#include <SPI.h>") < out.index("void tick();")
    assert out.index("void setup()") < out.index("void tick();")
    assert out.index("void tick();") < out.index("void tick() {")
    bino = p / "B.ino"
    disp_b = _line_directive_path(bino)
    assert f'#line 2 "{disp_b}"' in out.split("void tick();", maxsplit=1)[0]


def test_forward_decls_after_includes_when_no_arduino_yet(tmp_path: Path):
    ino = tmp_path / "X" / "X.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text("void f() {}\nvoid setup() {}\nvoid loop() {}\n", encoding="utf-8")
    out = build_sketch_cpp_body([ino])
    assert out.startswith('#include "Arduino.h"')
    assert out.index("void f();") < out.index("void f() {")


def test_line_directive_uses_absolute_path(tmp_path: Path) -> None:
    ino = tmp_path / "S" / "S.ino"
    ino.parent.mkdir(parents=True)
    ino.write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
    out = build_sketch_cpp_body([ino])
    assert f'#line 1 "{_line_directive_path(ino)}"' in out


def test_line_directive_before_secondary_ino_include(tmp_path: Path) -> None:
    """Concat order is preserved: secondary tab ``#line`` then its ``#include`` (no hoisting)."""
    p = tmp_path / "P"
    p.mkdir()
    (p / "P.ino").write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
    bino = p / "B.ino"
    bino.write_text('#include <SPI.h>\nvoid tick() {}\n', encoding="utf-8")
    out = build_sketch_cpp_body(list_sketch_inos(p))
    spi = out.index("#include <SPI.h>")
    line_b = out.index(f'#line 1 "{_line_directive_path(bino)}"')
    assert line_b < spi


def test_sketch_build_project_name_matches_primary_ino(tmp_path: Path) -> None:
    """ESP32 / IDE use ``WiFiClient.ino`` as ``build.project_name``, not ``WiFiClient``."""
    d = tmp_path / "WiFiClient"
    d.mkdir()
    (d / "WiFiClient.ino").write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
    assert sketch_build_project_name(d) == "WiFiClient.ino"


def test_preprocessor_copy_skips_write_when_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged ``sketch.cpp`` must keep its mtime so Ninja does not rebuild the sketch TU."""
    monkeypatch.setattr("acmake.sketch.find_arduino_preprocessor", lambda: None)
    cpp = tmp_path / "raw.cpp"
    cpp.write_text("void x() {}\n", encoding="utf-8")
    out = tmp_path / "sketch.cpp"
    run_arduino_preprocessor(cpp, out)
    t1 = out.stat().st_mtime_ns
    run_arduino_preprocessor(cpp, out)
    assert out.stat().st_mtime_ns == t1
