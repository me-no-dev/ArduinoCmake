"""Object cache invalidation fingerprints."""

import os
from pathlib import Path

from acmake.cache_invalidate import (
    _build_opt_fingerprint,
    _library_bundle_path,
    _platform_header_fingerprint,
    clear_entire_object_cache,
    maybe_refresh_object_cache,
    sdk_config_fingerprint,
)
from acmake.libraries import Library


def _make_sdk(root: Path, *, sdkconfig: bytes, defines: bytes) -> Path:
    """Minimal ESP-style SDK tree: root ``sdkconfig`` + ``flags/defines``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "sdkconfig").write_bytes(sdkconfig)
    flags = root / "flags"
    flags.mkdir(exist_ok=True)
    (flags / "defines").write_bytes(defines)
    return root


def test_sdk_config_fingerprint_empty_without_sdk_path() -> None:
    assert sdk_config_fingerprint({}) == ""
    assert sdk_config_fingerprint({"compiler.sdk.path": "   "}) == ""


def test_sdk_config_fingerprint_empty_when_tree_absent(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert sdk_config_fingerprint({"compiler.sdk.path": str(missing)}) == ""


def test_sdk_config_fingerprint_changes_with_sdkconfig(tmp_path: Path) -> None:
    sdk = _make_sdk(
        tmp_path / "sdk",
        sdkconfig=b"CONFIG_BT_AUDIO=n\n",
        defines=b"-DCONFIG_FOO=1\n",
    )
    before = sdk_config_fingerprint({"compiler.sdk.path": str(sdk)})
    (sdk / "sdkconfig").write_bytes(b"CONFIG_BT_AUDIO=y\n")
    after = sdk_config_fingerprint({"compiler.sdk.path": str(sdk)})
    assert before != after


def test_sdk_config_fingerprint_changes_with_flags_defines(tmp_path: Path) -> None:
    sdk = _make_sdk(
        tmp_path / "sdk",
        sdkconfig=b"CONFIG_BT_AUDIO=n\n",
        defines=b"-DCONFIG_BT_AUDIO=0\n",
    )
    before = sdk_config_fingerprint({"compiler.sdk.path": str(sdk)})
    (sdk / "flags" / "defines").write_bytes(b"-DCONFIG_BT_AUDIO=1\n")
    after = sdk_config_fingerprint({"compiler.sdk.path": str(sdk)})
    assert before != after


def test_sdk_config_fingerprint_stable_and_location_independent(tmp_path: Path) -> None:
    sdk_a = _make_sdk(
        tmp_path / "a" / "esp32s3",
        sdkconfig=b"CONFIG_BT_AUDIO=y\n",
        defines=b"-DCONFIG_BT_AUDIO=1\n",
    )
    sdk_b = _make_sdk(
        tmp_path / "b" / "esp32s3",
        sdkconfig=b"CONFIG_BT_AUDIO=y\n",
        defines=b"-DCONFIG_BT_AUDIO=1\n",
    )
    fp_a = sdk_config_fingerprint({"compiler.sdk.path": str(sdk_a)})
    assert fp_a
    assert fp_a == sdk_config_fingerprint({"compiler.sdk.path": str(sdk_a)})
    # Keyed by filename + content only, so an identical SDK at a different path matches.
    assert fp_a == sdk_config_fingerprint({"compiler.sdk.path": str(sdk_b)})


def test_sdk_config_fingerprint_ignores_lib_and_ld(tmp_path: Path) -> None:
    sdk = _make_sdk(
        tmp_path / "sdk",
        sdkconfig=b"CONFIG_BT_AUDIO=y\n",
        defines=b"-DCONFIG_BT_AUDIO=1\n",
    )
    before = sdk_config_fingerprint({"compiler.sdk.path": str(sdk)})
    lib = sdk / "lib"
    lib.mkdir()
    (lib / "libbt.a").write_bytes(b"\x00binary-archive-bytes\x00")
    ld = sdk / "ld"
    ld.mkdir()
    (ld / "memory.ld").write_bytes(b"MEMORY { }\n")
    after = sdk_config_fingerprint({"compiler.sdk.path": str(sdk)})
    assert before == after


def test_platform_header_fingerprint_ignores_build_opt(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.mkdir()
    var = tmp_path / "variant"
    var.mkdir()
    bd1 = tmp_path / "build1"
    bd1.mkdir()
    (bd1 / "build_opt.h").write_bytes(b"")
    bd2 = tmp_path / "build2"
    bd2.mkdir()
    (bd2 / "build_opt.h").write_bytes(b"-DFOO\n")
    assert _platform_header_fingerprint(core, var) == _platform_header_fingerprint(
        core, var
    )


def test_build_opt_fingerprint_changes_with_bytes(tmp_path: Path) -> None:
    bd = tmp_path / "build"
    bd.mkdir()
    (bd / "build_opt.h").write_bytes(b"")
    a = _build_opt_fingerprint(bd)
    (bd / "build_opt.h").write_bytes(b"-DFOO\n")
    b = _build_opt_fingerprint(bd)
    assert a != b


def test_build_opt_fingerprint_same_bytes_different_paths(tmp_path: Path) -> None:
    """Two sketch build dirs: identical ``build_opt.h`` bytes → same digest (path-independent)."""
    payload = b"-DCONFIG_CHIP=42\n"
    bd1 = tmp_path / "a" / "build"
    bd1.mkdir(parents=True)
    (bd1 / "build_opt.h").write_bytes(payload)
    bd2 = tmp_path / "b" / "build"
    bd2.mkdir(parents=True)
    (bd2 / "build_opt.h").write_bytes(payload)
    assert _build_opt_fingerprint(bd1) == _build_opt_fingerprint(bd2)


def test_maybe_refresh_build_opt_mtime_only_keeps_lib_cache(tmp_path: Path) -> None:
    """``build_opt.h`` invalidation is content-based: mtime-only change must not wipe lib .o."""
    cache = tmp_path / "acmake_objcache" / "k_bo_mtime"
    cache.mkdir(parents=True)
    stamps = cache / ".acmake_stamps"
    stamps.mkdir(parents=True)
    bd = tmp_path / "bd_bo_mt"
    bd.mkdir()
    (bd / "build_opt.h").write_bytes(b"-DSAME\n")
    c = tmp_path / "c_bo_mt"
    c.mkdir()
    v = tmp_path / "v_bo_mt"
    v.mkdir()
    (stamps / "platform_hdr_fp").write_text(
        _platform_header_fingerprint(c, v) + "\n", encoding="utf-8"
    )

    lib_root = tmp_path / "BoMtLib"
    lib_root.mkdir()
    (lib_root / "library.properties").write_text(
        "name=BoMtLib\narchitectures=*\n", encoding="utf-8"
    )
    src = lib_root / "src"
    src.mkdir()
    (src / "BoMtLib.h").write_text("//\n", encoding="utf-8")
    lib = Library(
        root=lib_root.resolve(),
        name="BoMtLib",
        architectures="*",
        depends=(),
        includes_override=None,
    )
    bundle = _library_bundle_path(cache, lib)
    bundle.mkdir(parents=True)
    odir = bundle / "o"
    odir.mkdir(parents=True)
    lo = odir / "BoMtLib.cpp.o"
    lo.write_bytes(b"o")

    maybe_refresh_object_cache(
        cache, build_dir=bd, core_path=c, variant_path=v, libraries=[lib]
    )
    assert lo.is_file()

    opt = bd / "build_opt.h"
    st = opt.stat()
    os.utime(opt, (st.st_atime + 800, st.st_mtime + 800))
    maybe_refresh_object_cache(
        cache, build_dir=bd, core_path=c, variant_path=v, libraries=[lib]
    )
    assert lo.is_file()


def test_maybe_refresh_wipes_core_on_header_change(tmp_path: Path) -> None:
    cache = tmp_path / "acmake_objcache" / "k"
    cache.mkdir(parents=True)
    stamps = cache / ".acmake_stamps"
    stamps.mkdir(parents=True)
    (stamps / "platform_hdr_fp").write_text("old\n", encoding="utf-8")

    bd = tmp_path / "build"
    bd.mkdir()
    (bd / "build_opt.h").write_bytes(b"")

    core = tmp_path / "core"
    core.mkdir()
    h = core / "X.h"
    h.write_text("//\n", encoding="utf-8")
    var = tmp_path / "variant"
    var.mkdir()

    odir = cache / "core" / "o"
    odir.mkdir(parents=True)
    stale = odir / "a.o"
    stale.write_bytes(b"x")

    maybe_refresh_object_cache(
        cache,
        build_dir=bd,
        core_path=core,
        variant_path=var,
        libraries=[],
    )
    assert not stale.is_file()


def test_maybe_refresh_lib_only_without_platform_change(tmp_path: Path) -> None:
    cache = tmp_path / "acmake_objcache" / "k2"
    cache.mkdir(parents=True)
    stamps = cache / ".acmake_stamps"
    stamps.mkdir(parents=True)
    bd = tmp_path / "bd"
    bd.mkdir()
    (bd / "build_opt.h").write_bytes(b"")
    c = tmp_path / "c"
    c.mkdir()
    v = tmp_path / "v"
    v.mkdir()
    hdr_fp = _platform_header_fingerprint(c, v)
    (stamps / "platform_hdr_fp").write_text(hdr_fp + "\n", encoding="utf-8")

    lib_root = tmp_path / "Wire"
    lib_root.mkdir()
    (lib_root / "library.properties").write_text(
        "name=Wire\narchitectures=*\n", encoding="utf-8"
    )
    src = lib_root / "src"
    src.mkdir()
    hdr = src / "Wire.h"
    hdr.write_text("//\n", encoding="utf-8")

    lib = Library(
        root=lib_root.resolve(),
        name="Wire",
        architectures="*",
        depends=(),
        includes_override=None,
    )

    bundle = _library_bundle_path(cache, lib)
    bundle.mkdir(parents=True)
    odir = bundle / "o"
    odir.mkdir(parents=True)
    lo = odir / "Wire.cpp.o"
    lo.write_bytes(b"o")

    maybe_refresh_object_cache(
        cache,
        build_dir=bd,
        core_path=c,
        variant_path=v,
        libraries=[lib],
    )
    assert lo.is_file()

    hdr.write_text("//changed\n", encoding="utf-8")
    maybe_refresh_object_cache(
        cache,
        build_dir=bd,
        core_path=c,
        variant_path=v,
        libraries=[lib],
    )
    assert not lo.is_file()


def test_maybe_refresh_lib_ignores_mtime_only_change(tmp_path: Path) -> None:
    """Library stamp uses file contents, not mtimes — touching headers must not wipe cache."""
    cache = tmp_path / "acmake_objcache" / "mtime"
    cache.mkdir(parents=True)
    stamps = cache / ".acmake_stamps"
    stamps.mkdir(parents=True)
    bd = tmp_path / "bd_mtime"
    bd.mkdir()
    (bd / "build_opt.h").write_bytes(b"")
    c = tmp_path / "c_mt"
    c.mkdir()
    v = tmp_path / "v_mt"
    v.mkdir()
    hdr_fp = _platform_header_fingerprint(c, v)
    (stamps / "platform_hdr_fp").write_text(hdr_fp + "\n", encoding="utf-8")

    lib_root = tmp_path / "ZLib"
    lib_root.mkdir()
    (lib_root / "library.properties").write_text(
        "name=ZLib\nversion=1.0.0\narchitectures=*\n", encoding="utf-8"
    )
    src = lib_root / "src"
    src.mkdir()
    (src / "ZLib.h").write_text("// stable\n", encoding="utf-8")
    (src / "ZLib.cpp").write_text("// cpp\n", encoding="utf-8")
    lib = Library(
        root=lib_root.resolve(),
        name="ZLib",
        architectures="*",
        depends=(),
        includes_override=None,
        version="1.0.0",
    )
    bundle = _library_bundle_path(cache, lib)
    bundle.mkdir(parents=True)
    odir = bundle / "o"
    odir.mkdir(parents=True)
    lo = odir / "src__ZLib.cpp.o"
    lo.write_bytes(b"o")

    maybe_refresh_object_cache(
        cache, build_dir=bd, core_path=c, variant_path=v, libraries=[lib]
    )
    assert lo.is_file()
    assert (bundle / ".acmake_hdr_fp").is_file()

    st = (src / "ZLib.h").stat()
    os.utime(src / "ZLib.h", (st.st_atime + 500, st.st_mtime + 500))
    maybe_refresh_object_cache(
        cache, build_dir=bd, core_path=c, variant_path=v, libraries=[lib]
    )
    assert lo.is_file()


def test_maybe_refresh_second_sketch_build_dir_keeps_lib_cache(tmp_path: Path) -> None:
    """Same FQBN cache + headers + build_opt: another example's build dir must not wipe libs."""
    cache = tmp_path / "acmake_objcache" / "k3"
    cache.mkdir(parents=True)
    stamps = cache / ".acmake_stamps"
    stamps.mkdir(parents=True)

    core = tmp_path / "c"
    core.mkdir()
    (core / "z.h").write_text("//\n", encoding="utf-8")
    var = tmp_path / "v"
    var.mkdir()
    hdr_fp = _platform_header_fingerprint(core, var)
    (stamps / "platform_hdr_fp").write_text(hdr_fp + "\n", encoding="utf-8")

    lib_root = tmp_path / "Matter"
    lib_root.mkdir()
    (lib_root / "library.properties").write_text(
        "name=Matter\narchitectures=*\n", encoding="utf-8"
    )
    src = lib_root / "src"
    src.mkdir()
    (src / "Matter.h").write_text("//\n", encoding="utf-8")
    lib = Library(
        root=lib_root.resolve(),
        name="Matter",
        architectures="*",
        depends=(),
        includes_override=None,
    )

    bundle = _library_bundle_path(cache, lib)
    bundle.mkdir(parents=True)
    odir = bundle / "o"
    odir.mkdir(parents=True)
    lo = odir / "Matter.cpp.o"
    lo.write_bytes(b"o")

    bd1 = tmp_path / "ExampleA" / "build"
    bd1.mkdir(parents=True)
    (bd1 / "build_opt.h").write_bytes(b"")

    maybe_refresh_object_cache(
        cache,
        build_dir=bd1,
        core_path=core,
        variant_path=var,
        libraries=[lib],
    )
    assert lo.is_file()

    bd2 = tmp_path / "ExampleB" / "build"
    bd2.mkdir(parents=True)
    (bd2 / "build_opt.h").write_bytes(b"")

    maybe_refresh_object_cache(
        cache,
        build_dir=bd2,
        core_path=core,
        variant_path=var,
        libraries=[lib],
    )
    assert lo.is_file()

    st2 = (bd2 / "build_opt.h").stat()
    os.utime(bd2 / "build_opt.h", (st2.st_atime + 700, st2.st_mtime + 700))
    maybe_refresh_object_cache(
        cache,
        build_dir=bd2,
        core_path=core,
        variant_path=var,
        libraries=[lib],
    )
    assert lo.is_file()


