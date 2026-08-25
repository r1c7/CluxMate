"""Check actual icon colors."""
from PIL import Image
from collections import Counter
img = Image.open("E:/workspace/CluxMate/desktop/resources/icon.png")
px = img.load()
w, h = img.size
samples = []
for y in range(0, h, 8):
    for x in range(0, w, 8):
        r, g, b, a = px[x, y]
        if a > 128:
            samples.append(f"#{r:02x}{g:02x}{b:02x}")
top = Counter(samples).most_common(15)
print(f"Sampled {len(samples)} pixels\n")
for c, n in top:
    print(f"  {c}  x{n}")
print(f"\nTarget: #b0543a = rgb(176,84,58)")
print(f"Current accent in code: ACCENT = (176, 84, 58)")

# Also check r2d2_orange.png
print("\n--- r2d2_orange.png ---")
img2 = Image.open("E:/workspace/CluxMate/desktop/resources/r2d2_orange.png")
px2 = img2.load()
w2, h2 = img2.size
samples2 = []
for y in range(0, h2, 8):
    for x in range(0, w2, 8):
        r, g, b, a = px2[x, y]
        if a > 128:
            samples2.append(f"#{r:02x}{g:02x}{b:02x}")
top2 = Counter(samples2).most_common(15)
for c, n in top2:
    print(f"  {c}  x{n}")
