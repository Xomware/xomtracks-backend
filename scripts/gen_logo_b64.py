"""
Regenerate lambdas/common/logo.py from the Xomtracks icon asset.

Flattens the transparent PNG icon onto solid black, resizes to a 640x640
square JPEG (<=256KB, the Spotify playlist-cover cap), base64-encodes it and
rewrites lambdas/common/logo.py's XOMTRACKS_LOGO_BASE_64 constant. Run from
the xomtracks-backend repo root:

    python scripts/gen_logo_b64.py

Requires Pillow (dev-only -- NOT a Lambda runtime dependency; the cover is
shipped as a committed base64 constant so the shared layer stays lean).
"""

import base64
import io
import textwrap

from PIL import Image

SRC = "/Users/dom/Code/xomtracks-frontend/src/assets/img/xomtracks-icon-source.png"
OUT = "lambdas/common/logo.py"
SIZE = 640
LIMIT = 256 * 1024  # Spotify playlist-cover byte cap


def build_base64() -> str:
    img = Image.open(SRC).convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS)
    bg = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))
    bg.paste(img, (0, 0), img)

    quality = 90
    while True:
        buf = io.BytesIO()
        bg.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= LIMIT or quality <= 40:
            break
        quality -= 5
    return base64.b64encode(data).decode("ascii")


def main() -> None:
    b64 = build_base64()
    wrapped = "\n".join('    "%s"' % line for line in textwrap.wrap(b64, 76))
    module = (
        '"""\n'
        "XOMTRACKS Playlist Cover Logo (committed base64)\n"
        "================================================\n"
        "The Xomtracks icon flattened onto solid black, resized to a 640x640\n"
        "square JPEG (<=256KB, the Spotify playlist-cover cap), base64-encoded\n"
        "and committed -- mirroring xomify's BLACK_LOGO_BASE_64 so cover upload\n"
        "needs NO Pillow dependency at Lambda runtime. Regenerate via\n"
        "scripts/gen_logo_b64.py.\n"
        '"""\n\n'
        "XOMTRACKS_LOGO_BASE_64 = (\n"
        f"{wrapped}\n"
        ")\n"
    )
    with open(OUT, "w") as f:
        f.write(module)
    print(f"wrote {OUT} (base64 len={len(b64)})")


if __name__ == "__main__":
    main()
