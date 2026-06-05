#!/usr/bin/env python3
"""P&ID viewer module for the HAZOP tool."""

import re
import json
from pathlib import Path

# Suppress Qt SVG parser warnings (font references, path truncations)
# These come from PyMuPDF's SVG output and are harmless display artefacts.
from PyQt6.QtCore import qInstallMessageHandler, QtMsgType

def _qt_msg_handler(mode, context, message):
    if message.startswith('qt.svg:'):
        return   # silently ignore SVG parser warnings
    # Let everything else through to stderr
    import sys
    print(message, file=sys.stderr)

qInstallMessageHandler(_qt_msg_handler)

from PyQt6.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QListWidget, QListWidgetItem, QAbstractItemView,
    QLineEdit, QLabel, QPushButton, QDialogButtonBox,
    QGraphicsView, QGraphicsScene, QGraphicsItem,
    QGraphicsPixmapItem, QGraphicsPathItem, QGraphicsEllipseItem,
    QGraphicsSimpleTextItem, QFrame, QSpinBox, QCheckBox, QGroupBox,
    QSlider, QColorDialog, QFileDialog, QMessageBox, QInputDialog,
    QSizePolicy, QMenu, QTableWidget, QTableWidgetItem, QHeaderView,
    QProgressDialog, QApplication, QGridLayout,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QRectF
from PyQt6.QtGui import (
    QColor, QPen, QBrush, QPainterPath, QPixmap, QImage, QFont,
    QPainter, QPicture,
)

# Optional OpenGL for GPU-accelerated rendering
try:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    HAS_OPENGL = True
except ImportError:
    HAS_OPENGL = False

# Optional SVG vector rendering (preferred — stays sharp at any zoom)
try:
    from PyQt6.QtSvg import QSvgRenderer
    HAS_SVG_RENDERER = True
except ImportError:
    QSvgRenderer = None
    HAS_SVG_RENDERER = False

try:
    import fitz
    HAS_PYMUPDF = True
except Exception:
    fitz = None
    HAS_PYMUPDF = False

# ── Optional OCR engines ──────────────────────────────────────────────────────
try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    pytesseract = None
    HAS_TESSERACT = False

try:
    import easyocr as _easyocr_module
    HAS_EASYOCR = True
except ImportError:
    _easyocr_module = None
    HAS_EASYOCR = False

try:
    from PIL import Image as _PILImage, ImageFilter, ImageEnhance, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

_easyocr_reader_cache = None


def _get_easyocr_reader():
    global _easyocr_reader_cache
    if _easyocr_reader_cache is None and HAS_EASYOCR:
        _easyocr_reader_cache = _easyocr_module.Reader(['en'], gpu=False, verbose=False)
    return _easyocr_reader_cache


def ocr_status() -> dict:
    """Return which OCR engines are available."""
    return {
        'tesseract': HAS_TESSERACT,
        'easyocr':   HAS_EASYOCR,
        'pil':       HAS_PIL,
    }


def _preprocess_for_ocr(pil_image):
    """Multi-strategy preprocessing for P&ID OCR."""
    gray = pil_image.convert('L')
    # Check whether drawing is light-on-dark or dark-on-light
    import statistics
    sample = list(gray.getdata())[::50]
    median_lum = statistics.median(sample)
    if median_lum < 100:
        # Light text on dark background — invert
        gray = ImageOps.invert(gray)
    # Enhance contrast strongly
    gray = ImageEnhance.Contrast(gray).enhance(3.0)
    gray = ImageEnhance.Sharpness(gray).enhance(2.0)
    # Gentle denoise
    gray = gray.filter(ImageFilter.MedianFilter(size=3))
    return gray


def _fix_ocr_common_errors(text: str) -> str:
    """Correct typical OCR misreads in alphanumeric equipment tags."""
    # In the letter prefix part: 0→O, 1→I
    # In the number suffix part: O→0, I→1
    m = re.match(r'^([A-Z0-9]{1,6})-?([0-9A-Z]{1,6})$', text.upper().strip())
    if not m:
        return text.upper().strip()
    prefix, suffix = m.group(1), m.group(2)
    prefix = prefix.replace('0', 'O').replace('1', 'I')
    suffix = suffix.replace('O', '0').replace('I', '1').replace('o', '0')
    return f"{prefix}-{suffix}"


def _ocr_page_tesseract(pil_image, scale: float):
    """Run Tesseract with multiple PSM modes; return list of (text, x_pdf, y_pdf)."""
    if not HAS_TESSERACT:
        return []

    # PSM 11 = sparse text (best for P&IDs with scattered labels)
    # PSM  6 = uniform block (catches denser areas)
    seen: set = set()
    results: list = []

    for psm in (11, 6):
        cfg = (f'--oem 3 --psm {psm} '
               r'-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.')
        try:
            data = pytesseract.image_to_data(
                pil_image, config=cfg,
                output_type=pytesseract.Output.DICT)
        except Exception:
            continue

        n = len(data['text'])
        for i in range(n):
            word = data['text'][i].strip()
            if not word:
                continue
            try:
                conf = int(data['conf'][i])
            except (ValueError, TypeError):
                conf = 0
            if conf < 20:          # low bar — tag filter handles false positives
                continue
            key = (data['left'][i], data['top'][i], word)
            if key in seen:
                continue
            seen.add(key)
            x_pdf = (data['left'][i] + data['width'][i] / 2) / scale
            y_pdf = (data['top'][i] + data['height'][i] / 2) / scale
            results.append((word.upper(), x_pdf, y_pdf))

        # Also try to find tags split across adjacent tokens on the same line
        blocks: dict = {}
        for i in range(n):
            if not data['text'][i].strip():
                continue
            bk = (data['block_num'][i], data['line_num'][i])
            blocks.setdefault(bk, []).append(i)

        for indices in blocks.values():
            tokens = [data['text'][j].strip().upper() for j in indices]
            # Try pairs and triples of consecutive tokens
            for start in range(len(tokens)):
                for length in (2, 3):
                    if start + length > len(tokens):
                        break
                    combined = ''.join(tokens[start:start + length])
                    key = ('combined', combined)
                    if key in seen:
                        continue
                    seen.add(key)
                    j0 = indices[start]
                    j1 = indices[start + length - 1]
                    x_pdf = (data['left'][j0] + data['width'][j1] +
                             data['left'][j1]) / 2 / scale
                    y_pdf = (data['top'][j0] + data['height'][j0] / 2) / scale
                    results.append((combined, x_pdf, y_pdf))

    return results


def _ocr_page_easyocr(pil_image, scale: float):
    """Run EasyOCR on a PIL image; return list of (text, x_pdf, y_pdf)."""
    reader = _get_easyocr_reader()
    if reader is None:
        return []
    try:
        import numpy as np
        img_array = np.array(pil_image.convert('RGB'))
        ocr_results = reader.readtext(img_array, detail=1)
        results = []
        for (bbox, text, conf) in ocr_results:
            if conf < 0.3 or not text.strip():
                continue
            cx = sum(p[0] for p in bbox) / 4 / scale
            cy = sum(p[1] for p in bbox) / 4 / scale
            results.append((text.strip().upper(), cx, cy))
        return results
    except Exception:
        return []


def _ocr_page(fitz_page, scale: float = 3.0, engine: str = 'auto'):
    """Render a PyMuPDF page and OCR it.

    Returns list of (text, x_pdf, y_pdf) tuples and the engine name used.
    """
    if not HAS_PYMUPDF or not HAS_PIL:
        return [], None

    mat = fitz.Matrix(scale, scale)
    pix = fitz_page.get_pixmap(matrix=mat, alpha=False)
    pil_img = _PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
    processed = _preprocess_for_ocr(pil_img)

    if engine == 'auto':
        engine = 'tesseract' if HAS_TESSERACT else ('easyocr' if HAS_EASYOCR else None)

    if engine == 'tesseract':
        return _ocr_page_tesseract(processed, scale), 'tesseract'
    elif engine == 'easyocr':
        return _ocr_page_easyocr(processed, scale), 'easyocr'
    return [], None

COMPONENT_TYPES = {
    'Ventil': ['Fullt öppen (fastnad)', 'Fullt stängd (fastnad)', 'Delvis öppen/stängd',
               'Intern läcka', 'Yttre läcka', 'Felaktig aktivering'],
    'Pump': ['Startar inte', 'Stannar oväntat', 'Reducerat flöde',
             'Backflöde', 'Kavitation', 'Mekaniskt haveri'],
    'Tank / Kärl': ['Överfyllnad', 'Tömning (låg nivå)', 'Övertryck',
                    'Undertryck', 'Yttre läcka', 'Korrosion'],
    'Värmeväxlare': ['Rörläcka (kors-kontam.)', 'Igensättning',
                     'Otillräcklig kylning', 'Överkylning', 'Yttre läcka'],
    'Kompressor': ['Startar inte', 'Stannar oväntat', 'Surging', 'Övertryckning', 'Läcka'],
    'Rörledning': ['Blockering', 'Yttre läcka / brott', 'Korrosion', 'Vibration'],
    'Instrument / Sensor': ['Falskt högt signal', 'Falskt lågt signal',
                             'Signalbortfall', 'Kalibreringsdrift'],
    'Säkerhetsventil (PSV)': ['Öppnar inte vid högt tryck',
                               'Stänger inte (förblir öppen)', 'Öppnar för tidigt'],
    'Övrigt': ['Mekaniskt haveri', 'Yttre läcka', 'Kontaminering', 'Felaktig manuell operation'],
}

CONSEQUENCE_TEMPLATES = [
    'Överfyllnad av {}',
    'Övertryck i {}',
    'Undertryck i {}',
    'Utsläpp / läcka från {}',
    'Inget flöde till {}',
    'För högt flöde till {}',
    'Felaktig temperatur i {}',
    'Kontaminering av {}',
    'Brand / explosion vid {}',
    'Driftstopp för {}',
    'Toxisk exponering vid {}',
    'Miljöutsläpp från {}',
]

# ── Consequence chain (mirrors hazop.py — no circular import) ────────────────
_PID_CHAIN_ITEMS = [
    ('loc',           'LOC — Utsläpp / läcka',            'Intermediär händelse'),
    ('fire',          'Brand',                             'Antändning / explosion'),
    ('flash_fire',    'Flash fire',                        None),
    ('explosion',     'Explosion (VCE / BLEVE)',           None),
    ('toxic',         'Toxisk exponering',                 'Toxisk / miljö'),
    ('environmental', 'Miljöutsläpp',                     None),
    ('personnel',     'Personskador',                      'Personell / tillgång'),
    ('fatality',      'Dödsfall',                          None),
    ('equipment',     'Utrustningsskador',                 None),
    ('production',    'Driftstopp',                        None),
]


def _pid_build_chain_text(base: str, chain: dict) -> str:
    parts = [base.strip()] if base.strip() else []
    for key, label, _ in _PID_CHAIN_ITEMS:
        if chain.get(key):
            short = label.split('(')[0].strip().split(' — ')[-1].strip()
            parts.append(short)
    return ' → '.join(parts)


MODE_NAV         = 0
MODE_NODE        = 1
MODE_CAUSE       = 2
MODE_CONSEQUENCE = 3
MODE_SAFEGUARD   = 4

Z_PAGE      = 0
Z_HIGHLIGHT = 1   # tag highlights between page and connections
Z_CONNECT   = 3
Z_OVERLAY   = 5
Z_TEMP      = 10

# Standalone tag: 1-6 letters, optional separator, 1-5 digits, 0-3 suffix letters
# Examples: PCV-101, FT201A, V-1, ESDV-1001AB
_TAG_RE = re.compile(r'^[A-Z]{1,6}[-./]?\d{1,5}[A-Z]{0,3}$')

# Tag within continuous text (used for full-page text search)
_FULL_TAG_RE = re.compile(r'(?<![A-Z0-9])([A-Z]{1,6})[-./]?(\d{1,5}[A-Z]{0,3})(?![A-Z0-9])')

# Area-prefixed: 20-PCV-101, 10FT201 — extract the tag part
_AREA_TAG_RE = re.compile(r'^\d{1,4}[-/]([A-Z]{1,6})[-./]?(\d{1,5}[A-Z]{0,3})$')

# ── Equipment prefix knowledge base ──────────────────────────────────────────
# Format: prefix → (swedish_display_name, COMPONENT_TYPES key)
KNOWN_PREFIXES = {
    # Ventiler
    'V':    ('Ventil (allmän)',                  'Ventil'),
    'HV':   ('Handventil',                       'Ventil'),
    'MOV':  ('Motorventil',                      'Ventil'),
    'PCV':  ('Tryckreduceringsventil',           'Ventil'),
    'FCV':  ('Flödesreglerventil',               'Ventil'),
    'LCV':  ('Nivåreglerventil',                 'Ventil'),
    'TCV':  ('Temperaturreglerventil',           'Ventil'),
    'AV':   ('Automatisk ventil',                'Ventil'),
    'ON':   ('Avstängningsventil',               'Ventil'),
    'BV':   ('Kulventil',                        'Ventil'),
    'CV':   ('Reglerventil / Backventil',        'Ventil'),
    'SV':   ('Stängningsventil',                 'Ventil'),
    'SDV':  ('Stängningsventil (SDV)',           'Ventil'),
    'BDV':  ('Tryckavsäkringsventil',            'Ventil'),
    'XV':   ('Nödavstängningsventil',            'Ventil'),
    'ESV':  ('Nödavstängningsventil',            'Ventil'),
    'ESDV': ('Nödavstängningsventil (ESDV)',     'Ventil'),
    'NRV':  ('Backventil',                       'Ventil'),
    'ROV':  ('Fjärrstyrd ventil (ROV)',          'Ventil'),
    'IV':   ('Isoleringsventil',                 'Ventil'),
    # Säkerhetsventiler
    'PSV':  ('Säkerhetsventil (PSV)',            'Säkerhetsventil (PSV)'),
    'PRV':  ('Trycksäkringsventil (PRV)',        'Säkerhetsventil (PSV)'),
    'RV':   ('Säkerhetsventil (RV)',             'Säkerhetsventil (PSV)'),
    'SRV':  ('Fjädersäkringsventil',             'Säkerhetsventil (PSV)'),
    'TSV':  ('Temperatursäkringsventil',         'Säkerhetsventil (PSV)'),
    'RD':   ('Sprängskiva (Rupture Disk)',       'Säkerhetsventil (PSV)'),
    # Pumpar
    'P':    ('Pump',                             'Pump'),
    'PP':   ('Pump',                             'Pump'),
    'DP':   ('Doseringspump',                    'Pump'),
    'CP':   ('Centrifugalpump',                  'Pump'),
    'VP':   ('Vakuumpump',                       'Pump'),
    'SP':   ('Skruvpump',                        'Pump'),
    # Kompressorer / fläktar
    'C':    ('Kompressor',                       'Kompressor'),
    'K':    ('Kompressor',                       'Kompressor'),
    'COM':  ('Kompressor',                       'Kompressor'),
    'BL':   ('Blåsmaskin / Fläkt',              'Kompressor'),
    'FN':   ('Fläkt',                            'Kompressor'),
    'EJE':  ('Ejektor',                          'Kompressor'),
    # Tankar och kärl
    'T':    ('Tank',                             'Tank / Kärl'),
    'TK':   ('Tank',                             'Tank / Kärl'),
    'D':    ('Drum / Separator',                 'Tank / Kärl'),
    'S':    ('Separator',                        'Tank / Kärl'),
    'SEP':  ('Separator',                        'Tank / Kärl'),
    'R':    ('Reaktor',                          'Tank / Kärl'),
    'COL':  ('Kolonn',                           'Tank / Kärl'),
    'ACC':  ('Ackumulator',                      'Tank / Kärl'),
    'SK':   ('Skrubber',                         'Tank / Kärl'),
    'KO':   ('Knock-out drum',                   'Tank / Kärl'),
    'FL':   ('Flare-system',                     'Tank / Kärl'),
    # Värmeväxlare
    'E':    ('Värmeväxlare',                     'Värmeväxlare'),
    'HE':   ('Värmeväxlare',                     'Värmeväxlare'),
    'AHE':  ('Luftkylare',                       'Värmeväxlare'),
    'REB':  ('Ångpanna / Reboiler',             'Värmeväxlare'),
    'HX':   ('Värmeväxlare',                     'Värmeväxlare'),
    'CD':   ('Kondensor',                        'Värmeväxlare'),
    'H':    ('Heater / Ugn',                     'Värmeväxlare'),
    # Filter / avskiljare
    'F':    ('Filter',                           'Övrigt'),
    'STR':  ('Sil / Strainer',                  'Övrigt'),
    'Y':    ('Y-sil',                            'Övrigt'),
    'CL':   ('Cyklon',                           'Övrigt'),
    # Instrument – Tryck
    'PI':   ('Tryckmätare (lokal)',              'Instrument / Sensor'),
    'PT':   ('Trycktransmitter',                 'Instrument / Sensor'),
    'PIT':  ('Trycktransm. + indikering',        'Instrument / Sensor'),
    'PIC':  ('Tryckreglering',                   'Instrument / Sensor'),
    'PICA': ('Tryckreglering + larm',            'Instrument / Sensor'),
    'PSH':  ('Högtrycksalarm (PSH)',             'Instrument / Sensor'),
    'PSL':  ('Lågtrycksalarm (PSL)',             'Instrument / Sensor'),
    'PSHH': ('Högtrycksalarm HH',               'Instrument / Sensor'),
    'PSLL': ('Lågtrycksalarm LL',               'Instrument / Sensor'),
    'PDI':  ('Differenstrycksmätare',            'Instrument / Sensor'),
    'PDT':  ('Differenstrycktransm.',            'Instrument / Sensor'),
    'PDIT': ('Differenstrycktransm. + indik.',   'Instrument / Sensor'),
    # Instrument – Flöde
    'FI':   ('Flödesmätare (lokal)',             'Instrument / Sensor'),
    'FT':   ('Flödestransmitter',                'Instrument / Sensor'),
    'FIT':  ('Flödestransm. + indikering',       'Instrument / Sensor'),
    'FIC':  ('Flödesreglering',                  'Instrument / Sensor'),
    'FICA': ('Flödesreglering + larm',           'Instrument / Sensor'),
    'FSH':  ('Högt flödesalarm',                 'Instrument / Sensor'),
    'FSL':  ('Lågt flödesalarm',                 'Instrument / Sensor'),
    'FQ':   ('Flödesmängdsmätare',               'Instrument / Sensor'),
    'FM':   ('Flödesmätare',                     'Instrument / Sensor'),
    # Instrument – Nivå
    'LI':   ('Nivåmätare (lokal)',               'Instrument / Sensor'),
    'LT':   ('Nivåtransmitter',                  'Instrument / Sensor'),
    'LIT':  ('Nivåtransm. + indikering',         'Instrument / Sensor'),
    'LIC':  ('Nivåreglering',                    'Instrument / Sensor'),
    'LICA': ('Nivåreglering + larm',             'Instrument / Sensor'),
    'LSH':  ('Högnivåalarm',                     'Instrument / Sensor'),
    'LSL':  ('Lågnivåalarm',                     'Instrument / Sensor'),
    'LSHH': ('Högnivåalarm HH',                 'Instrument / Sensor'),
    'LSLL': ('Lågnivåalarm LL',                 'Instrument / Sensor'),
    'LG':   ('Nivåglas',                         'Instrument / Sensor'),
    # Instrument – Temperatur
    'TI':   ('Temperaturrgivare (lokal)',        'Instrument / Sensor'),
    'TE':   ('Temperaturelement',                'Instrument / Sensor'),
    'TT':   ('Temperaturtransmitter',            'Instrument / Sensor'),
    'TIT':  ('Temperaturtransm. + indik.',       'Instrument / Sensor'),
    'TIC':  ('Temperaturreglering',              'Instrument / Sensor'),
    'TSH':  ('Högt temperaturlarm',              'Instrument / Sensor'),
    'TSL':  ('Lågt temperaturlarm',              'Instrument / Sensor'),
    # Instrument – Analys
    'AI':   ('Analysinstrument',                 'Instrument / Sensor'),
    'AT':   ('Analystransmitter',                'Instrument / Sensor'),
    'AIC':  ('Analysreglering',                  'Instrument / Sensor'),
    'ASH':  ('Högt analysalarm',                 'Instrument / Sensor'),
    'ASL':  ('Lågt analysalarm',                 'Instrument / Sensor'),
    # Övrigt
    'M':    ('Motor / Drivverk',                 'Övrigt'),
    'AG':   ('Omrörare / Agitator',             'Övrigt'),
    'MX':   ('Blandare',                         'Övrigt'),
    'G':    ('Generator',                        'Övrigt'),
    'TR':   ('Transformator',                    'Övrigt'),
    'BRN':  ('Brännare',                         'Övrigt'),
    'IG':   ('Tändare',                          'Övrigt'),
}


