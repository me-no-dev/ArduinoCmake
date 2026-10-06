# acmake — Arduino CMake builder

Python CLI that resolves an **FQBN**, discovers locally installed **Arduino cores and libraries**, generates **CMake + Ninja** build files from `platform.txt` / `boards.txt` recipes, compiles sketches, and can run **upload** patterns.

## Requirements

- Python 3.10+
- **CMake** 3.20+ and **Ninja** on `PATH`
- Arduino packages in the usual data directory (e.g. `~/Library/Arduino15` on macOS)
- Optional: **`arduino-preprocessor`** on `PATH` (or next to `arduino-cli`) for `.ino` prototype generation

## How to run the program

Use any one of these; the CLI is the same (`compile`, `upload`, `cache-clear`, `board`).

| Method | When |
|--------|------|
| `acmake …` | After `pip install -e .` or a normal install; `acmake` is on `PATH`. |
| `python -m acmake …` | Installed package; avoids shadowing by a local `acmake.py`. |
| `python3 acmake.py …` | From this repository root, without installing (prepends `src/` on `sys.path`). |
| `chmod +x acmake.py && ./acmake.py …` | Same as above if the file is executable. |
| `python3 arduino_cmake.py …` | **Portable** single file in the repo root (or copy it anywhere). Extracts the bundled `acmake` package under `~/.cache/arduino_cmake_portable/<id>/` on first use. |
| `./arduino_cmake.py …` | After `chmod +x arduino_cmake.py`. |

To rebuild `arduino_cmake.py` after changing sources under `src/acmake/`:

```bash
python3 tools/gen_arduino_cmake_portable.py
```

### Install (editable, for development)

```bash
pip install -e ".[dev]"
```

## Global options (all subcommands)

These apply before the subcommand name:

| Option | Description |
|--------|-------------|
| `-h`, `--help` | Show help for the top-level parser or `acmake <subcommand> -h`. |
| `-v` / `-vv` | Increase verbosity (`-v`: print CMake configure/build commands; `-vv`: more internal detail). |
| `--data-dir DIR` | Arduino **data** directory (Board Manager cores, tools). Default: `ARDUINO_DIRECTORIES_DATA` or OS default (e.g. `~/Library/Arduino15`). |
| `--user-dir DIR` | Sketchbook / **user** directory (sketches, user `hardware/`, `libraries/`). Default: `ARDUINO_DIRECTORIES_USER`, else **`~/Arduino`** on Linux or **`~/Documents/Arduino`** on macOS and Windows. |

## `acmake compile`

Generate CMake in the build directory, run CMake + Ninja, run optional post-build hooks, then print size / library summaries.

| Option | Required | Description |
|--------|----------|-------------|
| `--fqbn` | yes | Board FQBN, e.g. `arduino:avr:uno` or `espressif:esp32:esp32s3:PartitionScheme=default,CPUFreq=240`. **Quote** the whole value in the shell so commas stay in one argument. |
| `--sketch` | yes | Path to the sketch folder (the directory that contains the `.ino` files). |
| `--build-path` | no | Build output directory (same as `arduino-cli --build-path`). Default: `<sketch>/build`. |
| `--output-dir` | no | Alias for `--build-path` when `--build-path` is not set. |
| `--ide-version` | no | Value for `runtime.ide.version` style defines (default: `10607`). |
| `--clean` | no | Delete the build directory before configuring. |
| `--no-preprocessor` | no | Skip `arduino-preprocessor`; concatenates `.ino` files only (may break link if the sketch needs prototypes). |
| `--export-binaries` | no | After a successful build, run `recipe.hooks.savehex.presavehex` / `postsavehex` (same idea as `arduino-cli --export-binaries`). |
| `--no-object-cache` | no | Keep all `.o` files and `core.a` under the sketch build dir instead of the shared temp cache (see below). |
| `--warnings LEVEL` | no | One of `none`, `default`, `more`, `all`: sets ``compiler.warning_flags`` from ``compiler.warning_flags.<LEVEL>`` in ``platform.txt`` (same as arduino-cli ``--warnings``). With the shared object cache on, this selects a separate cache subtree so flags stay consistent. |
| `--build-property KEY=VALUE` | no | Override a merged platform/board property (repeatable; same as arduino-cli ``--build-property``). First ``=`` separates key from value, so values may contain ``=``. Changes the object-cache bucket when the shared cache is on. |

Examples:

```bash
acmake compile --fqbn arduino:avr:uno --sketch /path/to/Blink
acmake compile --fqbn arduino:avr:uno --sketch /path/to/Blink --clean
acmake compile --fqbn arduino:avr:uno --sketch /path/to/Blink --build-path /tmp/mybuild
acmake compile --fqbn arduino:avr:uno --sketch /path/to/Blink --no-preprocessor
acmake compile --fqbn arduino:avr:uno --sketch /path/to/Blink --export-binaries
acmake -v compile --fqbn arduino:avr:uno --sketch /path/to/Blink
acmake compile --fqbn 'espressif:esp32:esp32s3:USBMode=default' --sketch /path/to/MySketch \
  --data-dir ~/Library/Arduino15 --user-dir ~/Documents/Arduino
acmake compile --fqbn espressif:esp32:esp32s3 --sketch /path/to/Sketch \
  --build-property 'compiler.warning_flags.all=-Wall -Werror=all -Wextra'
```

