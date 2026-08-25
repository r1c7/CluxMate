"""Verify generated icon."""
from PIL import Image
img = Image.open("E:/workspace/CluxMate/desktop/resources/icon.png")
px = img.load()
w, h = img.size

# Bbox of content (white pixels)
mx, xx, my, xy = w, 0, h, 0
count = 0
for y in range(h):
    for x in range(w):
        r, g, b, a = px[x, y]
        if a > 0:
            count += 1
            mx = min(mx, x)
            xx = max(xx, x)
            my = min(my, y)
            xy = max(xy, y)

print(f"Opaque pixels: {count}")
print(f"bbox: ({mx},{my})-({xx},{xy})")
print(f"content size: {xx-mx}x{xy-my}")
cx = (mx + xx) // 2
cy = (my + xy) // 2
print(f"center: ({cx},{cy}) vs img center: ({w//2},{h//2})")

# Check bg color (corner of rounded rect, not edge)
print(f"BG at (10,10): rgba{px[10,10]} (expected: (176,84,58,255))")
print(f"BG at (10,500): rgba{px[10,500]}")
