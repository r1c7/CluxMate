"""Analyze R2D2 image colors and recolor to theme orange."""
from PIL import Image
import os

SRC = os.path.join(os.path.dirname(__file__), "..", "resources", "R2D2.jpeg")
ACCENT = (217, 119, 87)  # theme orange

img = Image.open(SRC).convert("RGBA")
px = img.load()
w, h = img.size

# Quantize color buckets
buckets = {}
for y in range(h):
    for x in range(w):
        r, g, b, a = px[x, y]
        if a > 128:
            k = (r // 32, g // 32, b // 32)
            buckets[k] = buckets.get(k, 0) + 1

sc = sorted(buckets.items(), key=lambda x: -x[1])
total = sum(buckets.values())
print(f"Total non-transparent: {total} pixels\n")
print("Top color buckets:")
for (cr, cg, cb), n in sc[:20]:
    r = cr * 32 + 16
    g = cg * 32 + 16
    b = cb * 32 + 16
    pct = n / total * 100
    # Classify luminance
    lum = r * 0.299 + g * 0.587 + b * 0.114
    label = "white" if lum > 200 else "lt-gray" if lum > 160 else "gray" if lum > 100 else "dk-gray" if lum > 50 else "black"
    print(f"  #{r:02x}{g:02x}{b:02x}  rgb({r:3d},{g:3d},{b:3d})  {pct:5.1f}%  {label}")