### Shared object cache (default on)

Object files for **core**, **variant**, and **each linked library** are shared under the system temp directory. The cache directory name is **32 hex characters**: SHA-256 (first 32 hex chars) of ``FQBN.to_string()`` + a NUL + the board package **version** string from ``platform.txt`` / expanded properties (so different core package versions never share the same cache tree). The SHA-256 **hex digest of ``build_opt.h`` bytes** in the current sketch ``build/`` directory is also mixed in (same bytes → same subtree, including across different examples; different bytes → a separate subtree). When the platform defines ``compiler.sdk.path`` (e.g. the ESP32 core's ``esp32-arduino-libs``), a ``sdk=…`` digest of that SDK's config inputs — the generated ``sdkconfig`` plus the ``flags/*`` define / include files — is mixed in as well, so regenerating the SDK with a different Kconfig (which does **not** bump the core package version) gets its own subtree instead of reusing ``.o`` files compiled against the old ``sdkconfig``. If you pass ``--warnings <LEVEL>``, a ``warnings=<LEVEL>`` segment is mixed into that hash so cached objects are not reused across different warning levels. If you pass one or more ``--build-property`` flags, a digest of those strings is added as ``props=…`` for the same reason. The **per-sketch build dir** defaults to ``<sketch>/build`` (override with ``--build-path``).

Layout (sketch ``.o`` files stay under the sketch build dir):

- ``<temp>/acmake_objcache/<32-hex-key>/core/o/…`` and ``…/core/core.a``
- ``<temp>/acmake_objcache/<32-hex-key>/variant/o/…``
- ``<temp>/acmake_objcache/<32-hex-key>/lib/<LibraryName>_<hash>/o/…``

**Invalidation (before each compile):** fingerprints under ``<cache>/.acmake_stamps/`` track when to delete cached ``.o`` (Ninja then rebuilds).

1. **Platform / global:** If **any header** under the core or variant trees changes, all cached **core**, **variant**, and **library** objects **for that 32-hex cache subtree** are removed (full wipe of those paths and ``core.a``). The fingerprint lives under ``<cache>/.acmake_stamps/platform_hdr_fp``. **``build_opt.h``** does not use this wipe: different ``build_opt.h`` **contents** select a **different** cache subtree (see the cache key paragraph above), so toggling flags does not destroy another subtree’s objects. ``build_opt.h`` is **not** scanned as a C header; it is **not** given default ``#pragma once`` or other boilerplate—only your sketch’s ``build_opt.h`` is copied when present, otherwise an **empty** file is created for recipes that ``-include`` it (build flags / defines only).
2. **Per core ``.o``:** After invalidation, each core object is rebuilt when its **``.c`` / ``.cpp`` / …** is newer than the ``.o`` (see ``cache_compile.py``).
3. **Per library subtree:** Each bundle’s ``.acmake_hdr_fp`` combines a digest of the library install path, ``library.properties`` bytes (version, ``depends=``, …), and **contents** of library ``*.h`` / ``*.cpp`` / … under that install (excluding ``examples/``, ``tests/``, etc.) with the **``build_opt.h`` content** digest for the **current** sketch ``build/`` copy. In normal use the cache subtree already matches that digest (see cache key); the stamp still catches odd cases (e.g. one cache path reused with different ``build_dir`` values). Mtime-only changes on library sources or on ``build_opt.h`` do **not** invalidate by themselves. Core / variant header drift is still (1).
4. **Per library ``.o``:** Same mtime rule as (2) for the translation unit source.

**CMake** still runs ``<python> <path/to/cache_compile.py> …`` for cached compiles (no ``acmake`` package needed on ``PYTHONPATH``). Ninja **DEPFILE** is not used for those rules; sketch objects still use ``-MMD`` / DEPFILE under ``<sketch build>/.acmake_deps/``.

- **Clear everything:** ``acmake cache-clear`` removes the entire ``<temp>/acmake_objcache`` directory (all FQBNs / versions).
- Disable cache for one build: ``acmake compile … --no-object-cache`` (``upload`` has the same flag).
- Environment: ``ACMAKE_OBJECT_CACHE=0`` (``false``, ``no``, ``off`` also turn it off).
- ``-v`` logs the resolved cache path as ``object cache: …``.

``build_opt.h`` is copied from the sketch only when bytes differ (so mtimes do not churn every run). ``sketch.cpp`` from the preprocessor is written only when output bytes differ. Library discovery and CMake dependency lists use deterministic ordering so ``CMakeLists.txt`` stays stable when nothing real changed. For shared-cached **library** compiles, paths under the sketch ``build_dir`` from recipe ``@…`` stubs (e.g. ``build_opt.h``) are omitted from Ninja ``DEPENDS`` so a brand-new example ``build/`` does not make Ninja re-run every linked library; the compiler argv still includes those files, and (1)–(3) still invalidate when they matter. Each cached library TU also gets its own ``-I`` list (core + variant + that library + others it pulls in via ``depends=`` or ``#include`` tokens among **already linked** libraries), not the whole sketch link set, so ``WiFi.cpp`` compiles the same across examples that add unrelated libraries. For those same rules, ``-MF`` points at a depfile path **relative to the sketch build dir** (not an absolute path under ``…/OtherSketch/build/``), so Ninja does not treat a new example directory as a different command for the same shared ``.o``. ESP32 also puts ``-I{build.source.path}`` in ``compiler.cpreprocessor.flags``; that flag is **removed** from cached core/variant/lib recipes only (sketch translation units keep it) so the rule text does not embed each example’s ``.ino`` directory path. ``cmake -S … -B …`` runs only until ``CMakeCache.txt`` and ``build.ninja`` exist; later builds use ``cmake --build`` only.

## `acmake cache-clear`

Deletes the whole shared object cache directory (``<temp>/acmake_objcache``). No arguments. Use after toolchain upgrades or if you want a clean slate for all boards.

## `acmake upload`

Resolves the same FQBN/sketch/build layout as `compile`, then runs the platform’s `upload.tool` recipe (e.g. `esptool`, `avrdude`).

| Option | Required | Description |
|--------|----------|-------------|
| `--fqbn` | yes | Same as `compile`. |
| `--sketch` | yes | Sketch directory. |
| `--port` | yes | Serial device, e.g. `/dev/cu.usbmodem14101` or `COM3`. |
| `--build-path` | no | Build directory (default: same as `compile`, `<sketch>/build`). |
| `--output-dir` | no | Alias for `--build-path` when `--build-path` is not set. |
| `--ide-version` | no | Same default as `compile`. |
| `--no-preprocessor` | no | Same meaning as `compile` (must match how the build was produced if you rely on sketch.cpp layout). |
| `--dry-run` | no | Print the upload command only; do not run it. |
| `--no-object-cache` | no | Same as for `compile`: skip the shared object cache when resolving the build plan. |
| `--warnings LEVEL` | no | Same as `compile`: ``compiler.warning_flags.<LEVEL>`` from ``platform.txt``. |
| `--build-property KEY=VALUE` | no | Same as `compile`: override one platform/board property (repeatable). |

Examples:

```bash
acmake upload --fqbn arduino:avr:uno --sketch /path/to/Blink --port /dev/cu.usbmodem14101
acmake upload --fqbn espressif:esp32:esp32s3 --sketch /path/to/Sketch --port /dev/cu.usbserial-* --dry-run
acmake upload --fqbn arduino:avr:uno --sketch /path/to/Blink --port COM5 --build-path /tmp/mybuild
```

## `acmake board`

List installed board FQBNs discovered from the data directory, user directory, and optional sketch-local `hardware/` trees.

| Option | Required | Description |
|--------|----------|-------------|
| `--sketch DIR` | no | Repeatable. For each sketch, also list boards under `<sketch>/hardware/…`. |

Examples:

```bash
acmake board
acmake board --sketch /path/to/MySketch
acmake board --sketch /path/to/A --sketch /path/to/B
```

## Environment variables

Same semantics as Arduino CLI:

- **`ARDUINO_DIRECTORIES_DATA`** — data directory (cores, built-in tools).
- **`ARDUINO_DIRECTORIES_USER`** — user directory (sketchbook, user `hardware/`, `libraries/`). If unset, acmake uses **`~/Arduino`** on Linux and **`~/Documents/Arduino`** on macOS and Windows (same idea as Arduino IDE defaults).

You can override per invocation with `--data-dir` and `--user-dir`.

## Custom cores next to a sketch

If a core lives under the sketch (portable layout), put it at:

`<sketch>/hardware/<vendor>/<arch>/<version>/` (with `platform.txt` and `boards.txt` like Board Manager packages).

`compile` / `upload` resolve **sketch `hardware/` first**, then the sketchbook `hardware/` folder, then `Arduino15/packages/`. When choosing a version folder under `hardware/<vendor>/<arch>/`, only subdirectories that contain **`platform.txt`** are considered (so a sibling **`libraries/`** tree does not get mistaken for a core release).

Tools are still resolved from `packages/` and from that platform’s own `tools/` directory when present.

## Limitations

Cores that do not use `recipe.c.combine.pattern` / standard objcopy recipes may need extra support. Conformance is validated incrementally against installed `arduino-cli` where available.