def test_maybe_refresh_lib_wiped_when_build_opt_differs_across_build_dirs(
    tmp_path: Path,
) -> None:
    """Shared lib bundle: sketch A empty build_opt then sketch B with flags must drop Lib .o."""
    cache = tmp_path / "acmake_objcache" / "k_bo_cross"
    cache.mkdir(parents=True)
    stamps = cache / ".acmake_stamps"
    stamps.mkdir(parents=True)

    core = tmp_path / "c_bo"
    core.mkdir()
    (core / "h.h").write_text("//\n", encoding="utf-8")
    var = tmp_path / "v_bo"
    var.mkdir()
    (stamps / "platform_hdr_fp").write_text(
        _platform_header_fingerprint(core, var) + "\n", encoding="utf-8"
    )

    lib_root = tmp_path / "SharedLib"
    lib_root.mkdir()
    (lib_root / "library.properties").write_text(
        "name=SharedLib\narchitectures=*\n", encoding="utf-8"
    )
    src = lib_root / "src"
    src.mkdir()
    (src / "SharedLib.h").write_text("//\n", encoding="utf-8")
    lib = Library(
        root=lib_root.resolve(),
        name="SharedLib",
        architectures="*",
        depends=(),
        includes_override=None,
    )

    bundle = _library_bundle_path(cache, lib)
    bundle.mkdir(parents=True)
    odir = bundle / "o"
    odir.mkdir(parents=True)
    lo = odir / "SharedLib.cpp.o"
    lo.write_bytes(b"o")

    bd_a = tmp_path / "SketchA" / "build"
    bd_a.mkdir(parents=True)
    (bd_a / "build_opt.h").write_bytes(b"")
    maybe_refresh_object_cache(
        cache,
        build_dir=bd_a,
        core_path=core,
        variant_path=var,
        libraries=[lib],
    )
    assert lo.is_file()

    bd_b = tmp_path / "SketchB" / "build"
    bd_b.mkdir(parents=True)
    (bd_b / "build_opt.h").write_bytes(b"-DUSE_LIB_FEATURE\n")
    maybe_refresh_object_cache(
        cache,
        build_dir=bd_b,
        core_path=core,
        variant_path=var,
        libraries=[lib],
    )
    assert not lo.is_file()