# ══════════════════════════════════════════════════════════════════════════════
# P&ID SYMBOL DATABASE — ISA 5.1 / ISO 10628
# ══════════════════════════════════════════════════════════════════════════════
#
# Each entry: (display_name, component_type_key, shape_classifier_fn)
# shape_classifier_fn receives a feature dict and returns True if this
# symbol type matches.  Features:
#   circles, large_circles, small_circles, lines, curves, rectangles,
#   triangles, total_paths, area_px2, aspect_ratio, filled_paths
#
# ISA 5.1 symbol shapes (simplified for robust detection):
#   Pump          — circle + internal arc/curve (impeller) = curves >= 4
#   Compressor    — circle OR diamond; similar to pump but larger
#   Valve (gate)  — 2 triangles tip-to-tip (hourglass) — triangles >= 2
#   Valve (globe) — circle + lines through centre
#   Valve (ball)  — circle with line (tag prefix V/BV)
#   Control valve — valve shape + circle on top = circles >= 2 AND triangles >= 2
#   Safety valve  — valve + spring/zigzag = triangles >= 2 AND curves >= 1
#   Check valve   — single triangle = triangles == 1
#   Instrument    — single circle, small size
#   Transmitter   — circle, medium size
#   Tank/vessel   — tall/wide rectangle OR circle (pressure vessel)
#   Heat exch.    — rectangle with internal parallel lines
#   Filter        — rectangle with cross-hatching or diamond
#   Agitator      — circle with propeller curves

_SYMBOL_DB = [
    # ── Pumpar ────────────────────────────────────────────────────────────────
    {
        'name': 'Centrifugalpump',
        'comp': 'Pump',
        'desc': 'Cirkel med impeller-båge — vanligaste pumpsymbolen (ISA 5.1)',
        'prefixes': ('P', 'PP', 'CP'),
        'check': lambda f: f['circles'] >= 1 and f['curves'] >= 4,
    },
    {
        'name': 'Positivpump / kolvpump',
        'comp': 'Pump',
        'desc': 'Cirkel med intern linjär rörelse (kolvpump, kugghjulspump)',
        'prefixes': ('P', 'DP', 'SP', 'VP'),
        'check': lambda f: f['circles'] >= 1 and f['lines'] >= 4 and f['curves'] < 4,
    },
    # ── Kompressorer ──────────────────────────────────────────────────────────
    {
        'name': 'Kompressor / blåsmaskin',
        'comp': 'Kompressor',
        'desc': 'Cirkel (liknar pump) eller romb; ofta större än pump',
        'prefixes': ('C', 'K', 'BL', 'COM'),
        'check': lambda f: (f['circles'] >= 1 and f['curves'] >= 4 and f['area'] > 600)
                           or (f['triangles'] >= 4),
    },
    # ── Ventiler — gate/globb/spjäll ─────────────────────────────────────────
    {
        'name': 'Ventil (spindel- / globventil)',
        'comp': 'Ventil',
        'desc': 'Två trianglar spets mot spets (timglas) — ISA 5.1 standardsymbol för ventil',
        'prefixes': ('V', 'HV', 'SV', 'MOV', 'ROV'),
        'check': lambda f: f['triangles'] >= 2 and f['circles'] == 0,
    },
    {
        'name': 'Regulerventil / kontrollventil',
        'comp': 'Ventil',
        'desc': 'Ventilsymbol + cirkel ovanpå (ställdon) — ISA 5.1',
        'prefixes': ('FCV', 'PCV', 'LCV', 'TCV', 'AV'),
        'check': lambda f: f['triangles'] >= 2 and f['circles'] >= 1,
    },
    {
        'name': 'Säkerhetsventil (PSV/PRV)',
        'comp': 'Säkerhetsventil (PSV)',
        'desc': 'Ventilsymbol + fjäder/vinkel (böjd linje) — ISA 5.1',
        'prefixes': ('PSV', 'PRV', 'SRV', 'RV', 'TSV'),
        'check': lambda f: f['triangles'] >= 2 and f['curves'] >= 1,
    },
    {
        'name': 'Backventil (NRV/CV)',
        'comp': 'Ventil',
        'desc': 'En triangel = backventil (flöde tillåts åt ett håll)',
        'prefixes': ('NRV', 'CV', 'BV'),
        'check': lambda f: f['triangles'] == 1 and f['circles'] == 0,
    },
    {
        'name': 'Kulventil / nålventil',
        'comp': 'Ventil',
        'desc': 'Cirkel med linje genom mitten — ISA 5.1 kulventil',
        'prefixes': ('BV', 'IV', 'ON'),
        'check': lambda f: f['circles'] == 1 and f['lines'] >= 2 and f['triangles'] == 0,
    },
    {
        'name': 'Fjärilsventil / spjäll',
        'comp': 'Ventil',
        'desc': 'Ellips med linje — ISA 5.1 fjärilsventil',
        'prefixes': ('BDV', 'SDV', 'ESDV', 'XV', 'ESV'),
        'check': lambda f: f['circles'] >= 1 and f['lines'] == 1 and f['triangles'] == 0,
    },
    {
        'name': 'Sprängskiva (Rupture Disk)',
        'comp': 'Säkerhetsventil (PSV)',
        'desc': 'Kort bågform utan triangel — ISA 5.1 RD-symbol',
        'prefixes': ('RD',),
        'check': lambda f: f['curves'] >= 1 and f['triangles'] == 0 and f['circles'] == 0
                           and f['lines'] <= 2,
    },
    # ── Instrument / givare ───────────────────────────────────────────────────
    {
        'name': 'Fältinstrument / lokal givare',
        'comp': 'Instrument / Sensor',
        'desc': 'Enkel cirkel utan extra element — ISA 5.1 lokalt instrument',
        'prefixes': ('PI', 'TI', 'FI', 'LI', 'AI', 'PDI'),
        'check': lambda f: f['circles'] == 1 and f['rectangles'] == 0
                           and f['triangles'] == 0 and f['area'] < 500,
    },
    {
        'name': 'Transmitter / fältgivare',
        'comp': 'Instrument / Sensor',
        'desc': 'Cirkel (medelstor) — ISA 5.1 transmitter (PT, FT, LT, TT…)',
        'prefixes': ('PT', 'FT', 'LT', 'TT', 'AT', 'PIT', 'FIT', 'LIT', 'TIT'),
        'check': lambda f: f['circles'] == 1 and f['rectangles'] == 0
                           and f['triangles'] == 0 and 200 < f['area'] < 2000,
    },
    {
        'name': 'DCS / datoriserad funktion',
        'comp': 'Instrument / Sensor',
        'desc': 'Kvadrat/rektangel — ISA 5.1 shared-display eller beräknad funktion',
        'prefixes': ('FIC', 'PIC', 'LIC', 'TIC', 'AIC'),
        'check': lambda f: f['rectangles'] >= 1 and f['circles'] == 0,
    },
    {
        'name': 'Differenstryckmätare',
        'comp': 'Instrument / Sensor',
        'desc': 'Cirkel + extra linjer — ISA 5.1 differenstryckgivare',
        'prefixes': ('PDI', 'PDT', 'PDIT'),
        'check': lambda f: f['circles'] == 1 and f['lines'] >= 2,
    },
    # ── Tankar och kärl ───────────────────────────────────────────────────────
    {
        'name': 'Tryckkärl / separator',
        'comp': 'Tank / Kärl',
        'desc': 'Vertikal oval eller cylinder — ISO 10628 tryckkärl',
        'prefixes': ('V', 'D', 'SEP', 'S', 'KO'),
        'check': lambda f: f['circles'] >= 1 and f['area'] > 2000,
    },
    {
        'name': 'Öppen tank / behållare',
        'comp': 'Tank / Kärl',
        'desc': 'Rektangel med öppen topp — öppen tank/silo',
        'prefixes': ('T', 'TK'),
        'check': lambda f: f['rectangles'] >= 1 and f['area'] > 1500 and f['lines'] >= 3,
    },
    {
        'name': 'Kolonn / torn',
        'comp': 'Tank / Kärl',
        'desc': 'Hög smal cylinder/rektangel — destillations-/absorptionskolonn',
        'prefixes': ('COL', 'R'),
        'check': lambda f: f['rectangles'] >= 1 and f['aspect'] < 0.5 and f['area'] > 2000,
    },
    # ── Värmeväxlare ──────────────────────────────────────────────────────────
    {
        'name': 'Värmeväxlare (shell & tube)',
        'comp': 'Värmeväxlare',
        'desc': 'Rektangel med inre horisontella linjer — ISO 10628',
        'prefixes': ('E', 'HE', 'HX'),
        'check': lambda f: f['rectangles'] >= 1 and f['lines'] >= 4,
    },
    {
        'name': 'Luftkylare (fin-fan)',
        'comp': 'Värmeväxlare',
        'desc': 'Rektangel med fläktsymbol (triangel) — ISO 10628',
        'prefixes': ('AHE', 'ACC'),
        'check': lambda f: f['rectangles'] >= 1 and f['triangles'] >= 1,
    },
    {
        'name': 'Kondensor / reboiler',
        'comp': 'Värmeväxlare',
        'desc': 'Rektangel med våglinjer eller böjda linjer',
        'prefixes': ('CD', 'REB'),
        'check': lambda f: f['rectangles'] >= 1 and f['curves'] >= 2,
    },
    # ── Filter / avskiljare ───────────────────────────────────────────────────
    {
        'name': 'Filter / sil (Y-strainer)',
        'comp': 'Övrigt',
        'desc': 'Romb/hexagon med inre linjer — filter/sil-symbol',
        'prefixes': ('F', 'FL', 'STR', 'Y'),
        'check': lambda f: f['triangles'] >= 3 or (f['lines'] >= 4 and f['circles'] == 0
                                                     and f['rectangles'] == 0),
    },
    # ── Omrörare / agitator ───────────────────────────────────────────────────
    {
        'name': 'Omrörare / agitator',
        'comp': 'Övrigt',
        'desc': 'Cirkel med propeller-kurvor — ISO 10628 agitator',
        'prefixes': ('AG', 'MX'),
        'check': lambda f: f['circles'] >= 1 and f['curves'] >= 6,
    },
]

# Prefix → component type (for tag-based override)
_PREFIX_TO_COMP = {}
for _e in _SYMBOL_DB:
    for _pfx in _e.get('prefixes', ()):
        _PREFIX_TO_COMP[_pfx] = _e['comp']


def _extract_shape_features(drawings: list, clip_area: float) -> dict:
    """Compute shape features from PyMuPDF page.get_drawings() output."""
    circles     = 0
    lines       = 0
    curves      = 0
    rectangles  = 0
    triangles   = 0
    filled      = 0
    total_area  = 0.0

    for d in drawings:
        items   = d.get('items', [])
        drect   = d.get('rect')
        is_fill = bool(d.get('fill'))

        if is_fill:
            filled += 1

        if drect:
            w, h = drect[2] - drect[0], drect[3] - drect[1]
            total_area += w * h

        # Count item types
        n_lines = sum(1 for it in items if it[0] == 'l')
        n_curves = sum(1 for it in items if it[0] in ('c', 'qu'))
        n_rects = sum(1 for it in items if it[0] == 're')

        lines     += n_lines
        curves    += n_curves
        rectangles += n_rects

        # Circle heuristic: 3+ curves + square-ish bounding box
        if drect and n_curves >= 3:
            w, h = drect[2] - drect[0], drect[3] - drect[1]
            if w > 0 and h > 0 and abs(w - h) / max(w, h) < 0.3:
                circles += 1

        # Triangle heuristic: exactly 3 lines closed
        if n_lines == 3 and n_curves == 0 and n_rects == 0:
            triangles += 1

    # Aspect ratio of entire clip
    from math import sqrt
    side = sqrt(max(clip_area, 1.0))
    aspect = 1.0  # can't reliably compute without clip rect here

    return {
        'circles':     circles,
        'lines':       lines,
        'curves':      curves,
        'rectangles':  rectangles,
        'triangles':   triangles,
        'filled':      filled,
        'total_paths': len(drawings),
        'area':        total_area,
        'aspect':      aspect,
    }


def classify_pid_symbol(page, fitz_rect, tag: str = '') -> tuple:
    """Classify the P&ID symbol inside fitz_rect.

    Returns (comp_type: str, symbol_name: str, confidence: str)
    e.g.  ('Ventil', 'Regulerventil / kontrollventil', 'high')
    """
    # 1. Tag-prefix override (most reliable)
    if tag:
        prefix = re.match(r'^([A-Z]+)', tag.upper())
        if prefix:
            pfx = prefix.group(1)
            # Direct KNOWN_PREFIXES lookup
            if pfx in KNOWN_PREFIXES:
                comp = KNOWN_PREFIXES[pfx][1]
                name = KNOWN_PREFIXES[pfx][0]
                return comp, name, 'high'
            if pfx in _PREFIX_TO_COMP:
                comp = _PREFIX_TO_COMP[pfx]
                return comp, pfx, 'medium'

    # 2. Vector shape analysis
    if not HAS_PYMUPDF:
        return '', '', 'none'
    try:
        drawings = page.get_drawings(clip=fitz_rect)
    except Exception:
        return '', '', 'none'

    if not drawings:
        return '', '', 'none'

    clip_area = (fitz_rect.width * fitz_rect.height)
    feats = _extract_shape_features(drawings, clip_area)

    # Score each symbol rule
    best_name  = ''
    best_comp  = ''
    best_score = 0

    for entry in _SYMBOL_DB:
        try:
            if entry['check'](feats):
                # Simple scoring: prefer entries whose prefixes match tag
                score = 1
                if tag:
                    pfx = re.match(r'^([A-Z]+)', tag.upper())
                    if pfx and pfx.group(1) in entry.get('prefixes', ()):
                        score = 3
                if score > best_score:
                    best_score = score
                    best_name  = entry['name']
                    best_comp  = entry['comp']
        except Exception:
            continue

    confidence = 'medium' if best_score >= 3 else ('low' if best_comp else 'none')
    return best_comp, best_name, confidence


