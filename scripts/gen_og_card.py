#!/usr/bin/env python3
"""Generate the social sharing card (og:image): static/img/og-card.png.

PURPOSE
    Produces the 1200x630 image referenced by the og:image / twitter:image tags
    in website/templates/_base.html. It is the preview shown when a page of the
    site is shared on social networks, in a chat, or quoted by an LLM.

WHEN TO REGENERATE IT
    One-off script, NOT called at runtime nor at build time: the PNG it produces
    is committed to the repository and served as-is as a static asset. Run this
    script again only when the visual must change (logo, text, colors), then
    commit the new static/img/og-card.png.

HOW IT WORKS
    1. Rasterize the project SVG logo (static/img/logo.svg) to a bitmap
       through resvg.
    2. Compose on a white 1200x630 background: logo on the left, mono text on
       the right.
    3. Write the optimized PNG to static/img/og-card.png.

WHY resvg (and not cairosvg)
    The logo contains <mask> elements (the white holes of the pins) and a
    mix-blend-mode:multiply (the overlap of the two pins). cairosvg rasterizes
    those layers at the viewBox resolution (16px) then upscales them -> blurry
    holes and ignored blend. resvg (a Rust engine) rasterizes them at device
    resolution: crisp holes and a correct multiply overlap, faithful to the
    browser rendering.

DEPENDENCIES / RUNNING
    Pillow (already in the project) + resvg-py. resvg-py is only a generation
    tool, not a runtime dependency: it is injected on the fly through uv so as
    not to pollute pyproject.toml.

        uv run --with resvg-py python scripts/gen_og_card.py
"""

import io
from pathlib import Path

import resvg_py
from PIL import Image, ImageDraw, ImageFont

# Paths: ROOT = repository root (this script lives in scripts/).
ROOT = Path(__file__).resolve().parent.parent
LOGO_SVG = ROOT / "static" / "img" / "logo.svg"
OUT_PNG = ROOT / "static" / "img" / "og-card.png"

# Dimensions imposed by the Open Graph standard (1.91:1 ratio).
W, H = 1200, 630
WHITE = (255, 255, 255)  # card background
DARK = (24, 24, 33)  # main text
GRAY = (110, 110, 125)  # secondary text (unused for now)

# Monospace fonts (consistent with the site's font-mono type).
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
MONO_R = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


def main() -> None:
    # 1. Rasterize the logo. svg_to_bytes returns a PNG (a list of bytes);
    #    we read it back in memory with Pillow. The SVG viewBox is a 16x16
    #    square, so width = height to keep the pins' ratio.
    logo_h = logo_w = 300
    png_bytes = resvg_py.svg_to_bytes(svg_path=str(LOGO_SVG), width=logo_w, height=logo_h)
    logo = Image.open(io.BytesIO(bytes(png_bytes))).convert("RGBA")

    # 2. Composition. White canvas, then paste the logo on the left, centered
    #    vertically. The 3rd argument (logo) acts as the alpha mask -> keeps
    #    the transparency around the pins.
    img = Image.new("RGB", (W, H), WHITE)
    img.paste(logo, (90, (H - logo_h) // 2), logo)
    d = ImageDraw.Draw(img)

    # 3. Text block right of the logo (x = left margin + logo width + gutter).
    #    y coordinates tuned to center the block on the logo height.
    x = 90 + logo_w + 70
    d.text((x, 212), "atp2osm", font=ImageFont.truetype(MONO_B, 92), fill=DARK)
    d.text((x, 330), "Enrichir OpenStreetMap", font=ImageFont.truetype(MONO_B, 40), fill=DARK)
    d.text((x, 378), "avec AllThePlaces", font=ImageFont.truetype(MONO_B, 40), fill=DARK)

    # 4. Write the final PNG (optimize = more aggressive lossless recompression).
    img.save(OUT_PNG, "PNG", optimize=True)
    print(f"written {OUT_PNG} {img.size}")


if __name__ == "__main__":
    main()
