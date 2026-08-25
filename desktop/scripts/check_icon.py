"""Check rendered icon."""
from PIL import Image
img = Image.open("E:/workspace/CluxMate/desktop/resources/icon.png")
px = img.load()
w, h = img.size
print("BG at (256,256):", px[256,256])
print("Inner bubble (200,180):", px[200,180])
print("Edge (120,80):", px[120,80])
px_w = sum(1 for y in range(h) for x in range(w) if px[x,y][0]>200 and px[x,y][3]>0)
print(f"White pixels: {px_w}")
# Check if bubble is actually visible
for y in range(0, h, 64):
    for x in range(0, w, 64):
        r,g,b,a = px[x,y]
        if a>0:
            print(f"  ({x},{y}): #{r:02x}{g:02x}{b:02x}")
