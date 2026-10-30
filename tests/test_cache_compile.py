"""Shared-cache compile wrapper (source vs object mtime)."""

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from acmake.cache_compile import main as cache_compile_main
from acmake.cache_compile import should_skip_cached_compile


def test_skips_compile_when_object_newer_than_source(tmp_path: Path) -> None:
    src = tmp_path / "a.cpp"
    obj = tmp_path / "a.o"
    src.write_text("//\n", encoding="utf-8")
    obj.write_bytes(b"x")
    t_old = 1_700_000_000
    t_new = t_old + 100
    os.utime(src, (t_old, t_old))
    os.utime(obj, (t_new, t_new))
    argv = [
        str(obj),
        str(src),
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(42)",
    ]
    assert cache_compile_main(argv) == 0


def test_runs_compile_when_source_newer_than_object(tmp_path: Path) -> None:
    src = tmp_path / "a.cpp"
    obj = tmp_path / "a.o"
    obj.write_bytes(b"x")
    src.write_text("//\n", encoding="utf-8")
    t_old = 1_700_000_000
    t_new = t_old + 100
    os.utime(obj, (t_old, t_old))
    os.utime(src, (t_new, t_new))
    argv = [
        str(obj),
        str(src),
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(43)",
    ]
    assert cache_compile_main(argv) == 43


def test_should_skip_equal_mtimes_and_missing_files(tmp_path: Path) -> None:
    src = tmp_path / "a.cpp"
    obj = tmp_path / "a.o"
    src.write_text("//\n", encoding="utf-8")
    obj.write_bytes(b"x")
    t = 1_700_000_000
    os.utime(src, (t, t))
    os.utime(obj, (t, t))
    assert should_skip_cached_compile(obj, src) is True
    assert should_skip_cached_compile(tmp_path / "missing.o", src) is False
    assert should_skip_cached_compile(obj, tmp_path / "missing.cpp") is False


def test_runs_compile_when_object_missing(tmp_path: Path) -> None:
    src = tmp_path / "a.cpp"
    obj = tmp_path / "missing.o"
    src.write_text("//\n", encoding="utf-8")
    argv = [
        str(obj),
        str(src),
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(44)",
    ]
    assert cache_compile_main(argv) == 44


def test_strips_cmd_exe_wrapped_argv_before_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cmd.exe``-spawned Python can receive ``"@rsp"`` / ``"-I..."``; strip for GCC."""

    captured: list[list[str]] = []

    def fake_run(cargv: list[str], **_kw):
        captured.append(list(cargv))
        return mock.Mock(returncode=0)

    monkeypatch.setattr("acmake.cache_compile.subprocess.run", fake_run)
    src = tmp_path / "a.cpp"
    obj = tmp_path / "a.o"
    src.write_text("//\n", encoding="utf-8")
    obj.write_bytes(b"x")
    t_old = 1_700_000_000
    t_new = t_old + 100
    os.utime(obj, (t_old, t_old))
    os.utime(src, (t_new, t_new))
    argv = [
        str(obj),
        str(src),
        "--",
        "fake-compiler",
        '"@C:/flags/cpp_flags"',
        '"-IC:/include/here"',
        '"C:/src/a.cpp"',
    ]
    assert cache_compile_main(argv) == 0
    assert captured[0] == [
        "fake-compiler",
        "@C:/flags/cpp_flags",
        "-IC:/include/here",
        "C:/src/a.cpp",
    ]
