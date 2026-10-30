#!/usr/bin/env python3
"""Generates the Verbatim icons from icon.svg.

- icon{16,32,48,128}.png: the app icon, a violet circle with two white
  closing quote marks. Used on the extensions page and the store. The
  favicon in index.html and the logo in popup.html and content.js are the
  same drawing inline, so change their fill too when the color changes.
- toolbar{16,32}.png: the browser toolbar button. No tile, just the two
  quote marks, solid, drawn edge to edge, in one violet (#7A5CFF) that
  reads on beige, white, grey and black toolbars alike, so nothing has
  to follow the theme. The brand #6B4EFF was too dark
  on Arc's grey; this is a step lighter. Solid, because a 2px outline on
  marks this small left a 1px pinhole at 16px.

Everything is drawn at 8x and downsampled so the edges stay clean at 16px.
Requires Pillow and rsvg-convert (build time only).
Run: python3 extension/icons/generate_icons.py [out_dir]
"""
import io
import os
import subprocess
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SS = 8
# The quote marks alone, cropped to their own box (from icon.svg).
QUOTES = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="32 31 64 64"><g fill="#fff">
<circle cx="47" cy="56" r="13"/><path d="M60 56c0 17-8 29-22 34l-4-7c8-4 12-10 13-18z"/>
<circle cx="81" cy="56" r="13"/><path d="M94 56c0 17-8 29-22 34l-4-7c8-4 12-10 13-18z"/>
</g></svg>"""


def svg_png(svg, px):
    out = subprocess.run(["rsvg-convert", "-w", str(px), "-h", str(px)],
                         input=svg.encode(), capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(out)).convert("RGBA")


TOOLBAR_VIOLET = (0x7A, 0x5C, 0xFF)


def toolbar(size, rgb=TOOLBAR_VIOLET):
    s = size * SS
    img = Image.new("RGBA", (s, s), rgb + (0,))
    img.putalpha(svg_png(QUOTES, s).getchannel("A"))
    return img.resize((size, size), Image.LANCZOS)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else HERE
    os.makedirs(out, exist_ok=True)
    app = open(os.path.join(HERE, "icon.svg")).read()
    for size in (16, 32, 48, 128):
        svg_png(app, size * SS).resize((size, size), Image.LANCZOS) \
            .save(os.path.join(out, f"icon{size}.png"))
    for size in (16, 32):
        toolbar(size).save(os.path.join(out, f"toolbar{size}.png"))


if __name__ == "__main__":
    main()
