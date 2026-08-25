"""Generate desktop/resources/icon.svg — vector trace of 图标.png.

The app icon is an R2D2-style silhouette in theme orange with white detail
panels, stored as a flat raster.  This script converts it to scalable vector
paths with marching-squares contour tracing (skimage) so the icon can be
re-rendered at any resolution without needing the raster source.

Usage: python scripts/gen_svg.py
"""
import os

import numpy as np
from PIL import Image
from skimage import measure

SRC = os.path.join(os.path.dirname(__file__), "..", "resources", "图标.png")
OUT = os.path.join(os.path.dirname(__file__), "..", "resources", "icon.svg")

BASE_COLOR = "#C26647"      # dominant opaque color of 图标.png
WHITE_COLOR = "#EAD7CC"     # representative cream of the white detail panels
ALPHA_THRESHOLD = 128
WHITE_MIN = 190             # r/g/b all >= this -> white detail pixel


def _trace(mask: np.ndarray, tolerance: float) -> list[str]:
    """Trace binary mask into SVG path substrings (evenodd-compatible)."""
    # Pad with a zero ring so every contour closes inside the array (the
    # silhouette touches all four image edges).
    padded = np.pad(mask.astype(np.float64), 1, mode="constant", constant_values=0.0)
    contours = measure.find_contours(
        padded, 0.5, fully_connected="high", positive_orientation="low"
    )
    paths: list[str] = []
    for c in contours:
        c = measure.approximate_polygon(c, tolerance)
        if len(c) < 3:
            continue
        c = c - 1.0  # remove padding ring
        # Douglas-Peucker can overshoot the canvas by <1px near edges.
        np.clip(c, 0.0, float(max(mask.shape)), out=c)
        d = "M{:.1f} {:.1f}".format(c[0, 1], c[0, 0])
        for pt in c[1:]:
            d += "L{:.1f} {:.1f}".format(pt[1], pt[0])
        d += "Z"
        paths.append(d)
    return paths


def main() -> None:
    arr = np.array(Image.open(SRC).convert("RGBA"))
    r, g, b, a = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2], arr[:, :, 3]
    h, w = a.shape

    # Douglas-Peucker simplification tolerance scaled to the source resolution
    # (~0.07% of the longest side keeps curves smooth at any render size).
    tolerance = max(w, h) * 0.00075

    base_mask = a > ALPHA_THRESHOLD
    white_mask = (a > ALPHA_THRESHOLD) & (r >= WHITE_MIN) & (g >= WHITE_MIN) & (b >= WHITE_MIN)

    base_paths = _trace(base_mask, tolerance)
    white_paths = _trace(white_mask, tolerance)

    def path_element(color: str, subpaths: list[str]) -> str:
        return (
            f'  <path fill="{color}" fill-rule="evenodd" d="'
            + "".join(subpaths)
            + '"/>\n'
        )

    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" role="img" aria-label="CluxMate logo">\n'
        f"  <!-- vector trace of {os.path.basename(SRC)} -->\n"
        + path_element(BASE_COLOR, base_paths)
        + path_element(WHITE_COLOR, white_paths)
        + "</svg>\n"
    )

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(svg)
    print(f"Saved {OUT}")
    print(f"  base subpaths: {len(base_paths)}, white subpaths: {len(white_paths)}")
    print(f"  size: {os.path.getsize(OUT) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
