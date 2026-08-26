"""Regenerate desktop/resources/icon.icns from icon.png (no macOS required).

ICNS is a trivial container: an "icns" magic + big-endian total-length header,
followed by a sequence of (4-byte type, big-endian chunk length, payload)
chunks. Modern ICNS embeds PNG data for each pixel size; Electron/macOS accept
the standard PNG-based type set. Run with:

    python desktop/scripts/make-icns.py
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image

RESOURCES = Path(__file__).resolve().parent.parent / "resources"

# (icns type code, pixel size). Order matches `iconutil` output: base sizes
# ascending, then the @2x (retina) variants.
_SIZES: list[tuple[str, int]] = [
    ("icp4", 16), ("icp5", 32), ("icp6", 64),
    ("ic07", 128), ("ic08", 256), ("ic09", 512), ("ic10", 1024),
    ("ic11", 32), ("ic12", 64), ("ic13", 256), ("ic14", 512),
]


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_icns(source: Path) -> bytes:
    base = Image.open(source).convert("RGBA")
    chunks: list[bytes] = []
    for code, size in _SIZES:
        payload = _png_bytes(base.resize((size, size), Image.LANCZOS))
        chunks.append(code.encode("ascii") + struct.pack(">I", 8 + len(payload)) + payload)
    body = b"".join(chunks)
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def main() -> None:
    src = RESOURCES / "icon.png"
    if not src.is_file():
        raise SystemExit(f"missing {src}")
    out = RESOURCES / "icon.icns"
    out.write_bytes(build_icns(src))
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
