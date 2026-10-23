"""The site's icon: a play button landing on a tray (fetched and kept), in the page's blue.

Pages carry it inline as data: URIs, so the open podcast paths need no other path opened."""

import base64
import io
import urllib.parse

from PIL import Image, ImageDraw

BLUE = (11, 92, 173)

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="15" fill="#0b5cad"/>'
    '<path d="M24 13 L45 26 L24 39 Z" fill="#fff" stroke="#fff" stroke-width="3" stroke-linejoin="round"/>'
    '<path d="M16 46 V51 H48 V46" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round" '
    'stroke-linejoin="round"/>'
    "</svg>"
)


def _touch_png(size: int = 180) -> bytes:
    """The same at 180x180 for iOS home screens (which don't take SVG), square: iOS rounds the corners."""
    img = Image.new("RGB", (size, size), BLUE)
    d = ImageDraw.Draw(img)
    s = size / 64
    d.polygon([(24 * s, 13 * s), (45 * s, 26 * s), (24 * s, 39 * s)], fill="white")
    w = round(5 * s)
    d.line([(16 * s, 46 * s), (16 * s, 51 * s), (48 * s, 51 * s), (48 * s, 46 * s)], fill="white", width=w, joint="curve")
    for x, y in ((16, 46), (48, 46)):  # round caps
        d.ellipse([x * s - w / 2, y * s - w / 2, x * s + w / 2, y * s + w / 2], fill="white")
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


SVG_URI = "data:image/svg+xml," + urllib.parse.quote(SVG)
TOUCH_URI = "data:image/png;base64," + base64.b64encode(_touch_png()).decode()

LINKS = (
    f'<link rel="icon" type="image/svg+xml" href="{SVG_URI}">'
    f'<link rel="apple-touch-icon" href="{TOUCH_URI}">'
    '<meta name="apple-mobile-web-app-title" content="Mediafang"><meta name="theme-color" content="#0b5cad">'
)
