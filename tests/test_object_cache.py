"""Object cache layout: FQBN segment + per-library bundles."""

from pathlib import Path

from acmake.build import _library_bundle_dir, _object_cache_root_for_fqbn
from acmake.cache_invalidate import _build_opt_fingerprint, sdk_config_fingerprint
from acmake.fqbn import FQBN, path_component_from_label
from acmake.libraries import Library


def test_object_cache_root_uses_fqbn_object_cache_key() -> None:
    fqbn = FQBN.parse("espressif:esp32:esp32s3:CPUFreq=240")
    cv = "3.0.0"
    root = _object_cache_root_for_fqbn(fqbn, cv)
    assert root.name == fqbn.object_cache_key(core_version=cv)
    assert len(root.name) == 32
    assert root.parent.name == "acmake_objcache"


def test_object_cache_key_changes_with_core_version() -> None:
    f = FQBN.parse("arduino:avr:uno")
    assert f.object_cache_key(core_version="1.8") != f.object_cache_key(core_version="1.9")


def test_object_cache_key_changes_with_board_options() -> None:
    a = FQBN.parse("espressif:esp32:esp32s3:CPUFreq=240")
    b = FQBN.parse("espressif:esp32:esp32s3:CPUFreq=80")
    assert a.object_cache_key() != b.object_cache_key()


def test_object_cache_key_stable_for_same_fqbn_string() -> None:
    s = "arduino:avr:uno"
    a = FQBN.parse(s).object_cache_key(core_version="2.0")
    b = FQBN.parse(s).object_cache_key(core_version="2.0")
    assert a == b


def test_object_cache_key_includes_build_opt_fingerprint() -> None:
    f = FQBN.parse("arduino:avr:uno")
    base = f.object_cache_key(core_version="1.0")
    a = f.object_cache_key(core_version="1.0", build_opt_fingerprint="aa" * 32)
    b = f.object_cache_key(core_version="1.0", build_opt_fingerprint="bb" * 32)
    assert a != b
    assert a != base
    assert b != base


def test_object_cache_root_differs_for_build_opt_bytes(tmp_path: Path) -> None:
    fqbn = FQBN.parse("arduino:avr:uno")
    bd_empty = tmp_path / "e" / "build"
    bd_empty.mkdir(parents=True)
    (bd_empty / "build_opt.h").write_bytes(b"")
    bd_flags = tmp_path / "f" / "build"
    bd_flags.mkdir(parents=True)
    (bd_flags / "build_opt.h").write_bytes(b"-DUSE_THING\n")
    r1 = _object_cache_root_for_fqbn(fqbn, "3.0.0", build_dir=bd_empty)
    r2 = _object_cache_root_for_fqbn(fqbn, "3.0.0", build_dir=bd_flags)
    assert r1 != r2
    assert r1.name != r2.name
    fp_empty = _build_opt_fingerprint(bd_empty)
    assert r1.name == fqbn.object_cache_key(
        core_version="3.0.0", build_opt_fingerprint=fp_empty
    )


def test_object_cache_key_includes_sdk_fingerprint() -> None:
    f = FQBN.parse("espressif:esp32:esp32s3")
    base = f.object_cache_key(core_version="3.3.0")
    a = f.object_cache_key(core_version="3.3.0", sdk_fingerprint="aa" * 32)
    b = f.object_cache_key(core_version="3.3.0", sdk_fingerprint="bb" * 32)
    assert a != b
    assert a != base
    assert b != base
    # Empty fingerprint leaves the key unchanged (non-SDK toolchains).
    assert f.object_cache_key(core_version="3.3.0", sdk_fingerprint="") == base


def test_object_cache_root_differs_for_sdk_config(tmp_path: Path) -> None:
    fqbn = FQBN.parse("espressif:esp32:esp32s3")
    sdk = tmp_path / "esp32-arduino-libs" / "esp32s3"
    (sdk / "flags").mkdir(parents=True)
    (sdk / "sdkconfig").write_bytes(b"CONFIG_BT_AUDIO=n\n")
    (sdk / "flags" / "defines").write_bytes(b"-DCONFIG_BT_AUDIO=0\n")
    fp_before = sdk_config_fingerprint({"compiler.sdk.path": str(sdk)})
    r1 = _object_cache_root_for_fqbn(fqbn, "3.3.0", sdk_fingerprint=fp_before)

    # Regenerate the SDK with a flipped Kconfig symbol (same core version).
    (sdk / "sdkconfig").write_bytes(b"CONFIG_BT_AUDIO=y\n")
    (sdk / "flags" / "defines").write_bytes(b"-DCONFIG_BT_AUDIO=1\n")
    fp_after = sdk_config_fingerprint({"compiler.sdk.path": str(sdk)})
    r2 = _object_cache_root_for_fqbn(fqbn, "3.3.0", sdk_fingerprint=fp_after)

    assert fp_before != fp_after
    assert r1 != r2
    assert r1.name != r2.name
    assert r1.name == fqbn.object_cache_key(
        core_version="3.3.0", sdk_fingerprint=fp_before
    )


def test_library_bundle_dir_stable_per_install(tmp_path: Path) -> None:
    cache = tmp_path / "acmake_objcache" / "esp32_esp32_esp32s3"
    lib_root = tmp_path / "MyLib"
    lib_root.mkdir()
    lib = Library(
        root=lib_root.resolve(),
        name="My Lib",
        architectures="*",
        depends=(),
        includes_override=None,
    )
    a = _library_bundle_dir(cache, lib)
    b = _library_bundle_dir(cache, lib)
    assert a == b
    assert a.parts[-2] == "lib"
    assert a.name.startswith("My_Lib_")


def test_library_bundle_differs_for_different_roots(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    r1 = tmp_path / "L1"
    r1.mkdir()
    r2 = tmp_path / "L2"
    r2.mkdir()
    lib_a = Library(
        root=r1.resolve(),
        name="SameName",
        architectures="*",
        depends=(),
        includes_override=None,
    )
    lib_b = Library(
        root=r2.resolve(),
        name="SameName",
        architectures="*",
        depends=(),
        includes_override=None,
    )
    assert _library_bundle_dir(cache, lib_a) != _library_bundle_dir(cache, lib_b)


def test_path_component_from_label() -> None:
    assert path_component_from_label("MyLib") == "MyLib"
    assert path_component_from_label("  ") == "x"