def test_maybe_refresh_library_properties_change_wipes_lib(tmp_path: Path) -> None:
    """Editing ``library.properties`` (e.g. version) changes the fingerprint and wipes that lib."""
    cache = tmp_path / "acmake_objcache" / "k_props"
    cache.mkdir(parents=True)
    stamps = cache / ".acmake_stamps"
    stamps.mkdir(parents=True)
    c = tmp_path / "c_pr"
    c.mkdir()
    v = tmp_path / "v_pr"
    v.mkdir()
    (stamps / "platform_hdr_fp").write_text(
        _platform_header_fingerprint(c, v) + "\n", encoding="utf-8"
    )
    bd = tmp_path / "bd_pr"
    bd.mkdir()
    (bd / "build_opt.h").write_bytes(b"")

    lib_root = tmp_path / "PropsLib"
    lib_root.mkdir()
    props = lib_root / "library.properties"
    props.write_text(
        "name=PropsLib\nversion=1\narchitectures=*\n", encoding="utf-8"
    )
    src = lib_root / "src"
    src.mkdir()
    (src / "PropsLib.h").write_text("//\n", encoding="utf-8")
    lib = Library(
        root=lib_root.resolve(),
        name="PropsLib",
        architectures="*",
        depends=(),
        includes_override=None,
        version="1",
    )
    bundle = _library_bundle_path(cache, lib)
    bundle.mkdir(parents=True)
    odir = bundle / "o"
    odir.mkdir(parents=True)
    lo = odir / "x.o"
    lo.write_bytes(b"o")

    maybe_refresh_object_cache(
        cache, build_dir=bd, core_path=c, variant_path=v, libraries=[lib]
    )
    assert lo.is_file()

    props.write_text(
        "name=PropsLib\nversion=2\narchitectures=*\n", encoding="utf-8"
    )
    lib2 = Library(
        root=lib_root.resolve(),
        name="PropsLib",
        architectures="*",
        depends=(),
        includes_override=None,
        version="2",
    )
    maybe_refresh_object_cache(
        cache, build_dir=bd, core_path=c, variant_path=v, libraries=[lib2]
    )
    assert not lo.is_file()


