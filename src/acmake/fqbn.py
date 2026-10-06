"""Parse Fully Qualified Board Names (FQBN) and board options."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_PATH_TOKEN = re.compile(r"[^\w.\-+]+")


def path_component_from_label(s: str, *, max_len: int = 48) -> str:
    """Sanitize a label (e.g. library name) for use as a single filesystem path segment."""
    t = _PATH_TOKEN.sub("_", s.strip()).strip("_")
    return (t or "x")[:max_len]


@dataclass(frozen=True)
class FQBN:
    vendor: str
    arch: str
    board_id: str
    options: dict[str, str] = field(default_factory=dict)

    @property
    def fqbn_arch(self) -> str:
        """Architecture string used in library.properties (e.g. avr, esp32)."""
        return self.arch

    def to_string(self) -> str:
        base = f"{self.vendor}:{self.arch}:{self.board_id}"
        if not self.options:
            return base
        opts = ",".join(f"{k}={v}" for k, v in sorted(self.options.items()))
        return f"{base}:{opts}"

    def build_path_segment(self) -> str:
        """Single path component for default build dirs (no ':' ',' '=' in names)."""
        parts = [self.vendor, self.arch, self.board_id]
        for k, v in sorted(self.options.items()):
            parts.append(_PATH_TOKEN.sub("_", k).strip("_"))
            parts.append(_PATH_TOKEN.sub("_", v).strip("_"))
        return "_".join(parts)

    def object_cache_key(
        self,
        *,
        core_version: str = "",
        compiler_warnings: str | None = None,
        build_property_tag: str | None = None,
        build_opt_fingerprint: str | None = None,
        sdk_fingerprint: str | None = None,
    ) -> str:
        """Fixed-length dir under ``acmake_objcache/`` (FQBN + core package ``version``).

        When *compiler_warnings* is set (``--warnings``), it is folded into the key so
        cached ``.o`` files built with different warning levels do not mix.

        *build_property_tag* is a short stable digest of ``--build-property`` overrides
        so different custom flags do not share the same cached objects.

        *build_opt_fingerprint* is the SHA-256 hex digest of ``build_opt.h`` bytes under the
        sketch ``build/`` directory (see ``acmake.cache_invalidate``). When set, it is folded
        in so **different** ``build_opt.h``
        contents use **separate** core / variant / library cache subtrees (same bytes →
        same key, including across different sketch ``build/`` directories).

        *sdk_fingerprint* is the SHA-256 hex digest of the platform SDK's config inputs
        (``compiler.sdk.path``: the generated ``sdkconfig`` plus the ``flags/*`` define /
        include files — see ``acmake.cache_invalidate.sdk_config_fingerprint``). The core
        package ``version`` does **not** change when those libs are regenerated locally
        (e.g. flipping a Kconfig symbol), yet the bytes baked onto every compile line do.
        Folding the digest in gives each SDK config its own core / variant / library cache
        subtree, so cached ``.o`` files built against a different ``sdkconfig`` are never
        reused. Empty on toolchains without an SDK path, leaving the key unchanged.
        """
        cv = (core_version or "").strip()
        w = (compiler_warnings or "").strip().lower()
        t = (build_property_tag or "").strip()
        bo = (build_opt_fingerprint or "").strip()
        sdk = (sdk_fingerprint or "").strip()
        parts = [self.to_string(), cv]
        if w:
            parts.append(f"warnings={w}")
        if t:
            parts.append(f"props={t}")
        if bo:
            parts.append(f"build_opt={bo}")
        if sdk:
            parts.append(f"sdk={sdk}")
        blob = "\0".join(parts)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def parse(s: str) -> FQBN:
        s = s.strip()
        if not s:
            raise ValueError("empty FQBN")
        parts = s.split(":", 3)
        if len(parts) < 3:
            raise ValueError(
                f"FQBN must be vendor:arch:board[:opt=val,...], got {s!r}"
            )
        vendor, arch, board_id = parts[0], parts[1], parts[2]
        options: dict[str, str] = {}
        opt_blob = parts[3] if len(parts) > 3 else ""
        if opt_blob:
            for chunk in opt_blob.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                if "=" not in chunk:
                    raise ValueError(f"invalid board option {chunk!r} in FQBN")
                k, v = chunk.split("=", 1)
                options[k.strip()] = v.strip()
        return FQBN(vendor=vendor, arch=arch, board_id=board_id, options=options)
