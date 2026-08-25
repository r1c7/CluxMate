"""Recolor R2D2 image to pure theme orange silhouette — no other colors."""
from PIL import Image
import os

SRC = os.path.join(os.path.dirname(__file__), "..", "resources", "R2D2.jpeg")
OUT = os.path.join(os.path.dirname(__file__), "..", "resources", "r2d2_orange.png")

# Theme orange
ACCENT = (217, 119, 87)

img = Image.open(SRC).convert("RGBA")
px = img.load()
w, h = img.size

# First pass: find the pixel with the highest luminosity (the "white" point)
# to use as reference for normalizing.
max_lum = 0
for y in range(h):
    for x in range(w):
        r, g, b, a = px[x, y]
        if a > 10:
            lum = r * 0.299 + g * 0.587 + b * 0.114
            max_lum = max(max_lum, lum)

out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
opx = out.load()

for y in range(h):
    for x in range(w):
        r, g, b, a = px[x, y]
        if a < 10:
            opx[x, y] = (0, 0, 0, 0)
            continue

        # Luminance of this pixel
        lum = r * 0.299 + g * 0.587 + b * 0.114

        # Normalize brightness 0-1 where max_lum maps to 1
        intensity = lum / max_lum if max_lum > 0 else 1.0
        intensity = max(0.0, min(1.0, intensity))

        # Map to theme orange with the same intensity
        or_ = int(ACCENT[0] * intensity)
        og = int(ACCENT[1] * intensity)
        ob = int(ACCENT[2] * intensity)

        # Clamp
        or_ = max(0, min(255, or_))
        og = max(0, min(255, og))
        ob = max(0, min(255, ob))

        opx[x, y] = (or_, og, ob, a)

out.save(OUT, "PNG")
print(f"Saved: {OUT}")
print(f"Reference max_lum: {max_lum:.0f}")
print(f"All pixels recolored to orange (#{ACCENT[0]:02x}{ACCENT[1]:02x}{ACCENT[2]:02x}) with brightness preserved")
