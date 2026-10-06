"""Command-line interface for acmake."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from acmake.board_list import list_installed_boards
from acmake.build import collect_gcov_notes, prepare_build
from acmake.properties import parse_build_property
from acmake.cache_invalidate import clear_entire_object_cache
from acmake.cmakegen import write_cmake
from acmake.hooks import PRE_NINJA_HOOK_PHASES, post_ninja_hook_phases, run_hook_phases
from acmake.config import ArduinoPaths
from acmake.fqbn import FQBN
from acmake.logging_util import setup_logging
from acmake.size_report import print_library_report, print_size_report
from acmake.upload import run_upload


def _cli_build_property(spec: str) -> str:
    """Validate ``KEY=VALUE`` for ``--build-property``; return *spec* unchanged."""
    parse_build_property(spec)
    return spec


def resolve_sketch_build_dir(
    sketch: Path,
    *,
    build_path: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> Path:
    """Sketch build directory (arduino-cli ``--build-path`` semantics).

    Precedence: ``build_path`` > ``output_dir`` > ``<sketch>/build``.
    """
    if build_path:
        return Path(build_path).expanduser().resolve()
    if output_dir:
        return Path(output_dir).expanduser().resolve()
    return (sketch / "build").resolve()


def should_run_cmake_configure(build_dir: Path) -> bool:
    """True only for an **unconfigured** tree (first generation).

    Do **not** compare ``CMakeLists.txt`` vs ``build.ninja`` mtimes: they are unreliable
    (touch order, filesystem granularity). After the first ``cmake -S … -B …``, use
    only ``cmake --build``: Ninja’s built-in rule will rerun CMake when
    ``CMakeLists.txt`` (or other generator inputs) actually change—without our
    forcing a full configure every compile, which was rebuilding the whole object cache.
    """
    bpath = Path(build_dir).resolve()
    return not (bpath / "CMakeCache.txt").is_file() or not (bpath / "build.ninja").is_file()


def _run_cmake(build_dir: Path, verbose: bool) -> None:
    """Run initial configure only once; otherwise rely on ``cmake --build`` / Ninja."""
    bd = str(build_dir.resolve())
    cmake = shutil.which("cmake")
    if not cmake:
        raise FileNotFoundError("cmake not found on PATH")
    bpath = Path(bd)

    if should_run_cmake_configure(bpath):
        cmd = [cmake, "-S", bd, "-B", bd, "-G", "Ninja"]
        if verbose:
            print(" ".join(cmd))
        subprocess.run(cmd, check=True)

    cmd2 = [cmake, "--build", bd]
    if verbose:
        print(" ".join(cmd2))
    subprocess.run(cmd2, check=True)


def cmd_compile(args: argparse.Namespace) -> int:
    log = setup_logging(args.verbose)
    paths = ArduinoPaths.from_env(
        Path(args.data_dir).expanduser() if args.data_dir else None,
        Path(args.user_dir).expanduser() if args.user_dir else None,
    )
    fqbn = FQBN.parse(args.fqbn)
    sketch = Path(args.sketch).expanduser().resolve()
    out = resolve_sketch_build_dir(
        sketch,
        build_path=getattr(args, "build_path", None),
        output_dir=getattr(args, "output_dir", None),
    )
    if args.clean and out.exists():
        shutil.rmtree(out)
    log.info("sketch=%s fqbn=%s build=%s", sketch, fqbn.to_string(), out)
    plan = prepare_build(
        fqbn,
        sketch,
        out,
        paths,
        runtime_ide_version=args.ide_version,
        use_preprocessor=not args.no_preprocessor,
        verbose=args.verbose >= 2,
        use_object_cache=not args.no_object_cache,
        compiler_warnings=getattr(args, "compiler_warnings", None),
        build_properties=getattr(args, "build_properties", None),
    )
    if args.verbose >= 1 and plan.library_resolution_notes:
        log.info("Linked libraries (%d):", len(plan.library_resolution_notes))
        for n in plan.library_resolution_notes:
            hdr = (
                f' (public header in this install: {n.public_header_relpath})'
                if n.public_header_relpath
                else ""
            )
            log.info(
                '  + %r ← include token %r%s — %s',
                n.library_name,
                n.include_token,
                hdr,
                n.via,
            )
    if plan.object_cache_dir is not None and args.verbose >= 1:
        log.info("object cache: %s", plan.object_cache_dir)
    write_cmake(plan)
    run_hook_phases(
        plan.expanded,
        plan.build_dir,
        PRE_NINJA_HOOK_PHASES,
        verbose=args.verbose >= 2,
    )
    _run_cmake(out, args.verbose >= 1)
    collect_gcov_notes(plan)
    if args.export_binaries:
        log.info("export-binaries: running recipe.hooks.savehex.*")
    run_hook_phases(
        plan.expanded,
        plan.build_dir,
        post_ninja_hook_phases(
            plan.expanded, export_binaries=args.export_binaries
        ),
        verbose=args.verbose >= 2,
    )
    fits = print_size_report(plan)
    print_library_report(plan)
    if not fits:
        log.error("build failed: sketch does not fit the selected board/partition scheme")
        return 1
    log.info(
        "build finished: %s",
        plan.hex_path or plan.bin_path or plan.elf_path,
    )
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    log = setup_logging(args.verbose)
    paths = ArduinoPaths.from_env(
        Path(args.data_dir).expanduser() if args.data_dir else None,
        Path(args.user_dir).expanduser() if args.user_dir else None,
    )
    fqbn = FQBN.parse(args.fqbn)
    sketch = Path(args.sketch).expanduser().resolve()
    out = resolve_sketch_build_dir(
        sketch,
        build_path=getattr(args, "build_path", None),
        output_dir=getattr(args, "output_dir", None),
    )
    plan = prepare_build(
        fqbn,
        sketch,
        out,
        paths,
        runtime_ide_version=args.ide_version,
        use_preprocessor=not args.no_preprocessor,
        verbose=args.verbose >= 2,
        use_object_cache=not args.no_object_cache,
        compiler_warnings=getattr(args, "compiler_warnings", None),
        build_properties=getattr(args, "build_properties", None),
    )
    run_upload(plan, args.port, dry_run=args.dry_run)
    return 0


def cmd_cache_clear(args: argparse.Namespace) -> int:
    _ = args
    root = Path(tempfile.gettempdir()) / "acmake_objcache"
    existed = root.is_dir()
    clear_entire_object_cache()
    if existed:
        print(f"Removed object cache: {root}")
    else:
        print(f"No object cache directory at {root}")
    return 0


def cmd_board_list(args: argparse.Namespace) -> int:
    paths = ArduinoPaths.from_env(
        Path(args.data_dir).expanduser() if args.data_dir else None,
        Path(args.user_dir).expanduser() if args.user_dir else None,
    )
    sketches = [Path(s).expanduser().resolve() for s in (args.sketch or [])]
    for line in list_installed_boards(paths, sketch_dirs=sketches or None):
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        help="Arduino data dir (default: ARDUINO_DIRECTORIES_DATA or OS default)",
    )
    common.add_argument(
        "--user-dir",
        help=(
            "Sketchbook / user dir (env ARDUINO_DIRECTORIES_USER, else ~/Arduino on Linux, "
            "~/Documents/Arduino on macOS/Windows)"
        ),
    )

    p = argparse.ArgumentParser(
        prog="acmake",
        description="Arduino sketch builder (CMake + FQBN)",
        parents=[common],
    )
    p.add_argument("-v", "--verbose", action="count", default=0)

    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compile", help="Generate CMake and build sketch")
    c.add_argument(
        "--fqbn",
        required=True,
        help=(
            "e.g. arduino:avr:uno or espressif:esp32:esp32s3:Opt=a,Opt2=b "
            "(quote the whole value in the shell so commas stay in one argument)"
        ),
    )
    c.add_argument("--sketch", required=True, help="Path to sketch folder")
    c.add_argument(
        "--build-path",
        dest="build_path",
        metavar="DIR",
        help="Build output directory (same role as arduino-cli --build-path; default: <sketch>/build)",
    )
    c.add_argument(
        "--output-dir",
        dest="output_dir",
        metavar="DIR",
        help="Alias for --build-path when --build-path is not set",
    )
    c.add_argument("--ide-version", default="10607", help="runtime.ide.version for defines")
    c.add_argument("--clean", action="store_true", help="Delete build dir before build")
    c.add_argument(
        "--no-preprocessor",
        action="store_true",
        help="Skip arduino-preprocessor (concat .ino only; may fail link)",
    )
    c.add_argument(
        "--export-binaries",
        action="store_true",
        help=(
            "After a successful build, run recipe.hooks.savehex.presavehex / "
            "recipe.hooks.savehex.postsavehex (export binaries; same idea as arduino-cli --export-binaries)"
        ),
    )
    c.add_argument(
        "--no-object-cache",
        action="store_true",
        help=(
            "Disable shared core/variant/library object cache under the system temp dir "
            "(default: on; override with env ACMAKE_OBJECT_CACHE=0)"
        ),
    )
    c.add_argument(
        "--warnings",
        dest="compiler_warnings",
        metavar="LEVEL",
        choices=["none", "default", "more", "all"],
        help=(
            "Maps to compiler.warning_flags.<LEVEL> in platform.txt (same idea as arduino-cli --warnings)"
        ),
    )
    c.add_argument(
        "--build-property",
        dest="build_properties",
        metavar="KEY=VALUE",
        action="append",
        type=_cli_build_property,
        help=(
            "Override a platform/board property (repeatable; same as arduino-cli --build-property). "
            "Example: --build-property 'compiler.warning_flags.all=-Wall -Werror'"
        ),
    )
    c.set_defaults(func=cmd_compile)

    u = sub.add_parser("upload", help="Run upload.tool recipe")
    u.add_argument("--fqbn", required=True)
    u.add_argument("--sketch", required=True)
    u.add_argument(
        "--build-path",
        dest="build_path",
        metavar="DIR",
        help="Build directory (default: <sketch>/build; same as compile)",
    )
    u.add_argument(
        "--output-dir",
        dest="output_dir",
        metavar="DIR",
        help="Alias for --build-path when --build-path is not set",
    )
    u.add_argument("--port", required=True, help="Serial device, e.g. /dev/cu.usbmodem*")
    u.add_argument("--ide-version", default="10607")
    u.add_argument("--no-preprocessor", action="store_true")
    u.add_argument("--dry-run", action="store_true", help="Print upload command only")
    u.add_argument(
        "--no-object-cache",
        action="store_true",
        help="Same as compile: do not use the shared object cache when resolving the build",
    )
    u.add_argument(
        "--warnings",
        dest="compiler_warnings",
        metavar="LEVEL",
        choices=["none", "default", "more", "all"],
        help="Same as compile: compiler.warning_flags.<LEVEL> from platform.txt",
    )
    u.add_argument(
        "--build-property",
        dest="build_properties",
        metavar="KEY=VALUE",
        action="append",
        type=_cli_build_property,
        help="Same as compile: override one platform/board property (repeatable)",
    )
    u.set_defaults(func=cmd_upload)

    cc = sub.add_parser(
        "cache-clear",
        help="Delete the entire shared object cache under the system temp dir (all FQBNs)",
    )
    cc.set_defaults(func=cmd_cache_clear)

    bl = sub.add_parser("board", help="List installed board FQBNs")
    bl.add_argument(
        "--sketch",
        action="append",
        metavar="DIR",
        help="Sketch folder; also list boards under <sketch>/hardware (repeatable)",
    )
    bl.set_defaults(func=cmd_board_list)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
