from pathlib import Path

from acmake.response_files import (
    argv_text_for_depfile_detection,
    collect_at_file_dep_paths,
    ensure_build_response_stubs,
    expand_at_file_arguments,
    read_gcc_response_file_tokens,
    resolve_build_dir_at_file_argv,
)


def test_read_rsp_flags(tmp_path: Path) -> None:
    f = tmp_path / "f.rsp"
    f.write_text("-std=gnu++17\n-DFOO=1\n", encoding="utf-8")
    assert read_gcc_response_file_tokens(f) == ["-std=gnu++17", "-DFOO=1"]


def test_read_rsp_quoted_include_paths_use_posix_split(tmp_path: Path) -> None:
    """ESP32 ``@flags/includes`` lines use POSIX quotes; Windows ``shlex`` must not use ``posix=False``."""
    f = tmp_path / "includes.rsp"
    f.write_text(
        '-I"C:/hardware/esp32/variants/esp32" -I"C:/hardware/esp32/cores/esp32"\n',
        encoding="utf-8",
    )
    assert read_gcc_response_file_tokens(f) == [
        "-IC:/hardware/esp32/variants/esp32",
        "-IC:/hardware/esp32/cores/esp32",
    ]


def test_read_header_as_include(tmp_path: Path) -> None:
    f = tmp_path / "opt.h"
    f.write_text("#define X 1\n", encoding="utf-8")
    toks = read_gcc_response_file_tokens(f)
    assert toks[0] == "-include"
    assert Path(toks[1]).resolve() == f.resolve()


def test_resolve_build_dir_drops_empty_file_opts(tmp_path: Path) -> None:
    bd = tmp_path / "build"
    bd.mkdir()
    fo = bd / "file_opts"
    fo.write_bytes(b"")
    argv, deps = resolve_build_dir_at_file_argv(bd, ["g++", f"@{fo}", "-c", "a.cpp"])
    assert "@" not in "".join(argv)
    assert str(fo.resolve()) in deps


def test_resolve_build_dir_header_to_include_relpath(tmp_path: Path) -> None:
    bd = tmp_path / "build"
    bd.mkdir()
    h = bd / "build_opt.h"
    h.write_text("#pragma once\n", encoding="utf-8")
    argv, _ = resolve_build_dir_at_file_argv(bd, ["g++", f"@{h}", "-c", "a.cpp"])
    assert "-include" in argv
    assert "build_opt.h" in argv
    assert f"@{h}" not in argv


def test_collect_at_file_dep_paths_keeps_argv_shape(tmp_path: Path) -> None:
    rsp = tmp_path / "opts.rsp"
    rsp.write_text("-DZ=1\n", encoding="utf-8")
    argv = ["gcc", f"@{rsp}", "-c", "a.c"]
    deps = collect_at_file_dep_paths(argv)
    assert argv == ["gcc", f"@{rsp}", "-c", "a.c"]
    assert Path(deps[0]).resolve() == rsp.resolve()


def test_resolve_build_dir_keeps_missing_generated_rsp(tmp_path: Path) -> None:
    """A prelink-generated ``link_libs.rsp`` must not be dropped at CMake-generation time."""
    bd = tmp_path / "build"
    bd.mkdir()
    rsp = bd / "link_libs.rsp"
    argv, deps = resolve_build_dir_at_file_argv(bd, ["g++", f"@{rsp}", "-o", "a.elf"])
    assert argv == ["g++", f"@{rsp}", "-o", "a.elf"]
    assert deps == []


def test_resolve_build_dir_inline_false_keeps_existing_rsp(tmp_path: Path) -> None:
    """Link/objcopy must read build-dir response files at execution time (not stale copies)."""
    bd = tmp_path / "build"
    bd.mkdir()
    rsp = bd / "link_libs.rsp"
    rsp.write_text("-lbt.matter\n", encoding="utf-8")
    argv, deps = resolve_build_dir_at_file_argv(bd, ["g++", f"@{rsp}"], inline=False)
    assert argv == ["g++", f"@{rsp}"]
    assert str(rsp.resolve()) in deps


def test_argv_text_for_depfile_peeks_rsp(tmp_path: Path) -> None:
    dep = tmp_path / "out.d"
    rsp = tmp_path / "c.rsp"
    rsp.write_text(f"-MMD -MF {dep}\n", encoding="utf-8")
    j = argv_text_for_depfile_detection(["g++", f"@{rsp}", "-c", "a.cpp"])
    assert "-MMD" in j and str(dep) in j


def test_expand_at_inline(tmp_path: Path) -> None:
    rsp = tmp_path / "opts.rsp"
    rsp.write_text("-DZ=1\n", encoding="utf-8")
    argv, deps = expand_at_file_arguments(["gcc", f"@{rsp}", "-c", "a.c"])
    assert argv == ["gcc", "-DZ=1", "-c", "a.c"]
    assert deps and Path(deps[0]).resolve() == rsp.resolve()


def test_ensure_stubs(tmp_path: Path) -> None:
    b = tmp_path / "b"
    s = tmp_path / "sk"
    s.mkdir()
    ensure_build_response_stubs(b, s)
    assert (b / "build_opt.h").is_file()
    assert (b / "build_opt.h").read_bytes() == b""
    assert (b / "file_opts").is_file()


def test_ensure_stubs_sketch_build_opt_preserves_mtime_when_unchanged(
    tmp_path: Path,
) -> None:
    """Re-copying the same ``build_opt.h`` must not touch the dest (Ninja / object cache)."""
    b = tmp_path / "b"
    s = tmp_path / "sk"
    s.mkdir()
    sketch_opt = s / "build_opt.h"
    sketch_opt.write_text("#define X 1\n", encoding="utf-8")
    ensure_build_response_stubs(b, s)
    dst = b / "build_opt.h"
    t1 = dst.stat().st_mtime_ns
    ensure_build_response_stubs(b, s)
    t2 = dst.stat().st_mtime_ns
    assert t1 == t2
    sketch_opt.write_text("#define X 2\n", encoding="utf-8")
    ensure_build_response_stubs(b, s)
    assert dst.read_text(encoding="utf-8") == "#define X 2\n"


def test_ensure_stubs_preserves_existing_file_opts(tmp_path: Path) -> None:
    b = tmp_path / "b"
    b.mkdir()
    s = tmp_path / "sk"
    s.mkdir()
    fo = b / "file_opts"
    fo.write_bytes(b"-O2\n")
    ensure_build_response_stubs(b, s)
    assert fo.read_bytes() == b"-O2\n"


def test_ensure_stubs_clears_stale_build_opt_when_sketch_has_none(
    tmp_path: Path,
) -> None:
    """Shared ``build_dir``: switching from a sketch with ``build_opt.h`` to one without must clear it."""
    b = tmp_path / "shared_build"
    s_with = tmp_path / "SketchWithOpt"
    s_with.mkdir()
    (s_with / "build_opt.h").write_text("-DUPDATE_SIGN\n", encoding="utf-8")
    s_without = tmp_path / "SketchWithoutOpt"
    s_without.mkdir()

    ensure_build_response_stubs(b, s_with)
    opt = b / "build_opt.h"
    assert opt.read_bytes() == b"-DUPDATE_SIGN\n"

    ensure_build_response_stubs(b, s_without)
    assert opt.read_bytes() == b""

    # Subsequent sketch without build_opt.h must not touch mtime of already-empty build_opt.h.
    t1 = opt.stat().st_mtime_ns
    ensure_build_response_stubs(b, s_without)
    t2 = opt.stat().st_mtime_ns
    assert t1 == t2
