"""Generate CluxMate application icons from 图标.png source."""
from PIL import Image
import os

SRC = os.path.join(os.path.dirname(__file__), "..", "resources", "图标.png")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "resources")

PNG_SIZE = 512
ICO_SIZES = [16, 24, 32, 48, 64, 96, 128, 256]
TRAY_SIZE = 22


def generate_final(size: int) -> Image.Image:
    src = Image.open(SRC).convert("RGBA")
    # Crop to square if needed (use the smaller dimension as square)
    w, h = src.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    src = src.crop((left, top, left + side, top + side))
    return src.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    icon_512 = generate_final(PNG_SIZE)
    png_path = os.path.join(OUT_DIR, "icon.png")
    icon_512.save(png_path, "PNG")
    print(f"Saved {png_path} ({PNG_SIZE}x{PNG_SIZE})")

    ico_images = [generate_final(s) for s in ICO_SIZES]
    ico_path = os.path.join(OUT_DIR, "icon.ico")
    ico_images[0].save(
        ico_path, format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=ico_images[1:],
    )
    print(f"Saved {ico_path} (multi-res ICO)")

    tray = generate_final(TRAY_SIZE)
    tray_path = os.path.join(OUT_DIR, "tray.png")
    tray.save(tray_path, "PNG")
    print(f"Saved {tray_path} ({TRAY_SIZE}x{TRAY_SIZE})")


if __name__ == "__main__":
    main()
