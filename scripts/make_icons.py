"""Draw Eridi's app icon (a stylized "E" on the app's green) in every size the desktop app
needs. Run after changing the design; the outputs are committed.

    uv run python scripts/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "icons"
GREEN = (11, 110, 91, 255)  # --accent in the web UI
WHITE = (255, 255, 255, 255)


def draw(size: int) -> Image.Image:
    scale = 4  # draw large, then shrink, for smooth edges
    s = size * scale
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=GREEN)
    left, top, bottom = int(s * 0.30), int(s * 0.24), int(s * 0.76)
    bar = int(s * 0.10)
    d.rectangle((left, top, left + bar, bottom), fill=WHITE)  # the spine
    for y, length in ((top, 0.42), ((top + bottom - bar) // 2, 0.34), (bottom - bar, 0.42)):
        d.rectangle((left, y, left + int(s * length), y + bar), fill=WHITE)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in (("32x32.png", 32), ("128x128.png", 128), ("128x128@2x.png", 256),
                       ("icon.png", 512), ("tray.png", 64)):  # fmt: skip
        draw(size).save(OUT / name)
    big = draw(1024)
    big.save(
        OUT / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    big.save(OUT / "icon.icns")
    print(f"Icons written to {OUT}")


if __name__ == "__main__":
    main()