def test_maybe_refresh_build_opt_change_wipes_libs(tmp_path: Path) -> None:
    cache = tmp_path / "acmake_objcache" / "k4"
    cache.mkdir(parents=True)
    stamps = cache / ".acmake_stamps"
    stamps.mkdir(parents=True)
    c = tmp_path / "c2"
    c.mkdir()
    v = tmp_path / "v2"
    v.mkdir()
    hdr_fp = _platform_header_fingerprint(c, v)
    (stamps / "platform_hdr_fp").write_text(hdr_fp + "\n", encoding="utf-8")

    bd = tmp_path / "one_build"
    bd.mkdir()
    (bd / "build_opt.h").write_bytes(b"")

    lib_root = tmp_path / "WiFi"
    lib_root.mkdir()
    (lib_root / "library.properties").write_text(
        "name=WiFi\narchitectures=*\n", encoding="utf-8"
    )
    src = lib_root / "src"
    src.mkdir()
    (src / "WiFi.h").write_text("//\n", encoding="utf-8")
    lib = Library(
        root=lib_root.resolve(),
        name="WiFi",
        architectures="*",
        depends=(),
        includes_override=None,
    )
    bundle = _library_bundle_path(cache, lib)
    bundle.mkdir(parents=True)
    odir = bundle / "o"
    odir.mkdir(parents=True)
    lo = odir / "WiFi.cpp.o"
    lo.write_bytes(b"o")

    maybe_refresh_object_cache(
        cache, build_dir=bd, core_path=c, variant_path=v, libraries=[lib]
    )
    assert lo.is_file()

    (bd / "build_opt.h").write_bytes(b"-DUSE_IPV6\n")
    maybe_refresh_object_cache(
        cache, build_dir=bd, core_path=c, variant_path=v, libraries=[lib]
    )
    assert not lo.is_file()


def test_clear_entire_object_cache_respects_temp_parent(tmp_path: Path) -> None:
    root = tmp_path / "acmake_objcache"
    root.mkdir()
    (root / "marker").write_text("x", encoding="utf-8")
    clear_entire_object_cache(temp_parent=str(tmp_path))
    assert not root.exists()
