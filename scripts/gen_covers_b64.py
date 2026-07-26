"""
Regenerate lambdas/common/covers.py from the xomify logo asset.

Xomtracks is folding into xomify, so the rolling-playlist covers are now
xomify-branded. The two ROLLING covers add a direction-encoding accent frame +
color band so the shared-music playlists read as SLIGHTLY different from a
normal xomify playlist:

  * XOMIFY_COVER_IN_BASE_64   -> "Shared With Me"  (inbound)  -- GREEN band
  * XOMIFY_COVER_OUT_BASE_64  -> "Shared By Me"    (outbound) -- PURPLE band
  * XOMIFY_COVER_BASE_64      -> neutral xomify cover (no band) -- the default
    for on-the-spot playlists.

Each is the xomify logo centered on xomify's near-black background, resized to
a 640x640 square JPEG (<=256KB, the Spotify playlist-cover cap), base64-encoded
and committed -- mirroring the old gen_logo_b64.py so cover upload needs NO
Pillow dependency at Lambda runtime. Run from the xomtracks-backend repo root:

    python3 scripts/gen_covers_b64.py [--preview-dir DIR]

Requires Pillow (dev-only -- NOT a Lambda runtime dependency).
"""

from __future__ import annotations

import argparse
import base64
import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

# xomify's real logo (transparent purple disc + green X). See xomify-frontend
# src/assets/img/logo-x-rework.png.
SRC = "/Users/dom/Code/xomify-frontend/src/assets/img/logo-x-rework.png"
OUT = "lambdas/common/covers.py"

SIZE = 640
LIMIT = 256 * 1024  # Spotify playlist-cover byte cap

# xomify brand palette (from xomify-frontend src/styles.scss):
BG = (10, 10, 20)          # #0a0a14 near-black background
BRAND_GREEN = (27, 220, 111)   # #1bdc6f
BRAND_PURPLE = (156, 10, 191)  # #9c0abf
INK = (10, 10, 20)         # dark text (on the light green band)
WHITE = (255, 255, 255)

FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)

# Per rolling direction: accent color + band label + label ink.
DIRECTIONS = {
    "in": {"color": BRAND_GREEN, "label": "SHARED WITH ME", "ink": INK},
    "out": {"color": BRAND_PURPLE, "label": "SHARED BY ME", "ink": WHITE},
}


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _logo(box: int) -> Image.Image:
    """xomify logo, autocropped to its opaque bounds, scaled to fit `box`."""
    img = Image.open(SRC).convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    scale = box / max(w, h)
    return img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_w: int, start: int) -> ImageFont.FreeTypeFont:
    size = start
    while size > 12:
        font = _load_font(size)
        if draw.textlength(text, font=font) <= max_w:
            return font
        size -= 2
    return _load_font(12)


def _base_canvas() -> Image.Image:
    return Image.new("RGB", (SIZE, SIZE), BG)


def _draw_frame(draw: ImageDraw.ImageDraw, color, width: int) -> None:
    inset = width // 2
    draw.rounded_rectangle(
        [inset, inset, SIZE - 1 - inset, SIZE - 1 - inset],
        radius=36,
        outline=color,
        width=width,
    )


def build_cover(direction: str | None) -> Image.Image:
    """direction in {"in","out"} -> rolling cover (accent frame + color band).
    direction None -> neutral xomify cover (slim purple frame, no band)."""
    canvas = _base_canvas()
    draw = ImageDraw.Draw(canvas)

    if direction is None:
        # Neutral "normal xomify playlist" look: slim purple frame, no band.
        _draw_frame(draw, BRAND_PURPLE, 10)
        logo = _logo(int(SIZE * 0.62))
        canvas.paste(logo, ((SIZE - logo.width) // 2, (SIZE - logo.height) // 2), logo)
        return canvas

    cfg = DIRECTIONS[direction]
    band_h = 104
    frame_w = 22

    # Accent frame in the direction color.
    _draw_frame(draw, cfg["color"], frame_w)

    # Logo centered in the region above the color band.
    logo_area_h = SIZE - band_h - frame_w
    logo = _logo(int(min(SIZE, logo_area_h) * 0.66))
    logo_cx = SIZE // 2
    logo_cy = frame_w + logo_area_h // 2
    canvas.paste(logo, (logo_cx - logo.width // 2, logo_cy - logo.height // 2), logo)

    # Bottom color band (sits inside the frame) with the direction label.
    band_top = SIZE - band_h - frame_w // 2
    draw.rectangle(
        [frame_w // 2, band_top, SIZE - 1 - frame_w // 2, SIZE - 1 - frame_w // 2],
        fill=cfg["color"],
    )
    label = cfg["label"]
    font = _fit_font(draw, label, SIZE - 2 * frame_w - 40, 46)
    tw = draw.textlength(label, font=font)
    ascent, descent = font.getmetrics()
    th = ascent + descent
    band_cy = band_top + (SIZE - frame_w // 2 - band_top) // 2
    draw.text(
        ((SIZE - tw) / 2, band_cy - th / 2),
        label,
        font=font,
        fill=cfg["ink"],
    )
    return canvas


def _encode(img: Image.Image) -> str:
    quality = 92
    while True:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= LIMIT or quality <= 40:
            break
        quality -= 5
    return base64.b64encode(data).decode("ascii")


def _const_block(name: str, b64: str) -> str:
    wrapped = "\n".join('    "%s"' % line for line in textwrap.wrap(b64, 76))
    return f"{name} = (\n{wrapped}\n)\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-dir", default=None, help="also write PNG previews here")
    args = parser.parse_args()

    variants = {
        "XOMIFY_COVER_BASE_64": build_cover(None),
        "XOMIFY_COVER_IN_BASE_64": build_cover("in"),
        "XOMIFY_COVER_OUT_BASE_64": build_cover("out"),
    }

    blocks = []
    for name, img in variants.items():
        b64 = _encode(img)
        blocks.append(_const_block(name, b64))
        print(f"{name}: base64 len={len(b64)}")
        if args.preview_dir:
            img.save(f"{args.preview_dir}/{name}.png")

    module = (
        '"""\n'
        "XOMIFY Playlist Cover Art (committed base64)\n"
        "===========================================\n"
        "Xomtracks folded into xomify -- these are xomify-branded playlist covers,\n"
        "the xomify logo on xomify's near-black background as 640x640 square JPEGs\n"
        "(<=256KB, the Spotify playlist-cover cap), base64-encoded and committed so\n"
        "cover upload needs NO Pillow dependency at Lambda runtime.\n"
        "\n"
        "The two ROLLING covers add a direction-encoding accent frame + color band\n"
        "so the shared-music playlists read as slightly different from a normal\n"
        "xomify playlist:\n"
        "  * IN  (shared with me / inbound)  -> GREEN band\n"
        "  * OUT (shared by me / outbound)   -> PURPLE band\n"
        "  * BASE (neutral, no band)         -> default for on-the-spot playlists.\n"
        "\n"
        "Regenerate via scripts/gen_covers_b64.py.\n"
        '"""\n\n'
        + "\n".join(blocks)
    )
    with open(OUT, "w") as f:
        f.write(module)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
