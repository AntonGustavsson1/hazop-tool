#!/usr/bin/env python3
"""One-off generator for packaging/app_icon.ico (2026-08-21, see NOTES.md
"Paketera HAZOP-appen som en installationsfil"). Renders icons/shield.svg
(recolored to the app's own accent blue, #2F5FD0 -- see NOTES.md "en bla
accentfarg ersatter svart") at several sizes via Qt's own SVG renderer,
then packs them into a single multi-resolution .ico via Pillow. Re-run
this if a real ProSa logo becomes available to replace the placeholder --
just point SVG_PATH at a different file.

Run with:
    python dev_scripts/generate_app_icon.py
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path

_HAZOP_DIR = Path(__file__).resolve().parent.parent
if str(_HAZOP_DIR) not in sys.path:
    sys.path.insert(0, str(_HAZOP_DIR))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPainter, QColor
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtCore import QByteArray, QRectF
from PIL import Image

SVG_PATH = _HAZOP_DIR / 'icons' / 'shield.svg'
OUT_PATH = _HAZOP_DIR / 'packaging' / 'app_icon.ico'
ACCENT = '#2F5FD0'
SIZES = [16, 24, 32, 48, 64, 128, 256]


def render_svg_to_png_bytes(svg_path: Path, color: str, size: int) -> bytes:
    from PyQt6.QtGui import QPixmap
    svg_text = svg_path.read_text(encoding='utf-8')
    svg_text = svg_text.replace('stroke="#42474d"', f'stroke="{color}"')
    renderer = QSvgRenderer(QByteArray(svg_text.encode('utf-8')))
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    from PyQt6.QtCore import QBuffer, QIODevice
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buf, 'PNG')
    return bytes(buf.data())


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    images = []
    for size in SIZES:
        png_bytes = render_svg_to_png_bytes(SVG_PATH, ACCENT, size)
        import io
        img = Image.open(io.BytesIO(png_bytes)).convert('RGBA')
        images.append(img)
    OUT_PATH.parent.mkdir(exist_ok=True)
    images[-1].save(
        OUT_PATH, format='ICO',
        sizes=[(im.width, im.height) for im in images],
        append_images=images[:-1])
    print(f"Wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes, sizes={SIZES})")


if __name__ == '__main__':
    main()
