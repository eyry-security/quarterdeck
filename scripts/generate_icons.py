#!/usr/bin/env python3
"""Generate the Quarterdeck PWA icon set from one reproducible vector mark."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "quarterdeck" / "static"

BG = (5, 7, 13, 255)
BLUE = (59, 130, 246)
PURPLE = (139, 92, 246)
SUPERSAMPLE = 4
GRADIENT_SIZE = 512 * SUPERSAMPLE

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" role="img" aria-label="Quarterdeck">
  <defs>
    <linearGradient id="qd-mark" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#3b82f6"/>
      <stop offset="1" stop-color="#8b5cf6"/>
    </linearGradient>
  </defs>
  <rect width="256" height="256" rx="56" fill="#05070d"/>
  <path d="M128 58 L62 174 L194 174 Z" fill="url(#qd-mark)"/>
</svg>
"""


def _gradient(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size))
    pixels = image.load()
    span = max(1, 2 * (size - 1))
    for y in range(size):
        for x in range(size):
            t = (x + y) / span
            pixels[x, y] = (
                round(BLUE[0] + (PURPLE[0] - BLUE[0]) * t),
                round(BLUE[1] + (PURPLE[1] - BLUE[1]) * t),
                round(BLUE[2] + (PURPLE[2] - BLUE[2]) * t),
                255,
            )
    return image


_GRADIENT = _gradient(GRADIENT_SIZE)


def render(size: int, glyph_ratio: float, *, rounded: bool) -> Image.Image:
    big = size * SUPERSAMPLE
    scene = Image.new("RGBA", (big, big), BG)

    glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    draw.ellipse(
        [-big * 0.15, -big * 0.2, big * 0.7, big * 0.65],
        fill=(*BLUE, 58),
    )
    draw.ellipse(
        [big * 0.35, big * 0.35, big * 1.15, big * 1.15],
        fill=(*PURPLE, 58),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(big * 0.12))
    scene = Image.alpha_composite(scene, glow)

    cx, cy = big / 2, big / 2
    width = big * glyph_ratio
    height = width * 0.92
    triangle = [
        (cx, cy - height / 2),
        (cx - width / 2, cy + height / 2),
        (cx + width / 2, cy + height / 2),
    ]
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).polygon(triangle, fill=255)
    gradient = _GRADIENT.resize((big, big), Image.LANCZOS)
    scene.paste(gradient, (0, 0), mask)

    if rounded:
        radius = round(big * 0.22)
        edge = Image.new("L", (big, big), 0)
        ImageDraw.Draw(edge).rounded_rectangle(
            [0, 0, big - 1, big - 1], radius=radius, fill=255
        )
        scene.putalpha(edge)

    return scene.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "favicon.svg").write_text(SVG, encoding="utf-8")

    render(192, 0.58, rounded=True).save(OUT / "icon-192.png")
    render(512, 0.58, rounded=True).save(OUT / "icon-512.png")
    render(192, 0.50, rounded=False).save(OUT / "icon-maskable-192.png")
    render(512, 0.50, rounded=False).save(OUT / "icon-maskable-512.png")
    render(180, 0.55, rounded=False).convert("RGB").save(
        OUT / "apple-touch-icon.png"
    )

    for size in (16, 32, 48, 256):
        render(size, 0.66, rounded=True).save(OUT / f"favicon-{size}.png")

    render(256, 0.66, rounded=True).save(
        OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)]
    )


if __name__ == "__main__":
    main()
