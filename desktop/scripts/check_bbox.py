"""Check R2D2 image bounding box."""
from PIL import Image
img = Image.open("E:/workspace/CluxMate/desktop/resources/r2d2_orange.png")
px = img.load()
w, h = img.size
mx, xx, my, xy = w, 0, h, 0
for y in range(h):
    for x in range(w):
        if px[x, y][3] > 0:
            mx = min(mx, x)
            xx = max(xx, x)
            my = min(my, y)
            xy = max(xy, y)
print(f"bbox: ({mx},{my})-({xx},{xy})")
print(f"size: {xx-mx}x{xy-my}")
print(f"center: ({(mx+xx)//2},{(my+xy)//2})")
print(f"image center: ({w//2},{h//2})")
