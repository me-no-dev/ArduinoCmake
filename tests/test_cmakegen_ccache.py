"""ccache is prefixed onto compile argv, not via CMAKE_C_COMPILER_LAUNCHER."""

from acmake.cmakegen import _prefix_ccache


def test_prefix_when_ccache_on_path(monkeypatch) -> None:
    monkeypatch.setenv("ACMAKE_CCACHE", "1")
    monkeypatch.setattr(
        "acmake.cmakegen.shutil.which", lambda name: "/opt/homebrew/bin/ccache"
    )
    assert _prefix_ccache(["g++", "-c", "a.cpp"]) == [
        "/opt/homebrew/bin/ccache",
        "g++",
        "-c",
        "a.cpp",
    ]


def test_no_prefix_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ACMAKE_CCACHE", "0")
    monkeypatch.setattr(
        "acmake.cmakegen.shutil.which", lambda name: "/usr/bin/ccache"
    )
    argv = ["g++", "-c", "a.cpp"]
    assert _prefix_ccache(argv) == argv


def test_no_prefix_when_ccache_missing(monkeypatch) -> None:
    monkeypatch.setenv("ACMAKE_CCACHE", "1")
    monkeypatch.setattr("acmake.cmakegen.shutil.which", lambda name: None)
    argv = ["g++", "-c", "a.cpp"]
    assert _prefix_ccache(argv) == argv


def test_does_not_double_prefix(monkeypatch) -> None:
    monkeypatch.setenv("ACMAKE_CCACHE", "1")
    monkeypatch.setattr(
        "acmake.cmakegen.shutil.which", lambda name: "/usr/bin/ccache"
    )
    argv = ["/usr/bin/ccache", "g++", "-c", "a.cpp"]
    assert _prefix_ccache(argv) == argv


def test_empty_argv(monkeypatch) -> None:
    monkeypatch.setenv("ACMAKE_CCACHE", "1")
    monkeypatch.setattr(
        "acmake.cmakegen.shutil.which", lambda name: "/usr/bin/ccache"
    )
    assert _prefix_ccache([]) == []