def _clean_for_tag(text: str) -> str:
    """Strip OCR artefacts — keep only characters that can appear in P&ID tags.

    '###HV#####'  →  'HV'
    '##PSV-101##' →  'PSV-101'
    'V - 101'     →  'V-101'
    """
    # Uppercase first
    text = text.upper()
    # Remove anything that can't be part of a tag (only A-Z, 0-9, -, ., /)
    cleaned = re.sub(r'[^A-Z0-9\-./]', ' ', text)
    # Collapse multiple spaces and strip
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def _collapse_spaces(text: str) -> str:
    """Remove spaces between individual letters/digits — fixes OCR spacing.

    'P C V - 1 0 1'  →  'PCV-101'
    'F T 2 0 1 A'    →  'FT201A'   (then _parse_tag normalises to 'FT-201A')
    Multi-word text is left alone (spaces between real words are kept).
    """
    # Only collapse if every token is 1-2 chars (= spaced-out tag, not words)
    tokens = text.strip().split()
    if not tokens:
        return text
    if all(len(t) <= 2 for t in tokens):
        return ''.join(tokens)
    # Also try just removing spaces around dashes and dots
    collapsed = re.sub(r'\s*[-./]\s*', '-', text)
    collapsed = re.sub(r'(?<=[A-Z0-9])\s+(?=[A-Z0-9])', '', collapsed)
    return collapsed


def _pick_best_tag(text: str) -> str:
    """Return the best equipment-tag match from arbitrary text, or ''."""
    if not text:
        return ''
    text = text.strip().upper()

    for candidate in _tag_candidates(text):
        matches = _FULL_TAG_RE.findall(candidate)
        if matches:
            return f"{matches[0][0]}-{matches[0][1]}"
        tag, _ = _parse_tag(candidate)
        if tag:
            return tag
    return ''


def _tag_candidates(text: str) -> list:
    """Return a prioritised list of text variants to try when parsing a tag."""
    candidates = [text]
    # 1. Strip OCR artefacts (###HV### → HV)
    cleaned = _clean_for_tag(text)
    if cleaned and cleaned != text:
        candidates.append(cleaned)
    # 2. Collapse spaces (P C V - 1 0 1 → PCV-101)
    collapsed = _collapse_spaces(text)
    if collapsed and collapsed not in candidates:
        candidates.append(collapsed)
    # 3. Collapse spaces on already-cleaned text
    if cleaned:
        cc = _collapse_spaces(cleaned)
        if cc and cc not in candidates:
            candidates.append(cc)
    return candidates


def _extract_prefix(tag: str) -> str:
    """Extract the letter prefix from an equipment tag like 'PCV-101' → 'PCV'."""
    m = re.match(r'^([A-Z]+)', tag)
    return m.group(1) if m else tag


def _words_from_native(fitz_page):
    """Extract (text, cx, cy) from a PDF page using PyMuPDF word list."""
    words = fitz_page.get_text("words")
    return [(w[4].strip().upper(), (w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
            for w in words if w[4].strip()]


def _tags_from_full_text(fitz_page, page_num: int) -> list:
    """Search the full text of a page with regex; returns [(tag, prefix, cx, cy)]."""
    # Use 'rawdict' to get character-level positions for precise x,y
    full_text = fitz_page.get_text("text")
    results = []
    for m in _FULL_TAG_RE.finditer(full_text):
        raw = m.group(0)
        tag, prefix = _parse_tag(raw)
        if tag and prefix:
            results.append((tag, prefix, 0.0, 0.0))  # positions approximated below
    return results


def _parse_tag(text: str):
    """Normalise an equipment tag string.

    Handles:
      - PCV-101, FT-201A, V-1, ESDV-1001AB   (with dash)
      - PCV101, FT201A, V1                     (no dash)
      - 20-PCV-101, 10/FT201                   (area prefix — stripped)
      - PCV.101                                 (dot separator)

    Returns (normalised_tag, prefix) or (None, None).
    """
    text = text.strip().upper()
    if not text:
        return None, None

    # Strip area prefix like "20-" or "10/"
    am = _AREA_TAG_RE.match(text)
    if am:
        text = f"{am.group(1)}-{am.group(2)}"

    # Already well-formed
    if _TAG_RE.match(text):
        # Normalise separator to dash
        norm = re.sub(r'[./]', '-', text)
        # Remove doubled dashes
        norm = re.sub(r'-+', '-', norm)
        # Ensure dash between letters and digits
        m2 = re.match(r'^([A-Z]{1,6})-(\d{1,5}[A-Z]{0,3})$', norm)
        if not m2:
            m2 = re.match(r'^([A-Z]{1,6})(\d{1,5}[A-Z]{0,3})$', norm)
            if m2:
                norm = f"{m2.group(1)}-{m2.group(2)}"
        return norm, _extract_prefix(norm)

    # No-dash: PCV101, FT201A
    m = re.match(r'^([A-Z]{1,6})(\d{1,5}[A-Z]{0,3})$', text)
    if m:
        tag = f"{m.group(1)}-{m.group(2)}"
        return tag, m.group(1)

    return None, None


def scan_pdf_for_equipment(pdf_doc, use_ocr: bool = False,
                           ocr_engine: str = 'auto',
                           progress_callback=None) -> dict:
    """Scan all pages of a PDF for equipment tags.

    Strategy per page:
      1. Full-text regex search (catches tags in paragraphs / annotations).
      2. Word-by-word matching (standalone tags with precise positions).
      3. If use_ocr=True: always run OCR and merge results.
         OCR finds tags that are part of raster graphics or vector-only layers.

    Returns:
        {prefix: {'tags': [str], 'pages': {tag: int}, 'positions': {tag: (x,y)},
                  'ocr_pages': set_of_page_nums},
         '_meta': {...}}
    """
    if not HAS_PYMUPDF or pdf_doc is None:
        return {}

    result: dict = {}
    ocr_engine_used = None

    def _add(tag, prefix, page_num, cx, cy, from_ocr=False):
        if prefix not in result:
            result[prefix] = {'tags': [], 'pages': {}, 'positions': {}, 'ocr_pages': set()}
        if tag not in result[prefix]['tags']:
            result[prefix]['tags'].append(tag)
        # First-seen wins for page assignment
        result[prefix]['pages'].setdefault(tag, page_num)
        result[prefix]['positions'].setdefault(tag, (cx, cy))
        if from_ocr:
            result[prefix]['ocr_pages'].add(page_num)

    for page_num in range(pdf_doc.page_count):
        page = pdf_doc.load_page(page_num)
        if progress_callback:
            progress_callback(
                page_num, pdf_doc.page_count,
                f"Sida {page_num + 1}/{pdf_doc.page_count} — nativ text…")

        # ── Pass 1: full-text regex ───────────────────────────────────────────
        full_text = page.get_text("text")
        for m in _FULL_TAG_RE.finditer(full_text):
            raw = m.group(0)
            tag, prefix = _parse_tag(raw)
            if tag and prefix:
                _add(tag, prefix, page_num, 0.0, 0.0, from_ocr=False)

        # ── Pass 2: word-by-word (gives precise x,y positions) ───────────────
        native_words = _words_from_native(page)
        tags_from_native = 0
        for text, cx, cy in native_words:
            tag, prefix = _parse_tag(text)
            if tag and prefix:
                # Overwrite position with precise coords
                if prefix in result and tag in result[prefix]['tags']:
                    result[prefix]['positions'][tag] = (cx, cy)
                else:
                    _add(tag, prefix, page_num, cx, cy, from_ocr=False)
                tags_from_native += 1

        # ── Pass 3: OCR ───────────────────────────────────────────────────────
        # Always run when requested — OCR finds tags that live in vector graphics
        if use_ocr:
            if progress_callback:
                progress_callback(
                    page_num, pdf_doc.page_count,
                    f"Sida {page_num + 1}/{pdf_doc.page_count} — OCR (skala 4×)…")

            # Use 4× scale for better small-text recognition
            ocr_words, engine_name = _ocr_page(page, scale=4.0, engine=ocr_engine)
            if engine_name:
                ocr_engine_used = engine_name

            native_tag_set = {
                result[p]['tags'][i]
                for p in result
                for i in range(len(result[p].get('tags', [])))
                if result[p]['pages'].get(result[p]['tags'][i]) == page_num
            }

            for raw_text, cx, cy in (ocr_words or []):
                corrected = _fix_ocr_common_errors(raw_text)
                for candidate in (corrected, raw_text.upper()):
                    tag, prefix = _parse_tag(candidate)
                    if tag and prefix and tag not in native_tag_set:
                        _add(tag, prefix, page_num, cx, cy, from_ocr=True)
                        native_tag_set.add(tag)
                        break

    for prefix in result:
        result[prefix]['tags'].sort(key=lambda t: (t[:re.search(r'\d', t).start()],
                                                    int(re.search(r'\d+', t).group()))
                                    if re.search(r'\d', t) else (t, 0))

    result['_meta'] = {
        'ocr_used':   any(result[p].get('ocr_pages') for p in result if not p.startswith('_')),
        'ocr_engine': ocr_engine_used,
        'total_tags': sum(len(result[p]['tags']) for p in result if not p.startswith('_')),
    }
    return result


class PDFVectorItem(QGraphicsItem):
    """Renders a PDF page as pure vector — crisp at any zoom.

    Performance strategy (layered):
    1. SVG is parsed ONCE into a QPicture at init (record draw commands).
    2. Each paint call just replays the QPicture — far cheaper than re-parsing SVG.
    3. DeviceCoordinateCache: Qt caches the QPicture replay as a screen-res pixmap.
       Pan  → copy cached pixmap (near-zero cost).
       Zoom → replay QPicture at new resolution (much faster than SVG parse).
    """

    def __init__(self, svg_bytes: bytes):
        super().__init__()
        renderer = QSvgRenderer(svg_bytes)
        vb = renderer.viewBoxF()
        self._rect = vb if (vb.isValid() and vb.width() > 0) \
                     else QRectF(0, 0,
                                 renderer.defaultSize().width(),
                                 renderer.defaultSize().height())

        # Pre-record SVG draw commands into QPicture (parse once, replay many)
        self._picture = QPicture()
        p = QPainter(self._picture)
        renderer.render(p, self._rect)
        p.end()
        # Keep renderer only for fallback; picture is what we actually paint
        del renderer

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption)
        # Qt caches QPicture replay at screen resolution:
        # panning reuses cache; zooming replays QPicture (fast)
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)

    def boundingRect(self):
        return self._rect

    def paint(self, painter, option, widget=None):
        if option and option.exposedRect.isValid():
            painter.setClipRect(option.exposedRect)
        painter.drawPicture(QPointF(0, 0), self._picture)

    def page_width(self):
        return self._rect.width()

    def page_height(self):
        return self._rect.height()


def find_tag_near_point(pdf_doc, page_num, x_pdf, y_pdf, radius=50):
    if pdf_doc is None or not HAS_PYMUPDF:
        return ''
    try:
        page = pdf_doc.load_page(page_num)
        rect = fitz.Rect(x_pdf - radius, y_pdf - radius,
                         x_pdf + radius, y_pdf + radius)
        words = page.get_text("words", clip=rect)
        if not words:
            return ''

        def dist(w):
            cx = (w[0] + w[2]) / 2
            cy = (w[1] + w[3]) / 2
            return ((cx - x_pdf) ** 2 + (cy - y_pdf) ** 2) ** 0.5

        words_sorted = sorted(words, key=dist)
        for w in words_sorted[:12]:
            text = w[4].strip()
            if _TAG_RE.match(text):
                return text
        return words_sorted[0][4].strip()
    except Exception:
        return ''


class EquipmentScanDialog(QDialog):
    """Two-tab dialog: grouped prefix view + individual editable tag table."""

    # Column indices for the tag table
    _C_CHK  = 0  # checkbox
    _C_TAG  = 1  # original tag (read-only)
    _C_EDIT = 2  # corrected tag (editable)
    _C_PFX  = 3  # derived prefix (read-only, updates on edit)
    _C_PAGE = 4  # page number
    _C_OCR  = 5  # OCR flag
    _C_TYPE = 6  # equipment type combo
    _C_DESC = 7  # free description

    _TYPE_ITEMS = [''] + sorted(COMPONENT_TYPES.keys()) + ['Rörledning', 'Övrigt / Okänd']

    def __init__(self, scan_result: dict, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Utrustningsskanning — P&ID")
        self.setMinimumSize(1000, 640)
        self.resize(1120, 720)

        self._meta       = scan_result.pop('_meta', {})
        self.scan_result = scan_result   # {prefix: {tags, pages, positions, ocr_pages}}

        outer = QVBoxLayout(self)

        # ── Header ────────────────────────────────────────────────────────────
        n_tags   = sum(len(v['tags']) for v in scan_result.values())
        n_groups = len(scan_result)
        ocr_eng  = self._meta.get('ocr_engine', '')
        ocr_used = self._meta.get('ocr_used', False)
        ocr_info = f"&nbsp;|&nbsp; 🔬 OCR: <b>{ocr_eng}</b>" if ocr_used else ""
        hdr = QLabel(
            f"Hittade <b>{n_tags} taggar</b> i <b>{n_groups} prefix-grupper</b>.{ocr_info}")
        hdr.setTextFormat(Qt.TextFormat.RichText)
        hdr.setStyleSheet("padding:5px; background:#e8f4fd; border:1px solid #bee3f8; "
                          "border-radius:4px;")
        outer.addWidget(hdr)

        # ── OCR status bar ────────────────────────────────────────────────────
        st = ocr_status()
        ocr_bar = QHBoxLayout()
        for label, avail, active in [
            ("pytesseract", st['tesseract'], ocr_eng == 'tesseract'),
            ("easyocr",     st['easyocr'],   ocr_eng == 'easyocr'),
        ]:
            icon   = '✅' if avail else '❌'
            suffix = ' (aktiv)' if avail and active else ''
            lbl    = QLabel(f"{icon} {label}{suffix}")
            lbl.setStyleSheet(f"color:{'#1a7a40' if avail else '#aaa'}; font-size:11px;")
            ocr_bar.addWidget(lbl)
        if not st['tesseract'] and not st['easyocr']:
            warn = QLabel("⚠️  Ingen OCR —  pip install pytesseract  eller  pip install easyocr")
            warn.setStyleSheet("color:#c0392b; font-size:11px;")
            ocr_bar.addWidget(warn)
        ocr_bar.addStretch()
        outer.addLayout(ocr_bar)

        # ── Tabs ──────────────────────────────────────────────────────────────
        self._tabs = QTabWidget()
        outer.addWidget(self._tabs)

        self._tabs.addTab(self._build_prefix_tab(),  "📂  Prefix-grupper")
        self._tabs.addTab(self._build_tag_tab(),     "🏷️  Alla taggar (redigera)")

        # ── Bottom buttons ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        save_btn   = QPushButton("💾 Spara typval")
        save_btn.setToolTip("Sparar prefix→typ-mappningar till DB")
        save_btn.clicked.connect(self._save_types)

        create_btn = QPushButton("🏭 Skapa noder (valda taggar)")
        create_btn.setToolTip("Skapar HAZOP-noder för ikryssade taggar i fliken 'Alla taggar'")
        create_btn.clicked.connect(self._create_nodes_from_tag_table)

        close_btn  = QPushButton("Stäng")
        close_btn.clicked.connect(self.accept)

        btn_row.addWidget(save_btn)
        btn_row.addWidget(create_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

        self._populate_prefix_table()
        self._populate_tag_table()

    # ── Tab builders ──────────────────────────────────────────────────────────

    def _build_prefix_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)

        frow = QHBoxLayout()
        frow.addWidget(QLabel("Filtrera:"))
        self._pfx_filter = QLineEdit()
        self._pfx_filter.setPlaceholderText("Sök prefix eller namn…")
        self._pfx_filter.textChanged.connect(self._apply_prefix_filter)
        frow.addWidget(self._pfx_filter)
        frow.addStretch()
        self._hide_unknown_btn = QPushButton("Dölj okända")
        self._hide_unknown_btn.setCheckable(True)
        self._hide_unknown_btn.toggled.connect(self._apply_prefix_filter)
        frow.addWidget(self._hide_unknown_btn)
        layout.addLayout(frow)

        self._pfx_table = QTableWidget(0, 5)
        self._pfx_table.setHorizontalHeaderLabels(
            ['Prefix', 'Antal', 'Exempeltaggar', 'Föreslagen/sparad typ', 'Bekräftad typ'])
        h = self._pfx_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self._pfx_table.setColumnWidth(0, 75)
        self._pfx_table.setColumnWidth(1, 50)
        self._pfx_table.setColumnWidth(3, 210)
        self._pfx_table.setColumnWidth(4, 200)
        self._pfx_table.verticalHeader().setVisible(False)
        self._pfx_table.setAlternatingRowColors(True)
        self._pfx_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._pfx_table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:4px;}")
        self._pfx_table.currentCellChanged.connect(self._pfx_show_tags)
        layout.addWidget(self._pfx_table)

        layout.addWidget(QLabel("Taggar i markerad grupp:"))
        self._pfx_tag_list = QListWidget()
        self._pfx_tag_list.setMaximumHeight(80)
        layout.addWidget(self._pfx_tag_list)
        return w

    def _build_tag_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(4, 4, 4, 4)

        note = QLabel(
            "Redigera taggar direkt (t.ex. rätta OCR-fel). "
            "Kryssa i de taggar du vill inkludera när du skapar noder.")
        note.setStyleSheet("color:#555; font-size:11px; padding:2px;")
        layout.addWidget(note)

        # Filter + select-all row
        frow = QHBoxLayout()
        frow.addWidget(QLabel("Filtrera:"))
        self._tag_filter = QLineEdit()
        self._tag_filter.setPlaceholderText("Sök tagg, typ eller sida…")
        self._tag_filter.textChanged.connect(self._apply_tag_filter)
        frow.addWidget(self._tag_filter)
        frow.addStretch()

        sel_all_btn  = QPushButton("Välj alla")
        desel_btn    = QPushButton("Avmarkera alla")
        ocr_only_btn = QPushButton("Visa OCR-taggar")
        ocr_only_btn.setCheckable(True)
        sel_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        desel_btn.clicked.connect(lambda: self._set_all_checked(False))
        ocr_only_btn.toggled.connect(self._apply_tag_filter)
        self._ocr_only_btn = ocr_only_btn
        for b in [sel_all_btn, desel_btn, ocr_only_btn]:
            frow.addWidget(b)
        layout.addLayout(frow)

        # Tag table
        self._tag_table = QTableWidget(0, 8)
        self._tag_table.setHorizontalHeaderLabels([
            '✓', 'Original tagg', 'Korrigerad tagg', 'Prefix',
            'Sida', 'OCR', 'Utrustningstyp', 'Beskrivning'])
        h = self._tag_table.horizontalHeader()
        h.setSectionResizeMode(self._C_CHK,  QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(self._C_TAG,  QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(self._C_EDIT, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(self._C_PFX,  QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(self._C_PAGE, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(self._C_OCR,  QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(self._C_TYPE, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(self._C_DESC, QHeaderView.ResizeMode.Stretch)
        self._tag_table.setColumnWidth(self._C_CHK,  30)
        self._tag_table.setColumnWidth(self._C_TAG,  100)
        self._tag_table.setColumnWidth(self._C_EDIT, 110)
        self._tag_table.setColumnWidth(self._C_PFX,  60)
        self._tag_table.setColumnWidth(self._C_PAGE, 50)
        self._tag_table.setColumnWidth(self._C_OCR,  40)
        self._tag_table.setColumnWidth(self._C_TYPE, 185)
        self._tag_table.verticalHeader().setVisible(False)
        self._tag_table.setAlternatingRowColors(True)
        self._tag_table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:4px;}")
        # Update prefix when edited tag changes
        self._tag_table.cellChanged.connect(self._on_tag_cell_changed)
        layout.addWidget(self._tag_table)

        # Row count label
        self._tag_count_lbl = QLabel("")
        self._tag_count_lbl.setStyleSheet("color:#555; font-size:11px;")
        layout.addWidget(self._tag_count_lbl)
        return w

    # ── Populate ──────────────────────────────────────────────────────────────

    def _populate_prefix_table(self):
        self._pfx_table.setRowCount(0)
        for prefix in sorted(self.scan_result.keys()):
            data  = self.scan_result[prefix]
            known = KNOWN_PREFIXES.get(prefix)
            saved = (self.db.get_equipment_type(prefix)
                     if hasattr(self.db, 'get_equipment_type') else None)

            r = self._pfx_table.rowCount()
            self._pfx_table.insertRow(r)

            pfx_item = QTableWidgetItem(prefix)
            pfx_item.setData(Qt.ItemDataRole.UserRole, prefix)
            pfx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            pfx_item.setFlags(pfx_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            pfx_item.setBackground(QBrush(QColor('#e8f8e8' if known else '#fff8e8')))
            self._pfx_table.setItem(r, 0, pfx_item)

            cnt = QTableWidgetItem(str(len(data['tags'])))
            cnt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            cnt.setFlags(cnt.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._pfx_table.setItem(r, 1, cnt)

            ex = ', '.join(data['tags'][:6])
            if len(data['tags']) > 6:
                ex += f" … (+{len(data['tags'])-6})"
            self._pfx_table.setItem(r, 2, QTableWidgetItem(ex))

            if saved:
                si = QTableWidgetItem(f"✅ {saved}")
                si.setForeground(QBrush(QColor('#1a7a40')))
            elif known:
                si = QTableWidgetItem(f"💡 {known[0]}")
                si.setForeground(QBrush(QColor('#1F4E79')))
            else:
                si = QTableWidgetItem("— Okänd —")
                si.setForeground(QBrush(QColor('#888')))
            si.setFlags(si.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._pfx_table.setItem(r, 3, si)

            combo = QComboBox()
            for t in self._TYPE_ITEMS:
                combo.addItem(t)
            target = saved or (known[1] if known else '')
            if target:
                idx = combo.findText(target)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            # Propagate combo choice to tag table
            combo.currentTextChanged.connect(
                lambda typ, pfx=prefix: self._propagate_type_to_tags(pfx, typ))
            self._pfx_table.setCellWidget(r, 4, combo)
            self._pfx_table.setRowHeight(r, 28)

        self._apply_prefix_filter()

    def _populate_tag_table(self):
        self._tag_table.blockSignals(True)
        self._tag_table.setRowCount(0)

        # Collect all tags with metadata
        all_tags = []
        for prefix, data in self.scan_result.items():
            known = KNOWN_PREFIXES.get(prefix)
            saved = (self.db.get_equipment_type(prefix)
                     if hasattr(self.db, 'get_equipment_type') else None)
            suggested_type = saved or (known[1] if known else '')
            ocr_pages = data.get('ocr_pages', set())
            for tag in data['tags']:
                page   = data['pages'].get(tag, 0)
                is_ocr = page in ocr_pages
                all_tags.append((tag, prefix, page, is_ocr, suggested_type))

        all_tags.sort(key=lambda x: (x[0]))  # sort by tag name

        for tag, prefix, page, is_ocr, sug_type in all_tags:
            r = self._tag_table.rowCount()
            self._tag_table.insertRow(r)

            # Checkbox
            chk = QTableWidgetItem()
            chk.setCheckState(Qt.CheckState.Checked)
            chk.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            self._tag_table.setItem(r, self._C_CHK, chk)

            # Original tag (read-only reference)
            orig = QTableWidgetItem(tag)
            orig.setFlags(orig.flags() & ~Qt.ItemFlag.ItemIsEditable)
            orig.setForeground(QBrush(QColor('#666')))
            self._tag_table.setItem(r, self._C_TAG, orig)

            # Editable corrected tag
            edit = QTableWidgetItem(tag)
            if is_ocr:
                edit.setBackground(QBrush(QColor('#fff3cd')))
                edit.setToolTip("Identifierad via OCR — kontrollera att taggen är korrekt")
            self._tag_table.setItem(r, self._C_EDIT, edit)

            # Prefix (read-only, derived)
            pfx_item = QTableWidgetItem(prefix)
            pfx_item.setFlags(pfx_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            pfx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._tag_table.setItem(r, self._C_PFX, pfx_item)

            # Page
            pg_item = QTableWidgetItem(str(page + 1))
            pg_item.setFlags(pg_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            pg_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._tag_table.setItem(r, self._C_PAGE, pg_item)

            # OCR indicator
            ocr_item = QTableWidgetItem('🔬' if is_ocr else '')
            ocr_item.setFlags(ocr_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            ocr_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            ocr_item.setToolTip("Hittad via OCR" if is_ocr else "Hittad via PDF-textlager")
            self._tag_table.setItem(r, self._C_OCR, ocr_item)

            # Type combo
            combo = QComboBox()
            for t in self._TYPE_ITEMS:
                combo.addItem(t)
            if sug_type:
                idx = combo.findText(sug_type)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            self._tag_table.setCellWidget(r, self._C_TYPE, combo)

            # Description (editable free text)
            self._tag_table.setItem(r, self._C_DESC, QTableWidgetItem(''))

            self._tag_table.setRowHeight(r, 26)

        self._tag_table.blockSignals(False)
        self._update_tag_count()
        self._apply_tag_filter()

    # ── Signals / updates ─────────────────────────────────────────────────────

    def _on_tag_cell_changed(self, row, col):
        if col != self._C_EDIT:
            return
        new_tag = (self._tag_table.item(row, self._C_EDIT).text().strip().upper()
                   if self._tag_table.item(row, self._C_EDIT) else '')
        # Derive new prefix
        new_prefix = _extract_prefix(new_tag) if new_tag else ''
        pfx_item = self._tag_table.item(row, self._C_PFX)
        if pfx_item:
            self._tag_table.blockSignals(True)
            pfx_item.setText(new_prefix)
            self._tag_table.blockSignals(False)
        # Suggest type from new prefix if combo is empty
        combo = self._tag_table.cellWidget(row, self._C_TYPE)
        if combo and not combo.currentText() and new_prefix:
            known = KNOWN_PREFIXES.get(new_prefix)
            if known:
                idx = combo.findText(known[1])
                if idx >= 0:
                    combo.setCurrentIndex(idx)

    def _propagate_type_to_tags(self, prefix: str, typ: str):
        """When a type is set in the prefix table, update matching rows in tag table."""
        if not typ:
            return
        for r in range(self._tag_table.rowCount()):
            pfx_item = self._tag_table.item(r, self._C_PFX)
            if pfx_item and pfx_item.text() == prefix:
                combo = self._tag_table.cellWidget(r, self._C_TYPE)
                if combo:
                    idx = combo.findText(typ)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)

    # ── Filters ───────────────────────────────────────────────────────────────

    def _apply_prefix_filter(self):
        text     = self._pfx_filter.text().lower()
        hide_unk = self._hide_unknown_btn.isChecked()
        for r in range(self._pfx_table.rowCount()):
            pfx = (self._pfx_table.item(r, 0).text().lower()
                   if self._pfx_table.item(r, 0) else '')
            sug = (self._pfx_table.item(r, 3).text().lower()
                   if self._pfx_table.item(r, 3) else '')
            is_unknown = '—' in (self._pfx_table.item(r, 3).text()
                                 if self._pfx_table.item(r, 3) else '')
            hidden = (text and text not in pfx and text not in sug) or \
                     (hide_unk and is_unknown)
            self._pfx_table.setRowHidden(r, hidden)

    def _apply_tag_filter(self):
        text     = self._tag_filter.text().lower()
        ocr_only = self._ocr_only_btn.isChecked()
        for r in range(self._tag_table.rowCount()):
            tag_txt  = (self._tag_table.item(r, self._C_EDIT).text().lower()
                        if self._tag_table.item(r, self._C_EDIT) else '')
            type_w   = self._tag_table.cellWidget(r, self._C_TYPE)
            type_txt = type_w.currentText().lower() if type_w else ''
            pg_txt   = (self._tag_table.item(r, self._C_PAGE).text()
                        if self._tag_table.item(r, self._C_PAGE) else '')
            ocr_item = self._tag_table.item(r, self._C_OCR)
            is_ocr   = bool(ocr_item and ocr_item.text())
            hidden = (text and text not in tag_txt and text not in type_txt
                      and text not in pg_txt) or (ocr_only and not is_ocr)
            self._tag_table.setRowHidden(r, hidden)
        self._update_tag_count()

    def _update_tag_count(self):
        visible = sum(1 for r in range(self._tag_table.rowCount())
                      if not self._tag_table.isRowHidden(r))
        checked = sum(1 for r in range(self._tag_table.rowCount())
                      if not self._tag_table.isRowHidden(r) and
                      self._tag_table.item(r, self._C_CHK) and
                      self._tag_table.item(r, self._C_CHK).checkState() == Qt.CheckState.Checked)
        self._tag_count_lbl.setText(f"{visible} taggar visas  |  {checked} valda")

    def _set_all_checked(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for r in range(self._tag_table.rowCount()):
            if not self._tag_table.isRowHidden(r):
                item = self._tag_table.item(r, self._C_CHK)
                if item:
                    item.setCheckState(state)
        self._update_tag_count()

    def _pfx_show_tags(self, current_row, *_):
        self._pfx_tag_list.clear()
        if current_row < 0:
            return
        item = self._pfx_table.item(current_row, 0)
        if not item:
            return
        prefix = item.data(Qt.ItemDataRole.UserRole)
        data   = self.scan_result.get(prefix, {})
        for tag in data.get('tags', []):
            page = data['pages'].get(tag, 0)
            self._pfx_tag_list.addItem(f"{tag}   (sida {page + 1})")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _save_types(self):
        if not hasattr(self.db, 'save_equipment_type'):
            return
        saved = 0
        for r in range(self._pfx_table.rowCount()):
            pfx_item = self._pfx_table.item(r, 0)
            combo    = self._pfx_table.cellWidget(r, 4)
            if pfx_item and combo and combo.currentText():
                known = KNOWN_PREFIXES.get(pfx_item.text(), ('', ''))
                self.db.save_equipment_type(
                    pfx_item.text(), combo.currentText(),
                    known[0] if known else combo.currentText())
                saved += 1
        QMessageBox.information(self, "Sparat",
                                f"{saved} prefix-mappningar sparade till databasen.")

    def _create_nodes_from_tag_table(self):
        rows_to_create = []
        for r in range(self._tag_table.rowCount()):
            chk = self._tag_table.item(r, self._C_CHK)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                edit_item = self._tag_table.item(r, self._C_EDIT)
                pg_item   = self._tag_table.item(r, self._C_PAGE)
                combo     = self._tag_table.cellWidget(r, self._C_TYPE)
                desc_item = self._tag_table.item(r, self._C_DESC)

                tag       = edit_item.text().strip() if edit_item else ''
                page      = int(pg_item.text()) - 1 if pg_item else 0
                eq_type   = combo.currentText() if combo else ''
                desc      = desc_item.text().strip() if desc_item else ''
                if tag:
                    rows_to_create.append((tag, page, eq_type, desc))

        if not rows_to_create:
            QMessageBox.information(self, "Ingen vald",
                "Kryssa i minst en tagg i fliken 'Alla taggar'.")
            return

        created = 0
        for tag, page, eq_type, desc in rows_to_create:
            node_id = self.db.add_node_with_markup(
                tag, [], {'color': '#FF8C00', 'width': 2, 'alpha': 180}, page)
            pid_ref = f"Sida {page + 1}"
            self.db.conn.execute(
                "UPDATE nodes SET name=?, pid_ref=?, description=? WHERE id=?",
                (tag, pid_ref,
                 f"{eq_type}{': ' + desc if desc else ''}",
                 node_id))
            self.db.conn.commit()
            created += 1

        QMessageBox.information(self, "Klart",
            f"{created} noder skapade. Uppdatera trädet i HAZOP-vyn.")


class ComponentPickerDialog(QDialog):
    def __init__(self, parent=None, suggested_tag='',
                 component_types=None, mode_freqs=None, preselect_type=''):
        super().__init__(parent)
        self.setWindowTitle("Välj komponent och felmod")
        self.setMinimumWidth(460)
        self.selected_type   = ''
        self.selected_modes  = []
        self.selected_tag    = ''
        self.selected_freqs  = {}
        self._comp_types     = component_types or COMPONENT_TYPES
        self._mode_freqs     = mode_freqs or {}
        self._preselect_type = preselect_type  # auto-detected symbol type

        layout = QVBoxLayout(self)

        # Show detected symbol type as a hint (if available)
        if preselect_type:
            hint = QLabel(f"🔍 Identifierad symboltyp: {preselect_type}")
            hint.setStyleSheet(
                "background:#e8f4fd; border:1px solid #bee3f8; border-radius:3px;"
                "padding:4px 8px; color:#1F4E79; font-weight:bold; font-size:11px;")
            layout.addWidget(hint)
        form   = QFormLayout()

        self.tag_edit = QLineEdit(suggested_tag)
        self.tag_edit.setPlaceholderText("t.ex. V-01  (lästes från PDF, ändra vid behov)")
        form.addRow("Komponent-ID:", self.tag_edit)

        self.type_combo = QComboBox()
        self.type_combo.addItems(list(self._comp_types.keys()))
        self.type_combo.currentTextChanged.connect(self._update_modes)
        form.addRow("Komponenttyp:", self.type_combo)
        # Pre-select detected type from symbol classifier
        if self._preselect_type:
            idx = self.type_combo.findText(self._preselect_type)
            if idx >= 0:
                self.type_combo.setCurrentIndex(idx)
        layout.addLayout(form)

        layout.addWidget(QLabel("Felmod(er) — flerval möjligt:"))
        self.mode_list = QListWidget()
        self.mode_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.mode_list.setMinimumHeight(160)
        layout.addWidget(self.mode_list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_modes(self.type_combo.currentText())

    def _update_modes(self, type_name):
        self.mode_list.clear()
        freqs = self._mode_freqs.get(type_name, {})
        for mode in self._comp_types.get(type_name, []):
            freq = freqs.get(mode)
            if freq is not None:
                # Show frequency info alongside mode description
                f_lbl = self._freq_label(freq)
                display = f"{mode}  [{f_lbl}]"
            else:
                display = mode
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, mode)   # original desc without freq
            if freq is not None:
                item.setToolTip(f"Frekvens: {freq:.4g} händelser/år  →  {f_lbl}")
            self.mode_list.addItem(item)

    @staticmethod
    def _freq_label(freq):
        """Format a frequency value as a readable F-level string."""
        if freq is None or freq <= 0:
            return "—"
        from math import floor, log10
        # Compute F-level (same formula as freq_to_f_level in hazop.py)
        boundaries = [1e-5, 1e-4, 1e-3, 0.01, 0.1, 1.0]
        f_level = len(boundaries) - 1
        for i, b in enumerate(boundaries):
            if freq < b:
                f_level = i - 1
                break
        return f"F={f_level}  ({freq:.3g}/år)"

    def _on_accept(self):
        self.selected_type = self.type_combo.currentText()
        self.selected_tag  = self.tag_edit.text().strip()
        selected = self.mode_list.selectedItems()
        if not selected and self.mode_list.count() > 0:
            self.mode_list.item(0).setSelected(True)
            selected = [self.mode_list.item(0)]
        # Use UserRole (original desc) to strip the freq annotation added in display
        self.selected_modes = [
            item.data(Qt.ItemDataRole.UserRole) or item.text()
            for item in selected
        ]
        # Collect freq_per_year for each selected mode
        freqs = self._mode_freqs.get(self.selected_type, {})
        self.selected_freqs = {mode: freqs.get(mode) for mode in self.selected_modes}
        self.accept()


class TargetPickerDialog(QDialog):
    def __init__(self, parent=None, suggested_tag='', db=None):
        super().__init__(parent)
        self.setWindowTitle("Välj konsekvens")
        self.setMinimumWidth(500)
        self._db            = db
        self.template       = ''
        self.target         = ''
        self.selected_chain = {}
        self.link_to_id     = None   # set if user picks an existing consequence

        layout = QVBoxLayout(self)

        # Link to existing button (when db is available)
        if db is not None:
            link_btn = QPushButton("🔗 Länka till befintlig konsekvens")
            link_btn.setStyleSheet(
                "background:#2c7bb6; color:white; border:none; border-radius:4px; padding:4px 10px;")
            link_btn.clicked.connect(self._pick_existing)
            layout.addWidget(link_btn)
            sep = QLabel("— eller skapa ny konsekvens —")
            sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sep.setStyleSheet("color:#888; font-size:10px;")
            layout.addWidget(sep)

        # ── Template list ─────────────────────────────────────────────────────
        layout.addWidget(QLabel("Konsekvensmall:"))
        self.template_list = QListWidget()
        self.template_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.template_list.setMaximumHeight(130)
        for tpl in CONSEQUENCE_TEMPLATES:
            self.template_list.addItem(QListWidgetItem(tpl))
        self.template_list.setCurrentRow(0)
        self.template_list.currentRowChanged.connect(self._update_preview)
        layout.addWidget(self.template_list)

        form = QFormLayout()
        self.target_edit = QLineEdit(suggested_tag)
        self.target_edit.setPlaceholderText("t.ex. T-101")
        self.target_edit.textChanged.connect(self._update_preview)
        form.addRow("Målobjekt:", self.target_edit)
        layout.addLayout(form)

        # ── Consequence chain ─────────────────────────────────────────────────
        chain_box = QGroupBox("Konsekvenskedja  (valfritt)")
        chain_grid = QGridLayout(chain_box)
        chain_grid.setSpacing(3)
        self._chain_checks: dict = {}
        row_idx, col_idx, last_group = 0, 0, None

        for key, label, group in _PID_CHAIN_ITEMS:
            if group and group != last_group:
                if col_idx > 0:
                    row_idx += 1; col_idx = 0
                hdr = QLabel(group)
                hdr.setStyleSheet(
                    "color:#1F4E79; font-weight:bold; font-size:10px; margin-top:3px;")
                chain_grid.addWidget(hdr, row_idx, 0, 1, 3)
                row_idx += 1; col_idx = 0
                last_group = group
            chk = QCheckBox(label)
            chk.stateChanged.connect(self._update_preview)
            self._chain_checks[key] = chk
            chain_grid.addWidget(chk, row_idx, col_idx)
            col_idx += 1
            if col_idx >= 3:
                col_idx = 0; row_idx += 1

        layout.addWidget(chain_box)

        # ── Full preview ──────────────────────────────────────────────────────
        layout.addWidget(QLabel("Fullständig text:"))
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet(
            "color:#1F4E79; font-weight:bold; padding:4px;"
            "background:#eef4fb; border:1px solid #bee3f8; border-radius:3px;")
        layout.addWidget(self.preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_preview()

    def set_target(self, name):
        self.target_edit.setText(name or '')

    def _current_template(self):
        item = self.template_list.currentItem()
        return item.text() if item else (CONSEQUENCE_TEMPLATES[0] if CONSEQUENCE_TEMPLATES else '{}')

    def _base_text(self):
        tpl  = self._current_template()
        name = self.target_edit.text().strip() or '[okänt objekt]'
        try:
            return tpl.format(name)
        except Exception:
            return tpl.replace('{}', name)

    def _update_preview(self, *_):
        chain = {k: chk.isChecked() for k, chk in self._chain_checks.items()}
        full  = _pid_build_chain_text(self._base_text(), chain)
        self.preview.setText(full or self._base_text())

    def _pick_existing(self):
        if self._db is None:
            return
        dlg = ExistingConsequencePicker(self._db, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_id:
            self.link_to_id = dlg.selected_id
            self.accept()

    def _on_accept(self):
        self.template = self._current_template()
        self.target   = self.target_edit.text().strip()
        self.selected_chain = {k: chk.isChecked()
                               for k, chk in self._chain_checks.items()}
        self.accept()


class ExistingConsequencePicker(QDialog):
    """Pick an existing consequence from the project to link to the current cause."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.selected_id = None
        self.setWindowTitle("Länka till befintlig konsekvens")
        self.setMinimumSize(640, 380)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Välj en befintlig konsekvens att länka den nya orsaken till:"))

        # Search
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filtrera…")
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        # Table
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(['Nod', 'Orsak', 'Konsekvens', 'Risk'])
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 90)
        self._table.setColumnWidth(3, 80)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.doubleClicked.connect(self._accept)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:3px;}")
        layout.addWidget(self._table)

        btns = QHBoxLayout()
        ok_btn = QPushButton("🔗 Länka till markerad")
        ok_btn.clicked.connect(self._accept)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(ok_btn); btns.addStretch(); btns.addWidget(cancel_btn)
        layout.addLayout(btns)

        self._populate()

    def _populate(self):
        self._rows = []   # list of (cons_id, node_name, cause_desc, cons_desc, risk_label, bg)
        try:
            for node in self.db.nodes():
                for cause in self.db.causes(node['id']):
                    for cons in self.db.consequences(cause['id']):
                        self._rows.append((
                            cons['id'],
                            node['name'],
                            cause['description'],
                            cons['description'],
                        ))
        except Exception:
            pass
        self._apply_filter()

    def _apply_filter(self):
        text = self._filter.text().lower()
        self._table.setRowCount(0)
        for cons_id, node, cause, cons_desc in self._rows:
            if text and text not in (node + cause + cons_desc).lower():
                continue
            r = self._table.rowCount()
            self._table.insertRow(r)
            for col, val in enumerate([node, cause, cons_desc, '']):
                item = QTableWidgetItem(val)
                item.setData(Qt.ItemDataRole.UserRole, cons_id)
                self._table.setItem(r, col, item)
            self._table.setRowHeight(r, 24)

    def _accept(self):
        row = self._table.currentRow()
        if row < 0:
            return
        item = self._table.item(row, 0)
        if item:
            self.selected_id = item.data(Qt.ItemDataRole.UserRole)
            self.accept()


class ExistingSafeguardPicker(QDialog):
    """Pick an existing safeguard to link to the current consequence."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.selected_id = None
        self.setWindowTitle("Länka till befintlig safeguard")
        self.setMinimumSize(600, 340)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Välj en befintlig safeguard att länka till denna konsekvens:"))

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filtrera…")
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(['Konsekvens', 'Safeguard', 'RRF'])
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(2, 70)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.doubleClicked.connect(self._accept)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:3px;}")
        layout.addWidget(self._table)

        btns = QHBoxLayout()
        ok_btn = QPushButton("🔗 Länka till markerad")
        ok_btn.clicked.connect(self._accept)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(ok_btn); btns.addStretch(); btns.addWidget(cancel_btn)
        layout.addLayout(btns)

        self._populate()

    def _populate(self):
        self._rows = []
        try:
            for node in self.db.nodes():
                for cause in self.db.causes(node['id']):
                    for cons in self.db.consequences(cause['id']):
                        for sg in self.db.safeguards(cons['id']):
                            self._rows.append((
                                sg['id'],
                                cons['description'],
                                sg['description'],
                                sg['rrf'] or 1,
                            ))
        except Exception:
            pass
        self._apply_filter()

    def _apply_filter(self):
        text = self._filter.text().lower()
        self._table.setRowCount(0)
        for sg_id, cons_desc, sg_desc, rrf in self._rows:
            if text and text not in (cons_desc + sg_desc).lower():
                continue
            r = self._table.rowCount()
            self._table.insertRow(r)
            for col, val in enumerate([cons_desc, sg_desc, f"RRF {rrf}" if rrf > 1 else "—"]):
                item = QTableWidgetItem(val)
                item.setData(Qt.ItemDataRole.UserRole, sg_id)
                self._table.setItem(r, col, item)
            self._table.setRowHeight(r, 24)

    def _accept(self):
        row = self._table.currentRow()
        if row < 0:
            return
        item = self._table.item(row, 0)
        if item:
            self.selected_id = item.data(Qt.ItemDataRole.UserRole)
            self.accept()


class SafeguardPickerDialog(QDialog):
    def __init__(self, parent=None, suggested_tag='', existing_safeguards=None, db=None):
        super().__init__(parent)
        self.setWindowTitle("Markera safeguard / barriär")
        self.setMinimumWidth(420)
        self._db         = db
        self.tag         = ''
        self.description = ''
        self.add_more    = False
        self.link_to_id  = None   # set if user chooses to link an existing safeguard

        layout = QVBoxLayout(self)

        # Link to existing button (shown when db is available)
        if db is not None:
            link_btn = QPushButton("🔗 Länka till befintlig safeguard")
            link_btn.setStyleSheet(
                "background:#2c7bb6; color:white; border:none; border-radius:4px; padding:4px 10px;")
            link_btn.clicked.connect(self._pick_existing)
            layout.addWidget(link_btn)

            sep = QLabel("— eller skapa ny —")
            sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sep.setStyleSheet("color:#888; font-size:10px;")
            layout.addWidget(sep)

        form   = QFormLayout()

        self.tag_edit = QLineEdit(suggested_tag)
        self.tag_edit.setPlaceholderText("t.ex. PSV-101  (lästes från PDF)")
        form.addRow("ID / tag:", self.tag_edit)

        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("t.ex. Säkerhetsventil, Nivålarm LAH-101")
        form.addRow("Beskrivning:", self.desc_edit)
        layout.addLayout(form)

        if existing_safeguards:
            layout.addWidget(QLabel("Snabbval (befintliga safeguards för denna konsekvens):"))
            for sg_text in existing_safeguards[:6]:
                btn = QPushButton(sg_text)
                btn.setFlat(True)
                btn.setStyleSheet("text-align:left; color:#1a7a40; padding:2px;")
                btn.clicked.connect(lambda _, s=sg_text: self.desc_edit.setText(s))
                layout.addWidget(btn)

        # Buttons: OK | + Lägg till ytterligare | Avbryt
        btn_row = QHBoxLayout()

        ok_btn = QPushButton("✓ Spara")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(lambda: self._on_accept(add_more=False))
        btn_row.addWidget(ok_btn)

        add_btn = QPushButton("➕ Spara och lägg till ytterligare")
        add_btn.setToolTip(
            "Sparar denna safeguard och håller läget klart\n"
            "för att lägga till ytterligare safeguard på kartan.")
        add_btn.setStyleSheet(
            "background:#1F4E79; color:white; border:none;"
            "border-radius:4px; padding:4px 10px; font-weight:bold;")
        add_btn.clicked.connect(lambda: self._on_accept(add_more=True))
        btn_row.addWidget(add_btn)

        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

    def _pick_existing(self):
        if self._db is None:
            return
        dlg = ExistingSafeguardPicker(self._db, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_id:
            self.link_to_id = dlg.selected_id
            self.add_more   = False
            self.accept()

    def _on_accept(self, add_more=False):
        self.tag         = self.tag_edit.text().strip()
        self.description = self.desc_edit.text().strip()
        if not self.description:
            self.description = self.tag or 'Safeguard'
        self.add_more = add_more
        self.accept()


class PIDGraphicsView(QGraphicsView):
    node_markup_finished  = pyqtSignal(list, int)
    # Third parameter = extracted tag text from drawn rectangle (may be empty)
    cause_clicked         = pyqtSignal(object, int, str)
    consequence_clicked   = pyqtSignal(object, int, str)
    safeguard_clicked     = pyqtSignal(object, int, str)
    context_action        = pyqtSignal(str, object, int)
    marker_clicked        = pyqtSignal(str, int)

    # Keys for QGraphicsItem.setData / .data
    _DATA_TYPE = 0    # 'cause' | 'consequence' | 'safeguard'
    _DATA_ID   = 1    # database id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        # White background — matches typical P&ID drawing background
        self.setBackgroundBrush(QBrush(QColor(255, 255, 255)))

        # ── GPU-accelerated rendering (OpenGL) ────────────────────────────────
        if HAS_OPENGL:
            gl = QOpenGLWidget()
            self.setViewport(gl)
            # With OpenGL, full-viewport update is required (GPU composites all)
            self.setViewportUpdateMode(
                QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        else:
            # CPU fallback: only repaint changed tiles
            self.setViewportUpdateMode(
                QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)

        # Avoid unnecessary bounding-rect adjustments during pan/zoom
        self.setOptimizationFlag(
            QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True)
        # Don't save/restore painter state per item — slight speedup
        self.setOptimizationFlag(
            QGraphicsView.OptimizationFlag.DontSavePainterState, True)

        self._press_pos         = None  # NAV mode: click vs drag detection
        self._rect_start        = None  # rect-select mode: start scene point
        self._rect_item         = None  # temporary rubber-band QGraphicsRectItem
        self._detected_comp_type = ''   # set by _extract_tag_from_rect after rect draw
        self._detected_sym_name  = ''   # human-readable detected symbol name

        self.mode             = MODE_NAV
        self.pdf_doc          = None
        self.current_page     = 0
        self.page_item        = None
        self.render_scale     = 1.0   # 1:1 for SVG (scene coords = PDF coords)
        self.page_rect_width  = 0.0
        self.page_rect_height = 0.0
        self._use_vector      = HAS_SVG_RENDERER   # prefer vector over raster

        self.draw_points        = []
        self.draw_pen           = QPen(QColor(255, 140, 0), 3)
        self.draw_pen.setCosmetic(True)
        self.draw_brush         = QBrush(QColor(255, 140, 0, 60))
        self.temp_items         = []
        self.rubber_line        = None
        self._pending_path_item = None

        self._placeholder = None
        self._show_placeholder("Öppna en P&ID-fil (PDF) för att börja.")
        self.set_mode(MODE_NAV)

    def _show_placeholder(self, text):
        self._clear_placeholder()
        item = self._scene.addSimpleText(text)
        f = QFont(); f.setPointSize(14)
        item.setFont(f)
        item.setBrush(QBrush(QColor(70, 70, 70)))
        item.setZValue(Z_TEMP)
        self._placeholder = item
        self._scene.setSceneRect(item.boundingRect().adjusted(-40, -40, 40, 40))

    def _clear_placeholder(self):
        if self._placeholder is not None:
            try:
                self._scene.removeItem(self._placeholder)
            except Exception:
                pass
            self._placeholder = None

    def load_pdf(self, path, page=0):
        if not HAS_PYMUPDF:
            self._show_placeholder("Installera PyMuPDF:\n  pip install PyMuPDF")
            return False
        try:
            self.pdf_doc = fitz.open(str(path))
        except Exception as e:
            self._show_placeholder(f"Kunde inte öppna PDF:\n{e}")
            self.pdf_doc = None
            return False
        if self.pdf_doc.page_count == 0:
            self._show_placeholder("PDF saknar sidor.")
            return False
        self.current_page = max(0, min(page, self.pdf_doc.page_count - 1))
        self._render_page()
        return True

    def _render_page(self):
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return
        self._clear_placeholder()
        page = self.pdf_doc.load_page(self.current_page)
        rect = page.rect
        self.page_rect_width  = float(rect.width)
        self.page_rect_height = float(rect.height)

        # Remove previous page item
        if self.page_item is not None:
            try:
                self._scene.removeItem(self.page_item)
            except Exception:
                pass
            self.page_item = None

        if self._use_vector:
            # ── Vector SVG rendering — crisp at any zoom ──────────────────────
            try:
                svg_str   = page.get_svg_image(matrix=fitz.Identity)
                svg_bytes = svg_str.encode('utf-8') if isinstance(svg_str, str) else svg_str
                self.page_item = PDFVectorItem(svg_bytes)
                self.page_item.setZValue(Z_PAGE)
                self._scene.addItem(self.page_item)
                self._scene.setSceneRect(self.page_item.boundingRect())
                # Scene coords = PDF coords (72 DPI points) — no scaling needed
                self.render_scale = 1.0
                return
            except Exception as e:
                # SVG failed — fall through to raster
                self._use_vector = False

        # ── Raster fallback (pixmap) ──────────────────────────────────────────
        raster_scale = 3.0  # 3× for raster fallback quality
        mat  = fitz.Matrix(raster_scale, raster_scale)
        pix  = page.get_pixmap(matrix=mat, alpha=False)
        img  = QImage(pix.samples, pix.width, pix.height,
                      pix.stride, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(img.copy())
        self.page_item = QGraphicsPixmapItem(pixmap)
        self.page_item.setZValue(Z_PAGE)
        self.page_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self.page_item)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self.render_scale = raster_scale

    def page_count(self):
        return self.pdf_doc.page_count if self.pdf_doc else 0

    def goto_page(self, n):
        if self.pdf_doc is None:
            return
        n = max(0, min(n, self.pdf_doc.page_count - 1))
        if n == self.current_page and self.page_item is not None:
            return
        self.current_page = n
        self._cancel_drawing()
        self._render_page()

    def scene_to_pdf(self, point):
        # With SVG rendering: scene coords == PDF coords (render_scale=1.0)
        # With raster fallback: apply the raster scale
        return (point.x() / self.render_scale, point.y() / self.render_scale)

    def pdf_to_scene(self, x, y):
        return QPointF(x * self.render_scale, y * self.render_scale)

    def set_mode(self, mode):
        self.mode = mode
        if mode == MODE_NAV:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif mode == MODE_NODE:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode in (MODE_CAUSE, MODE_CONSEQUENCE, MODE_SAFEGUARD):
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)  # cross = draw a rect
        if mode != MODE_NODE:
            self._cancel_drawing()
        self.setFocus()

    def set_pen_style(self, color, width, alpha):
        c = QColor(color)
        self.draw_pen = QPen(QColor(c.red(), c.green(), c.blue(), alpha), width)
        self.draw_pen.setCosmetic(True)
        self.draw_brush = QBrush(QColor(c.red(), c.green(), c.blue(), max(30, alpha // 4)))

    def _add_draw_point(self, sp):
        self.draw_points.append(sp)
        r = max(4.0, self.draw_pen.widthF() * 0.6)
        dot = self._scene.addEllipse(
            sp.x() - r, sp.y() - r, 2 * r, 2 * r,
            QPen(Qt.PenStyle.NoPen), QBrush(self.draw_pen.color())
        )
        dot.setZValue(Z_TEMP)
        self.temp_items.append(dot)
        if len(self.draw_points) >= 2:
            p0  = self.draw_points[-2]
            seg = self._scene.addLine(p0.x(), p0.y(), sp.x(), sp.y(), self.draw_pen)
            seg.setZValue(Z_TEMP)
            self.temp_items.append(seg)

    def _update_rubber_band(self, sp):
        if not self.draw_points:
            return
        last = self.draw_points[-1]
        pen  = QPen(self.draw_pen); pen.setStyle(Qt.PenStyle.DashLine)
        if self.rubber_line is None:
            self.rubber_line = self._scene.addLine(
                last.x(), last.y(), sp.x(), sp.y(), pen)
            self.rubber_line.setZValue(Z_TEMP)
        else:
            self.rubber_line.setPen(pen)
            self.rubber_line.setLine(last.x(), last.y(), sp.x(), sp.y())

    def _finish_drawing(self):
        if len(self.draw_points) < 2:
            self._cancel_drawing()
            return
        path = QPainterPath()
        path.moveTo(self.draw_points[0])
        for pt in self.draw_points[1:]:
            path.lineTo(pt)
        path.closeSubpath()
        pdf_points = [self.scene_to_pdf(pt) for pt in self.draw_points]
        self._remove_temp_items()

        item = QGraphicsPathItem(path)
        item.setPen(self.draw_pen)
        item.setBrush(self.draw_brush)
        item.setZValue(Z_OVERLAY)
        self._scene.addItem(item)
        self._pending_path_item = item

        self.draw_points = []
        self.node_markup_finished.emit(pdf_points, self.current_page)

    def _cancel_drawing(self):
        self._remove_temp_items()
        self.draw_points = []

    def _remove_temp_items(self):
        for item in self.temp_items:
            try: self._scene.removeItem(item)
            except Exception: pass
        self.temp_items = []
        if self.rubber_line is not None:
            try: self._scene.removeItem(self.rubber_line)
            except Exception: pass
            self.rubber_line = None

    def add_node_overlay(self, node_id, points_pdf, style, label):
        if not points_pdf:
            return
        self._pending_path_item = None
        color = QColor(style.get('color', '#FF8C00'))
        width = int(style.get('width', 3))
        alpha = int(style.get('alpha', 220))
        pen   = QPen(QColor(color.red(), color.green(), color.blue(), alpha), width)
        pen.setCosmetic(True)
        brush = QBrush(QColor(color.red(), color.green(), color.blue(), max(25, alpha // 5)))

        path = QPainterPath()
        first = self.pdf_to_scene(*points_pdf[0])
        path.moveTo(first)
        for x, y in points_pdf[1:]:
            path.lineTo(self.pdf_to_scene(x, y))
        path.closeSubpath()

        item = QGraphicsPathItem(path)
        item.setPen(pen); item.setBrush(brush); item.setZValue(Z_OVERLAY)
        item.setToolTip(label or '')
        self._scene.addItem(item)

        if label:
            cx = sum(p[0] for p in points_pdf) / len(points_pdf)
            cy = sum(p[1] for p in points_pdf) / len(points_pdf)
            center = self.pdf_to_scene(cx, cy)
            txt = QGraphicsSimpleTextItem(label)
            f = QFont(); f.setBold(True); f.setPointSize(11)
            txt.setFont(f)
            txt.setBrush(QBrush(QColor(30, 30, 30)))
            br = txt.boundingRect()
            txt.setPos(center.x() - br.width() / 2, center.y() - br.height() / 2)
            txt.setZValue(Z_OVERLAY + 1)
            self._scene.addItem(txt)

    def add_cause_marker(self, cause_id, x_pdf, y_pdf, comp_type, label, tag=''):
        center = self.pdf_to_scene(x_pdf, y_pdf)
        r = 14.0
        circle = QGraphicsEllipseItem(center.x() - r, center.y() - r, 2 * r, 2 * r)
        circle.setPen(QPen(QColor(160, 0, 0), 2))
        circle.setBrush(QBrush(QColor(231, 76, 60, 200)))
        circle.setZValue(Z_OVERLAY)
        tip = f"{tag + ': ' if tag else ''}{comp_type}" + (f"\n{label}" if label else '')
        tip += "\n🖱 Klicka för att navigera i trädet"
        circle.setToolTip(tip)
        circle.setData(self._DATA_TYPE, 'cause')
        circle.setData(self._DATA_ID,   cause_id)
        circle.setAcceptHoverEvents(True)
        circle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._scene.addItem(circle)

        display = tag if tag else comp_type[:3].upper()
        inner = QGraphicsSimpleTextItem(display)
        f = QFont(); f.setPointSize(7 if len(display) > 4 else 8); f.setBold(True)
        inner.setFont(f)
        inner.setBrush(QBrush(QColor(255, 255, 255)))
        ibr = inner.boundingRect()
        inner.setPos(center.x() - ibr.width() / 2, center.y() - ibr.height() / 2)
        inner.setZValue(Z_OVERLAY + 1)
        self._scene.addItem(inner)

        if label:
            short = label[:30]
            txt = QGraphicsSimpleTextItem(short)
            f2 = QFont(); f2.setPointSize(8)
            txt.setFont(f2)
            txt.setBrush(QBrush(QColor(120, 0, 0)))
            txt.setPos(center.x() + r + 3, center.y() - 8)
            txt.setZValue(Z_OVERLAY + 1)
            self._scene.addItem(txt)

    def add_consequence_marker(self, cons_id, x_pdf, y_pdf, target):
        center = self.pdf_to_scene(x_pdf, y_pdf)
        r = 12.0
        circle = QGraphicsEllipseItem(center.x() - r, center.y() - r, 2 * r, 2 * r)
        circle.setPen(QPen(QColor(180, 100, 0), 2))
        circle.setBrush(QBrush(QColor(243, 156, 18, 190)))
        circle.setZValue(Z_OVERLAY)
        circle.setToolTip((target or '') + "\n🖱 Klicka för att navigera i trädet")
        circle.setData(self._DATA_TYPE, 'consequence')
        circle.setData(self._DATA_ID,   cons_id)
        circle.setAcceptHoverEvents(True)
        circle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._scene.addItem(circle)

        inner = QGraphicsSimpleTextItem("K")
        f = QFont(); f.setPointSize(8); f.setBold(True)
        inner.setFont(f); inner.setBrush(QBrush(QColor(255, 255, 255)))
        ibr = inner.boundingRect()
        inner.setPos(center.x() - ibr.width() / 2, center.y() - ibr.height() / 2)
        inner.setZValue(Z_OVERLAY + 1)
        self._scene.addItem(inner)

    def add_safeguard_marker(self, sg_id, x_pdf, y_pdf, tag, description):
        center = self.pdf_to_scene(x_pdf, y_pdf)
        r = 12.0
        circle = QGraphicsEllipseItem(center.x() - r, center.y() - r, 2 * r, 2 * r)
        circle.setPen(QPen(QColor(20, 120, 20), 2))
        circle.setBrush(QBrush(QColor(39, 174, 96, 200)))
        circle.setZValue(Z_OVERLAY)
        tip = f"{tag + ': ' if tag else ''}{description}\n🖱 Klicka för att navigera i trädet"
        circle.setToolTip(tip)
        circle.setData(self._DATA_TYPE, 'safeguard')
        circle.setData(self._DATA_ID,   sg_id)
        circle.setAcceptHoverEvents(True)
        circle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._scene.addItem(circle)

        display = tag if tag else 'SG'
        inner = QGraphicsSimpleTextItem(display[:4])
        f = QFont(); f.setPointSize(7 if len(display) > 3 else 8); f.setBold(True)
        inner.setFont(f); inner.setBrush(QBrush(QColor(255, 255, 255)))
        ibr = inner.boundingRect()
        inner.setPos(center.x() - ibr.width() / 2, center.y() - ibr.height() / 2)
        inner.setZValue(Z_OVERLAY + 1)
        self._scene.addItem(inner)

        if description:
            txt = QGraphicsSimpleTextItem(description[:30])
            f2 = QFont(); f2.setPointSize(8)
            txt.setFont(f2)
            txt.setBrush(QBrush(QColor(20, 100, 20)))
            txt.setPos(center.x() + r + 3, center.y() - 8)
            txt.setZValue(Z_OVERLAY + 1)
            self._scene.addItem(txt)

    def _extract_tag_from_rect(self, pdf_rect: QRectF) -> tuple:
        """Extract tag text AND classify the P&ID symbol inside the rectangle.

        Returns (tag: str, comp_type: str, symbol_name: str)
        e.g. ('PSV-101', 'Säkerhetsventil (PSV)', 'Säkerhetsventil (PSV/PRV)')
        """
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return '', '', ''
        try:
            page  = self.pdf_doc.load_page(self.current_page)
            frect = fitz.Rect(pdf_rect.x(), pdf_rect.y(),
                               pdf_rect.x() + pdf_rect.width(),
                               pdf_rect.y() + pdf_rect.height())

            # ── 1. Native text extraction ─────────────────────────────────────
            words = page.get_text("words", clip=frect)
            native_text = ' '.join(w[4].strip() for w in words if w[4].strip())
            tag = _pick_best_tag(native_text) or native_text.strip()

            # ── 2. OCR fallback ───────────────────────────────────────────────
            if not tag and HAS_PIL:
                min_dim  = max(pdf_rect.width(), pdf_rect.height(), 10.0)
                scale    = max(4.0, min(16.0, 300.0 / min_dim))
                mat      = fitz.Matrix(scale, scale)
                pix      = page.get_pixmap(matrix=mat, clip=frect, alpha=False)
                pil      = _PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
                pil      = _preprocess_for_ocr(pil)
                ocr_text = ''
                if HAS_TESSERACT:
                    try:
                        cfg = ('--oem 3 --psm 7 '
                               '-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.')
                        ocr_text = pytesseract.image_to_string(pil, config=cfg).strip()
                    except Exception:
                        pass
                if not ocr_text and HAS_EASYOCR:
                    try:
                        import numpy as np
                        reader = _get_easyocr_reader()
                        if reader:
                            results = reader.readtext(np.array(pil))
                            ocr_text = ' '.join(r[1] for r in results if r[2] > 0.3)
                    except Exception:
                        pass
                tag = _pick_best_tag(ocr_text) or ocr_text.strip()

            # ── 3. Symbol shape classification ───────────────────────────────
            comp_type, sym_name, _conf = classify_pid_symbol(page, frect, tag)

            return tag, comp_type, sym_name

        except Exception:
            pass
        return '', '', ''

    def add_tag_highlight(self, bbox: 'fitz.Rect', color: str, tooltip: str = ''):
        """Draw a semi-transparent highlight rectangle at the tag's PDF position."""
        r = QRectF(bbox.x0, bbox.y0, bbox.width, bbox.height)
        pen = QPen(Qt.PenStyle.NoPen)
        brush = QBrush(QColor(color))
        item = self._scene.addRect(r, pen, brush)
        item.setOpacity(0.35)
        item.setZValue(Z_HIGHLIGHT)
        if tooltip:
            item.setToolTip(tooltip)
        return item

    def clear_highlights(self):
        """Remove all tag highlights (Z_HIGHLIGHT items)."""
        for item in list(self._scene.items()):
            if item.zValue() == Z_HIGHLIGHT:
                try: self._scene.removeItem(item)
                except Exception: pass

    def add_connection_line(self, start: QPointF, end: QPointF, color: str, dashed=False):
        pen = QPen(QColor(color), 1.5)
        pen.setCosmetic(True)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        line = self._scene.addLine(start.x(), start.y(), end.x(), end.y(), pen)
        line.setZValue(Z_CONNECT)

    def clear_overlays(self):
        for item in list(self._scene.items()):
            if item is self.page_item or item is self._placeholder:
                continue
            if item.zValue() >= Z_CONNECT:
                try: self._scene.removeItem(item)
                except Exception: pass
        if self._pending_path_item is not None:
            try: self._scene.removeItem(self._pending_path_item)
            except Exception: pass
            self._pending_path_item = None

    def mousePressEvent(self, event):
        if self.mode == MODE_NAV:
            self._press_pos = event.position()
        sp = self.mapToScene(event.position().toPoint())
        if self.mode == MODE_NODE:
            if event.button() == Qt.MouseButton.LeftButton:
                self._add_draw_point(sp); event.accept(); return
            elif event.button() == Qt.MouseButton.RightButton:
                self._cancel_drawing(); event.accept(); return
        elif self.mode in (MODE_CAUSE, MODE_CONSEQUENCE, MODE_SAFEGUARD):
            if event.button() == Qt.MouseButton.LeftButton:
                # Start rubber-band rectangle selection
                self._rect_start = sp
                self._rect_item  = None
                event.accept(); return

        if event.button() == Qt.MouseButton.RightButton and self.mode == MODE_NAV:
            menu = QMenu(self.viewport())
            menu.addAction("⚙️ Orsak",
                           lambda: self.context_action.emit('cause', sp, self.current_page))
            menu.addAction("⚠️ Konsekvens",
                           lambda: self.context_action.emit('consequence', sp, self.current_page))
            menu.addAction("🛡️ Safeguard",
                           lambda: self.context_action.emit('safeguard', sp, self.current_page))
            menu.addSeparator()
            menu.addAction("🔀 Risk Scenario",
                           lambda: self.context_action.emit('risk_scenario', sp, self.current_page))
            menu.addSeparator()
            menu.addAction("✏️ Rita Nodgräns",
                           lambda: self.context_action.emit('node', sp, self.current_page))
            menu.exec(event.globalPosition().toPoint())
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.mode == MODE_NODE and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            self._add_draw_point(sp)
            self._finish_drawing()
            event.accept(); return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        # ── Rect-select release for cause/consequence/safeguard ───────────────
        if (event.button() == Qt.MouseButton.LeftButton and
                self.mode in (MODE_CAUSE, MODE_CONSEQUENCE, MODE_SAFEGUARD) and
                self._rect_start is not None):

            end_sp = self.mapToScene(event.position().toPoint())
            rect   = QRectF(self._rect_start, end_sp).normalized()

            # Remove rubber-band rect
            if self._rect_item is not None:
                try: self._scene.removeItem(self._rect_item)
                except Exception: pass
                self._rect_item = None
            self._rect_start = None

            # Convert to PDF coordinates
            rs = self.render_scale
            pdf_rect = QRectF(rect.x() / rs, rect.y() / rs,
                               rect.width() / rs, rect.height() / rs)

            # Extract tag text + classify symbol from the selected rectangle
            suggested, comp_type, sym_name = self._extract_tag_from_rect(pdf_rect)
            # Store detected type so PIDPanel can pre-select in the dialog
            self._detected_comp_type = comp_type
            self._detected_sym_name  = sym_name

            center = rect.center()
            if self.mode == MODE_CAUSE:
                self.cause_clicked.emit(center, self.current_page, suggested)
            elif self.mode == MODE_CONSEQUENCE:
                self.consequence_clicked.emit(center, self.current_page, suggested)
            elif self.mode == MODE_SAFEGUARD:
                self.safeguard_clicked.emit(center, self.current_page, suggested)
            event.accept()
            return

        # ── NAV mode: click on marker navigates tree ──────────────────────────
        if (self.mode == MODE_NAV and
                event.button() == Qt.MouseButton.LeftButton and
                self._press_pos is not None):
            p  = event.position()
            dx = p.x() - self._press_pos.x()
            dy = p.y() - self._press_pos.y()
            if dx * dx + dy * dy < 25:
                sp = self.mapToScene(p.toPoint())
                for item in self._scene.items(sp):
                    itype = item.data(self._DATA_TYPE)
                    iid   = item.data(self._DATA_ID)
                    if itype in ('cause', 'consequence', 'safeguard') and iid is not None:
                        self.marker_clicked.emit(itype, int(iid))
                        break
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if self.mode == MODE_NODE and self.draw_points:
            self._update_rubber_band(self.mapToScene(event.position().toPoint()))
        elif self.mode in (MODE_CAUSE, MODE_CONSEQUENCE, MODE_SAFEGUARD) \
                and self._rect_start is not None:
            current = self.mapToScene(event.position().toPoint())
            rect = QRectF(self._rect_start, current).normalized()
            if self._rect_item is not None:
                try: self._scene.removeItem(self._rect_item)
                except Exception: pass
            pen = QPen(QColor(0, 100, 220), 1.5)
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            self._rect_item = self._scene.addRect(
                rect, pen, QBrush(QColor(0, 100, 220, 30)))
            self._rect_item.setZValue(Z_TEMP)
            event.accept(); return
        super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        # Smooth zoom: scale by a factor proportional to the wheel delta
        # so trackpad pinch-zoom gives fine-grained control
        delta = event.angleDelta().y()
        if delta == 0:
            event.accept(); return
        # 1.001^delta gives ~1.15× per 120-unit tick (standard wheel notch)
        factor = 1.001 ** delta
        # Clamp to prevent extreme zoom
        cur = self.transform().m11()
        if cur * factor < 0.02:
            factor = 0.02 / cur
        elif cur * factor > 200:
            factor = 200 / cur
        self.scale(factor, factor)
        event.accept()

    def keyPressEvent(self, event):
        if self.mode == MODE_NODE:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._finish_drawing(); event.accept(); return
            elif event.key() == Qt.Key.Key_Escape:
                self._cancel_drawing(); event.accept(); return
        super().keyPressEvent(event)


def _vline():
    f = QFrame()
    f.setFrameShape(QFrame.Shape.VLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


class PIDPanel(QWidget):
    node_created            = pyqtSignal(int)
    cause_created           = pyqtSignal(int)
    consequence_created     = pyqtSignal(int)
    safeguard_created       = pyqtSignal(int)
    risk_scenario_requested = pyqtSignal(int, object, int)  # node_id, scene_pos, page
    marker_navigated        = pyqtSignal(str, int)          # 'cause'|'consequence'|'safeguard', id

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        self._pen_color             = QColor(255, 140, 0)
        self._active_node_id        = None
        self._active_cause_id       = None
        self._active_consequence_id = None
        self._pending_markup_pts    = None
        self._pending_markup_page   = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        bar = QHBoxLayout(); bar.setSpacing(4)

        self.open_btn = QPushButton("📂 Öppna P&ID")
        self.open_btn.clicked.connect(self._open_pdf)
        bar.addWidget(self.open_btn)

        self.scan_btn = QPushButton("🔍 Skanna utrustning")
        self.scan_btn.setToolTip("Identifiera utrustning från PDF och tilldela typer")
        self.scan_btn.clicked.connect(self._scan_equipment)
        self.scan_btn.setEnabled(False)
        bar.addWidget(self.scan_btn)

        bar.addWidget(_vline())

        self.prev_btn = QPushButton("◀")
        self.prev_btn.setFixedWidth(28)
        self.prev_btn.clicked.connect(lambda: self._goto_page(self.viewer.current_page - 1))
        bar.addWidget(self.prev_btn)

        self.page_label = QLabel("—")
        self.page_label.setMinimumWidth(70)
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.addWidget(self.page_label)

        self.next_btn = QPushButton("▶")
        self.next_btn.setFixedWidth(28)
        self.next_btn.clicked.connect(lambda: self._goto_page(self.viewer.current_page + 1))
        bar.addWidget(self.next_btn)

        bar.addWidget(_vline())

        self.mode_buttons = {}
        mode_defs = [
            (MODE_NAV,         "🔍 Navigera"),
            (MODE_NODE,        "✏️ Nodgräns"),
            (MODE_CAUSE,       "⚙️ Orsak"),
            (MODE_CONSEQUENCE, "⚠️ Konsekvens"),
            (MODE_SAFEGUARD,   "🛡️ Safeguard"),
        ]
        for mode, label in mode_defs:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked, m=mode: self._set_mode(m))
            bar.addWidget(btn)
            self.mode_buttons[mode] = btn

        bar.addWidget(_vline())

        self.style_widget = QWidget()
        sl = QHBoxLayout(self.style_widget)
        sl.setContentsMargins(0, 0, 0, 0); sl.setSpacing(4)

        sl.addWidget(QLabel("Tjocklek:"))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 15); self.width_spin.setValue(3)
        self.width_spin.valueChanged.connect(self._update_pen)
        sl.addWidget(self.width_spin)

        sl.addWidget(QLabel("Transparens:"))
        self.alpha_slider = QSlider(Qt.Orientation.Horizontal)
        self.alpha_slider.setRange(20, 255); self.alpha_slider.setValue(220)
        self.alpha_slider.setFixedWidth(80)
        self.alpha_slider.valueChanged.connect(self._update_pen)
        sl.addWidget(self.alpha_slider)

        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(28, 28)
        self.color_btn.clicked.connect(self._pick_color)
        self._refresh_color_btn()
        sl.addWidget(self.color_btn)

        self.create_node_btn = QPushButton("✅ Skapa Nod")
        self.create_node_btn.setEnabled(False)
        self.create_node_btn.clicked.connect(self._create_node_from_markup)
        sl.addWidget(self.create_node_btn)

        self.style_widget.setVisible(False)
        bar.addWidget(self.style_widget)
        bar.addStretch()
        layout.addLayout(bar)

        # ── Scenario guided-mode banner ───────────────────────────────────────
        self._scenario_active = False
        self._scenario_step   = 0   # 1=cause 2=consequence 3+=safeguard

        self._scenario_banner = QFrame()
        self._scenario_banner.setStyleSheet(
            "QFrame{background:#1F4E79; border-radius:4px; padding:2px;}")
        self._scenario_banner.setFixedHeight(46)
        sb_lay = QVBoxLayout(self._scenario_banner)
        sb_lay.setContentsMargins(8, 2, 8, 2)
        sb_lay.setSpacing(2)

        # Top row: step pills
        pill_row = QHBoxLayout(); pill_row.setSpacing(4)
        self._step_pills = []
        _PILL_BASE = "border-radius:3px; padding:1px 6px; font-size:10px; font-weight:bold;"
        for txt in ["1 ⚙️ Orsak", "2 ⚠️ Konsekvens", "3 🛡️ Safeguard"]:
            lbl = QLabel(txt)
            lbl.setStyleSheet(_PILL_BASE + "background:#3a6fa3; color:#aac;")
            pill_row.addWidget(lbl)
            self._step_pills.append(lbl)
            if txt != "3 🛡️ Safeguard":
                pill_row.addWidget(QLabel("→").setStyleSheet and QLabel("→"))
                # (arrow is just cosmetic)
        pill_row.addStretch()

        self._sc_abort_btn = QPushButton("✕ Avbryt")
        self._sc_abort_btn.setFixedHeight(20)
        self._sc_abort_btn.setStyleSheet(
            "background:#c0392b; color:white; border:none; border-radius:3px; padding:0 8px;")
        self._sc_abort_btn.clicked.connect(self._scenario_abort)
        pill_row.addWidget(self._sc_abort_btn)
        sb_lay.addLayout(pill_row)

        # Bottom row: instruction + action buttons
        act_row = QHBoxLayout(); act_row.setSpacing(6)
        self._sc_instr = QLabel("")
        self._sc_instr.setStyleSheet("color:white; font-size:11px;")
        act_row.addWidget(self._sc_instr)
        act_row.addStretch()

        self._sc_add_sg_btn = QPushButton("+ Fler safeguards")
        self._sc_add_sg_btn.setFixedHeight(20)
        self._sc_add_sg_btn.setStyleSheet(
            "background:#27ae60; color:white; border:none; border-radius:3px; padding:0 8px;")
        self._sc_add_sg_btn.setVisible(False)
        self._sc_add_sg_btn.clicked.connect(
            lambda: (self._set_mode(MODE_SAFEGUARD),
                     self._sc_instr.setText("Klicka på nästa safeguard på P&ID:n")))
        act_row.addWidget(self._sc_add_sg_btn)

        self._sc_finish_btn = QPushButton("✓ Slutför")
        self._sc_finish_btn.setFixedHeight(20)
        self._sc_finish_btn.setStyleSheet(
            "background:#2ecc71; color:white; border:none; border-radius:3px; padding:0 8px; font-weight:bold;")
        self._sc_finish_btn.setVisible(False)
        self._sc_finish_btn.clicked.connect(self._scenario_finish)
        act_row.addWidget(self._sc_finish_btn)
        sb_lay.addLayout(act_row)

        self._scenario_banner.setVisible(False)
        layout.addWidget(self._scenario_banner)

        # ── Viewer ────────────────────────────────────────────────────────────
        self.viewer = PIDGraphicsView()
        self.viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.viewer.node_markup_finished.connect(self._on_markup_finished)
        self.viewer.cause_clicked.connect(self._on_cause_click)
        self.viewer.consequence_clicked.connect(self._on_consequence_click)
        self.viewer.safeguard_clicked.connect(self._on_safeguard_click)
        self.viewer.context_action.connect(self._on_context_action)
        self.viewer.marker_clicked.connect(
            lambda t, i: self.marker_navigated.emit(t, i))

        # Connect existing signals for scenario auto-progression
        self.cause_created.connect(self._sc_on_cause)
        self.consequence_created.connect(self._sc_on_consequence)
        self.safeguard_created.connect(self._sc_on_safeguard)

        layout.addWidget(self.viewer)

        self._set_mode(MODE_NAV)
        self._update_pen()

    def _refresh_color_btn(self):
        c = self._pen_color
        self.color_btn.setStyleSheet(
            f"background:{c.name()}; border:1px solid #555; border-radius:3px;")

    def _pick_color(self):
        c = QColorDialog.getColor(self._pen_color, self, "Välj färg")
        if c.isValid():
            self._pen_color = c
            self._refresh_color_btn()
            self._update_pen()

    def _open_pdf(self):
        if not HAS_PYMUPDF:
            QMessageBox.warning(self, "PyMuPDF saknas",
                "Installera med:\n    pip install PyMuPDF\nStarta sedan om.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Öppna P&ID", "", "PDF-dokument (*.pdf);;Alla filer (*.*)")
        if not path:
            return
        if not self.viewer.load_pdf(path, page=0):
            QMessageBox.warning(self, "Fel", "Kunde inte öppna PDF-filen.")
            return
        self.db.set_pid_path(path)
        self._update_page_label()
        self._load_overlays()
        self.scan_btn.setEnabled(True)

    def _goto_page(self, n):
        if self.viewer.pdf_doc is None:
            return
        self.viewer.goto_page(n)
        self._update_page_label()
        self._load_overlays()

    def _update_page_label(self):
        total = self.viewer.page_count()
        self.page_label.setText(
            f"{self.viewer.current_page + 1} / {total}" if total > 0 else "—")

    def _set_mode(self, mode):
        for m, btn in self.mode_buttons.items():
            btn.setChecked(m == mode)
        self.viewer.set_mode(mode)
        self.style_widget.setVisible(mode == MODE_NODE)

    def _update_pen(self):
        self.viewer.set_pen_style(
            self._pen_color, self.width_spin.value(), self.alpha_slider.value())

    def _on_markup_finished(self, pts, page):
        self._pending_markup_pts  = pts
        self._pending_markup_page = page
        self.create_node_btn.setEnabled(True)

    def _create_node_from_markup(self):
        if not self._pending_markup_pts:
            return
        name, ok = QInputDialog.getText(self, "Ny nod", "Namn på nod:", text="Ny nod")
        if not ok:
            return
        name  = name.strip() or "Ny nod"
        style = {'color': self._pen_color.name(),
                 'width': self.width_spin.value(),
                 'alpha': self.alpha_slider.value()}
        node_id = self.db.add_node_with_markup(
            name, self._pending_markup_pts, style, self._pending_markup_page)
        self._pending_markup_pts  = None
        self._pending_markup_page = None
        self.create_node_btn.setEnabled(False)
        self._load_overlays()
        self.node_created.emit(node_id)

    def _on_cause_click(self, scene_pos, page, suggested_tag=''):
        # Validate node — might be None, 0, or deleted from DB
        if not self._active_node_id:
            QMessageBox.information(self, "Välj nod",
                "Välj en nod i trädet innan du placerar orsaker.")
            return
        if hasattr(self.db, 'get_node') and not self.db.get_node(self._active_node_id):
            QMessageBox.information(self, "Ogiltig nod",
                "Den valda noden finns inte längre. Välj en nod i trädet.")
            self._active_node_id = None
            return
        # suggested_tag comes from the drawn rectangle's OCR/text extraction

        comp_data  = (self.db.all_component_types_dict()
                      if hasattr(self.db, 'all_component_types_dict') else None)
        mode_freqs = self._load_mode_freqs()
        # Priority: 1. Tag database lookup (most reliable)
        #            2. Shape/prefix classifier (fallback)
        detected_type = (self._db_comp_for_tag(suggested_tag)
                         or self.viewer._detected_comp_type)
        dlg = ComponentPickerDialog(self, suggested_tag=suggested_tag,
                                    component_types=comp_data,
                                    mode_freqs=mode_freqs,
                                    preselect_type=detected_type)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        comp_type = dlg.selected_type
        tag       = dlg.selected_tag
        modes     = dlg.selected_modes
        if not modes:
            return

        pdf_x, pdf_y = self.viewer.scene_to_pdf(scene_pos)
        last_cause_id = None
        for mode in modes:
            label    = f"{tag + ' — ' if tag else ''}{comp_type}: {mode}"
            try:
                cause_id = self.db.add_cause(self._active_node_id)
            except Exception as e:
                QMessageBox.critical(self, "Databasfel",
                    f"Kunde inte skapa orsak:\n{e}\n\nKontrollera att noden finns i trädet.")
                return
            self.db.update_cause(cause_id, label)

            # Auto-set F-level AND store base_freq from component failure frequency
            freq = dlg.selected_freqs.get(mode)
            if freq is not None:
                f_level = self._compute_f_level(freq)
                self.db.update_cause(cause_id, likelihood=f_level, base_freq=freq)
            else:
                # No frequency defined — store None so CausePanel shows empty
                self.db.update_cause(cause_id, base_freq=None)

            self.db.add_cause_marker(cause_id, page, pdf_x, pdf_y, comp_type, tag)
            self.viewer.add_cause_marker(cause_id, pdf_x, pdf_y, comp_type, mode, tag)
            last_cause_id = cause_id

        if last_cause_id is not None:
            self.cause_created.emit(last_cause_id)

    def _on_consequence_click(self, scene_pos, page, suggested_tag=''):
        if self._active_cause_id is None:
            QMessageBox.information(self, "Välj orsak",
                "Välj en cause i trädet innan du placerar en konsekvens.")
            return

        dlg = TargetPickerDialog(self, suggested_tag=suggested_tag, db=self.db)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        pdf_x, pdf_y = self.viewer.scene_to_pdf(scene_pos)

        if dlg.link_to_id:
            # ── Link to existing consequence ──────────────────────────────────
            cons_id = self.db.copy_consequence(dlg.link_to_id, self._active_cause_id)
            if cons_id is None:
                return
            cons = self.db.get_consequence(cons_id)
            display = cons['description'] if cons else '🔗 Länkad konsekvens'
        else:
            # ── Create new consequence ────────────────────────────────────────
            template  = dlg.template
            target    = dlg.target
            chain     = dlg.selected_chain
            base_desc = template.format(target) if target else template.replace('{}', '[okänt objekt]')
            full_desc = _pid_build_chain_text(base_desc, chain) or base_desc
            display   = full_desc

            import json as _json
            chain_json = _json.dumps(chain) if chain else ''
            cons_id = self.db.add_consequence(self._active_cause_id)
            try:
                self.db.update_consequence(cons_id, full_desc, 1, '', chain_json)
            except TypeError:
                self.db.update_consequence(cons_id, full_desc, 1)

        self.db.add_consequence_marker(cons_id, page, pdf_x, pdf_y,
                                       dlg.target if not dlg.link_to_id else '')
        self.viewer.add_consequence_marker(cons_id, pdf_x, pdf_y, display)
        self.consequence_created.emit(cons_id)

    def _on_safeguard_click(self, scene_pos, page, suggested_tag=''):
        if self._active_consequence_id is None:
            QMessageBox.information(self, "Välj konsekvens",
                "Välj en consequence i trädet innan du markerar en safeguard.")
            return

        existing = [s['description'] for s in self.db.safeguards(self._active_consequence_id)]

        dlg = SafeguardPickerDialog(self, suggested_tag=suggested_tag,
                                    existing_safeguards=existing, db=self.db)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        pdf_x, pdf_y = self.viewer.scene_to_pdf(scene_pos)

        if dlg.link_to_id:
            # ── Link to existing safeguard ────────────────────────────────────
            sg_id = self.db.copy_safeguard(dlg.link_to_id, self._active_consequence_id)
            if sg_id is None:
                return
            sg = self.db.get_safeguard(sg_id)
            tag         = ''
            description = sg['description'] if sg else '🔗 Länkad safeguard'
        else:
            # ── Create new safeguard ──────────────────────────────────────────
            tag         = dlg.tag
            description = dlg.description
            sg_id = self.db.add_safeguard(self._active_consequence_id)
            self.db.update_safeguard(sg_id, description)

        self.db.add_safeguard_marker(sg_id, page, pdf_x, pdf_y, tag)
        self.viewer.add_safeguard_marker(sg_id, pdf_x, pdf_y, tag, description)
        self.safeguard_created.emit(sg_id)

        # "Lägg till ytterligare" — reset banner to ready state for next safeguard
        if dlg.add_more and self._scenario_active:
            self._sc_instr.setText("Klicka på nästa safeguard på P&ID:n")
            self._sc_add_sg_btn.setVisible(False)
            self._sc_finish_btn.setVisible(False)
            self._set_mode(MODE_SAFEGUARD)

    def _on_context_action(self, action, pos, page):
        if action == 'cause':
            self._set_mode(MODE_CAUSE)
            # Point click from context menu — extract tag the old way
            tag = find_tag_near_point(self.viewer.pdf_doc, page,
                                      *self.viewer.scene_to_pdf(pos)) \
                  if self.viewer.pdf_doc else ''
            self._on_cause_click(pos, page, tag)
        elif action == 'consequence':
            self._set_mode(MODE_CONSEQUENCE)
            tag = find_tag_near_point(self.viewer.pdf_doc, page,
                                      *self.viewer.scene_to_pdf(pos)) \
                  if self.viewer.pdf_doc else ''
            self._on_consequence_click(pos, page, tag)
        elif action == 'safeguard':
            self._set_mode(MODE_SAFEGUARD)
            tag = find_tag_near_point(self.viewer.pdf_doc, page,
                                      *self.viewer.scene_to_pdf(pos)) \
                  if self.viewer.pdf_doc else ''
            self._on_safeguard_click(pos, page, tag)
        elif action == 'node':
            self._set_mode(MODE_NODE)
        elif action == 'risk_scenario':
            node_id = self._active_node_id or 0
            self.risk_scenario_requested.emit(node_id, pos, page)

    def _draw_tag_highlights(self):
        """Highlight recognized tags on the current PDF page.

        Yellow  = tag in database but not yet a HAZOP cause.
        Green   = tag has at least one defined HAZOP cause.
        """
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            return
        if not hasattr(self.db, 'all_active_tag_codes'):
            return

        # Check if smart database or any database is active
        smart_on = self.db.tag_db_setting('smart_enabled', '0') == '1'
        tag_codes = self.db.all_active_tag_codes()
        if not tag_codes and not smart_on:
            return

        self.viewer.clear_highlights()
        page_num = self.viewer.current_page
        fitz_page = self.viewer.pdf_doc.load_page(page_num)

        # Collect tags that already have HAZOP causes
        used_tags: set = set()
        try:
            markers = self.db.cause_markers_for_page(page_num)
            for m in markers:
                tag = (m['component_tag'] or '').upper().strip()
                if tag:
                    used_tags.add(tag)
            # Also check cause descriptions for tag numbers
            for node in self.db.nodes():
                for cause in self.db.causes(node['id']):
                    from pid_viewer import _pick_best_tag
                    t = _pick_best_tag(cause['description'])
                    if t:
                        used_tags.add(t.upper())
        except Exception:
            pass

        # For smart mode: scan ALL text on page for tag patterns
        if smart_on:
            words = fitz_page.get_text("words")
            for w in words:
                txt = w[4].strip().upper()
                from pid_viewer import _pick_best_tag, _collapse_spaces
                tag = _pick_best_tag(txt) or _pick_best_tag(_collapse_spaces(txt))
                if not tag:
                    continue
                color = '#90EE90' if tag in used_tags else '#FFFFE0'  # green or yellow
                import fitz
                bbox = fitz.Rect(w[0], w[1], w[2], w[3])
                self.viewer.add_tag_highlight(bbox, color,
                    f"{'✓ HAZOP-orsak definierad' if tag in used_tags else '○ Identifierad tagg'}: {tag}")
        else:
            # Database-driven: only highlight tags from active standard
            for code in tag_codes:
                try:
                    hits = fitz_page.search_for(code, quads=False)
                    for bbox in hits:
                        # Check if any specific tag with this prefix is a cause
                        color = '#90EE90' if any(code in t for t in used_tags) else '#FFFFE0'
                        self.viewer.add_tag_highlight(bbox, color,
                            f"{'✓ Definierad' if color == '#90EE90' else '○ Taggkod'}: {code}")
                except Exception:
                    continue

    def _load_overlays(self):
        self.viewer.clear_overlays()
        page = self.viewer.current_page

        for node in self.db.nodes():
            nd       = dict(node)
            raw_pts  = nd.get('markup_points', '') or ''
            nd_page  = int(nd.get('pid_page', 0) or 0)
            if not raw_pts or nd_page != page:
                continue
            try:
                points = [(float(p[0]), float(p[1])) for p in json.loads(raw_pts)]
                style  = json.loads(nd.get('markup_style', '') or '{}')
            except Exception:
                continue
            if points:
                self.viewer.add_node_overlay(nd['id'], points, style, nd.get('name', ''))

        for m in self.db.cause_markers_for_page(page):
            md    = dict(m)
            cause = self.db.get_cause(md['cause_id'])
            label = dict(cause).get('description', '') if cause else ''
            self.viewer.add_cause_marker(
                md['cause_id'], md['x'], md['y'],
                md.get('component_type', ''), label, md.get('component_tag', ''))

        for m in self.db.consequence_markers_for_page(page):
            md   = dict(m)
            cons = self.db.get_consequence(md['consequence_id'])
            desc = dict(cons).get('description', '') if cons else md.get('target_name', '')
            self.viewer.add_consequence_marker(md['consequence_id'], md['x'], md['y'], desc)

        for m in self.db.safeguard_markers_for_page(page):
            md = dict(m)
            sg = self.db.conn.execute(
                "SELECT description FROM safeguards WHERE id=?",
                (md['safeguard_id'],)).fetchone()
            desc = sg['description'] if sg else ''
            self.viewer.add_safeguard_marker(
                md['safeguard_id'], md['x'], md['y'], md.get('tag', ''), desc)

        # Draw connections
        cause_pos = {m['cause_id']: self.viewer.pdf_to_scene(m['x'], m['y'])
                     for m in self.db.cause_markers_for_page(page)}
        cons_pos  = {m['consequence_id']: self.viewer.pdf_to_scene(m['x'], m['y'])
                     for m in self.db.consequence_markers_for_page(page)}
        sg_pos    = {m['safeguard_id']: self.viewer.pdf_to_scene(m['x'], m['y'])
                     for m in self.db.safeguard_markers_for_page(page)}

        for cid, cpos in cons_pos.items():
            c = self.db.get_consequence(cid)
            if c and c['cause_id'] in cause_pos:
                self.viewer.add_connection_line(cause_pos[c['cause_id']], cpos, '#c0392b')

        for sid, spos in sg_pos.items():
            s = self.db.get_safeguard(sid)
            if s and s['consequence_id'] in cons_pos:
                self.viewer.add_connection_line(cons_pos[s['consequence_id']], spos, '#27ae60', dashed=True)

        # Draw tag highlights (yellow = known, green = defined as cause)
        self._draw_tag_highlights()

    def set_active_node(self, node_id):
        self._active_node_id        = node_id
        self._active_cause_id       = None
        self._active_consequence_id = None

    def set_active_cause(self, cause_id):
        self._active_cause_id       = cause_id
        self._active_consequence_id = None
        row = self.db.get_cause(cause_id)
        if row:
            self._active_node_id = dict(row).get('node_id')

    def set_active_consequence(self, cons_id):
        self._active_consequence_id = cons_id
        row = self.db.get_consequence(cons_id)
        if not row:
            return
        cause_id = dict(row).get('cause_id')
        self._active_cause_id = cause_id
        if cause_id:
            cause = self.db.get_cause(cause_id)
            if cause:
                self._active_node_id = dict(cause).get('node_id')

    def _scan_equipment(self):
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.warning(self, "Ingen PDF", "Öppna en P&ID-fil först.")
            return

        # Ask about OCR
        status = ocr_status()
        use_ocr   = False
        ocr_engine = 'auto'

        if status['tesseract'] or status['easyocr']:
            engines = []
            if status['tesseract']: engines.append("pytesseract")
            if status['easyocr']:   engines.append("easyocr")
            reply = QMessageBox.question(
                self, "OCR — skanningsalternativ",
                f"Tillgängliga OCR-motorer: {', '.join(engines)}\n\n"
                "Vill du använda OCR för sidor med lite text?\n"
                "(Ger bättre resultat på skannade P&ID-ritningar men tar längre tid.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            use_ocr = (reply == QMessageBox.StandardButton.Yes)
        else:
            # No OCR available — inform user
            native_test_pg = self.viewer.pdf_doc.load_page(0)
            if len(native_test_pg.get_text("words")) < 10:
                QMessageBox.information(
                    self, "OCR saknas",
                    "PDF:en verkar innehålla lite sökbar text.\n\n"
                    "Installera en OCR-motor för bättre resultat:\n"
                    "  • pip install pytesseract\n"
                    "    (+ ladda ner Tesseract: https://github.com/UB-Mannheim/tesseract/wiki)\n"
                    "  • pip install easyocr  (tyngre men enklare att installera)")

        # Progress dialog
        n_pages = self.viewer.pdf_doc.page_count
        progress = QProgressDialog("Förbereder…", "Avbryt", 0, n_pages, self)
        progress.setWindowTitle("Skannar P&ID")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        def _cb(page_num, total, msg):
            if progress.wasCanceled():
                return
            progress.setValue(page_num)
            progress.setLabelText(msg)
            QApplication.processEvents()

        result = scan_pdf_for_equipment(
            self.viewer.pdf_doc,
            use_ocr=use_ocr,
            ocr_engine=ocr_engine,
            progress_callback=_cb)
        progress.setValue(n_pages)
        progress.close()

        if progress.wasCanceled():
            return

        # Remove meta before checking emptiness
        meta = result.pop('_meta', {})
        real_result = {k: v for k, v in result.items() if not k.startswith('_')}
        result['_meta'] = meta  # put back for dialog

        if not real_result:
            QMessageBox.information(
                self, "Inga taggar",
                "Inga utrustningstaggar hittades.\n\n"
                + ("Försök aktivera OCR (installera pytesseract eller easyocr)."
                   if not use_ocr else
                   "Kontrollera att PDF-filens text är läsbar."))
            return

        dlg = EquipmentScanDialog(result, self.db, self)
        dlg.exec()

    # ── Guided Risk Scenario mode ─────────────────────────────────────────────

    def start_scenario_mode(self, node_id=None):
        """Start guided Risk Scenario: Orsak → Konsekvens → Safeguard."""
        if node_id:
            self._active_node_id = node_id
        if not self._active_node_id:
            QMessageBox.information(None, "Välj nod",
                "Välj en nod i trädet eller på P&ID:n innan du startar Risk Scenario.")
            return
        self._scenario_active = True
        self._scenario_step   = 1
        self._scenario_banner.setVisible(True)
        self._sc_add_sg_btn.setVisible(False)
        self._sc_finish_btn.setVisible(False)
        self._update_scenario_ui()
        self._set_mode(MODE_CAUSE)

    def _update_scenario_ui(self):
        step = self._scenario_step
        _ACTIVE  = "border-radius:3px; padding:1px 6px; font-size:10px; font-weight:bold; background:#ffffff; color:#1F4E79;"
        _DONE    = "border-radius:3px; padding:1px 6px; font-size:10px; font-weight:bold; background:#27ae60; color:white;"
        _WAITING = "border-radius:3px; padding:1px 6px; font-size:10px; font-weight:bold; background:#3a6fa3; color:#aac;"

        for i, pill in enumerate(self._step_pills):
            s = i + 1
            if s < step:
                pill.setStyleSheet(_DONE)
            elif s == step:
                pill.setStyleSheet(_ACTIVE)
            else:
                pill.setStyleSheet(_WAITING)

        instructions = {
            1: "Klicka på orsak/utrustning på P&ID:n",
            2: "Klicka på konsekvens/målobjekt på P&ID:n",
            3: "Klicka på safeguard/barriär på P&ID:n",
        }
        self._sc_instr.setText(instructions.get(step, ""))

    def _sc_on_cause(self, cause_id):
        if not self._scenario_active:
            return
        self._scenario_step = 2
        self.set_active_cause(cause_id)
        self._update_scenario_ui()
        self._set_mode(MODE_CONSEQUENCE)

    def _sc_on_consequence(self, cons_id):
        if not self._scenario_active:
            return
        self._scenario_step = 3
        self.set_active_consequence(cons_id)
        self._update_scenario_ui()
        self._set_mode(MODE_SAFEGUARD)

    def _sc_on_safeguard(self, _sg_id):
        if not self._scenario_active:
            return
        # Show action buttons — stay in safeguard mode for adding more
        self._sc_instr.setText("Safeguard markerad! Lägg till fler eller slutför.")
        self._sc_add_sg_btn.setVisible(True)
        self._sc_finish_btn.setVisible(True)
        self._set_mode(MODE_SAFEGUARD)

    def _scenario_finish(self):
        self._scenario_active = False
        self._scenario_banner.setVisible(False)
        self._sc_add_sg_btn.setVisible(False)
        self._sc_finish_btn.setVisible(False)
        self._set_mode(MODE_NAV)

    def _scenario_abort(self):
        self._scenario_active = False
        self._scenario_banner.setVisible(False)
        self._sc_add_sg_btn.setVisible(False)
        self._sc_finish_btn.setVisible(False)
        self._set_mode(MODE_NAV)

    # ── Tag-database component lookup ─────────────────────────────────────────

    # Maps Excel category strings → component_type keys used in the app
    _CAT_TO_COMP = {
        'instrument':        'Instrument / Sensor',
        'givare':            'Instrument / Sensor',
        'reglerfunktion':    'Instrument / Sensor',
        'larm':              'Instrument / Sensor',
        'brytare':           'Instrument / Sensor',
        'mätvärde':          'Instrument / Sensor',
        'transmitter':       'Instrument / Sensor',
        'reglerventil':      'Ventil',
        'ventil':            'Ventil',
        'pump':              'Pump',
        'kompressor':        'Kompressor',
        'blåsmaskin':        'Kompressor',
        'tank':              'Tank / Kärl',
        'kärl':              'Tank / Kärl',
        'behållare':         'Tank / Kärl',
        'kolonn':            'Tank / Kärl',
        'värmeväxlare':      'Värmeväxlare',
        'kylare':            'Värmeväxlare',
        'kondensor':         'Värmeväxlare',
        'filter':            'Övrigt',
        'sil':               'Övrigt',
        'säkerhetsventil':   'Säkerhetsventil (PSV)',
        'avlastningsventil': 'Säkerhetsventil (PSV)',
        'rörledning':        'Rörledning',
    }

    def _comp_from_db_entry(self, entry: dict) -> str:
        """Map a tag_database entry's category to a component type string."""
        if not entry:
            return ''
        cat = str(entry.get('category', '')).lower()
        for key, comp in self._CAT_TO_COMP.items():
            if key in cat:
                return comp
        name = str(entry.get('name_sv', '') + ' ' + entry.get('name_en', '')).lower()
        for key, comp in self._CAT_TO_COMP.items():
            if key in name:
                return comp
        return ''

    def _db_comp_for_tag(self, tag: str) -> str:
        """Look up the tag prefix in the active tag database and return comp type."""
        if not tag or not hasattr(self.db, 'tag_code_lookup'):
            return ''
        import re as _re
        m = _re.match(r'^([A-Z]+)', tag.upper())
        if not m:
            return ''
        entry = self.db.tag_code_lookup(m.group(1))
        return self._comp_from_db_entry(entry)

    def _load_mode_freqs(self):
        """Return {comp_type: {mode_desc: freq_per_year}} from DB."""
        if not hasattr(self.db, 'component_types'):
            return {}
        result = {}
        for ct in self.db.component_types():
            freqs = {}
            for fm in self.db.failure_modes(ct['id']):
                if fm['freq_per_year'] is not None:
                    freqs[fm['description']] = fm['freq_per_year']
            result[ct['name']] = freqs
        return result

    def _compute_f_level(self, freq_per_year):
        """Convert frequency (events/year) to F-level using matrix boundaries."""
        if not freq_per_year or freq_per_year <= 0:
            return 3   # default
        cfg        = self.db.get_risk_matrix() if hasattr(self.db, 'get_risk_matrix') else {}
        boundaries = sorted(
            float(b) for b in (cfg or {}).get('freq_boundaries',
                                              [1e-5, 1e-4, 1e-3, 0.01, 0.1, 1.0]))
        for i, b in enumerate(boundaries):
            if float(freq_per_year) < b:
                return i - 1
        return len(boundaries) - 1

    def try_reload_pdf(self):
        path = self.db.get_pid_path()
        if path and Path(path).exists() and HAS_PYMUPDF:
            if self.viewer.load_pdf(path, page=0):
                self._update_page_label()
                self._load_overlays()
                self.scan_btn.setEnabled(True)
