#!/usr/bin/env python3
"""Pixel/image-based "find similar symbol" — 2026-08-15, see NOTES.md
"Bildbaserad 'hitta liknande symbol' — vid sidan av vektorlogiken".

Pure Python/PyMuPDF/OpenCV — no Qt dependency (mirrors symbol_geometry.py's
and equipment_detection.py's own stated goal), importable standalone.

Why this exists alongside symbol_geometry.py/equipment_detection.py's
existing vector-based matching, not instead of it: on heavily-tessellated
CAD exports a single symbol's own strokes can be split across dozens of
disconnected drawing paths (see NOTES.md "'Hitta liknande symbol' visar
bara ett streck", 2026-08-15) — automatic vector clustering then has
nothing complete to compare. Visually, a human recognizes the same symbol
instantly regardless of how many vector fragments it happens to be split
into. This module renders the reference region and each candidate page to
grayscale bitmaps and matches with OpenCV's normalized cross-correlation
template matching instead of vector geometry — a completely different
failure mode, so it complements rather than replaces the vector path.
equipment_detection.find_similar_shapes()'s own docstring already flagged
this gap explicitly: "This is the vector/geometry half of the feature...
A pixel/image-based fallback for those pages is a separate, not-yet-built
undertaking."

Candidate contract: find_similar_shapes_visual() returns the exact same
(sim, page_num, x, y, outline) tuple shape as
equipment_detection._scan_candidates() — so pid_viewer.py's
SimilarSymbolSearchDialog can plug either matching method into the SAME
threshold-filtering/live-count/on-canvas-preview/shape_similar_results()
pipeline with zero duplicated UI code.

Known performance tradeoff (not hidden): a full-size page rendered at
300 DPI, matched across up to 7 scale factors and 4-12 rotation steps,
is meaningfully more expensive than vector clustering. Mitigated by a
conservative default DPI, per-page progress/cancellation (same pattern as
_scan_candidates), and the existing "this page only" scope option.

opencv-python/numpy are not new dependencies — both are already installed
transitively via rapidocr_onnxruntime (already a hard requirement in
requirements.txt for OCR) but are now imported directly, so they are also
listed explicitly in requirements.txt.
"""
import importlib.util
import math

import fitz

import symbol_geometry

HAS_CV2 = importlib.util.find_spec('cv2') is not None
HAS_NUMPY = importlib.util.find_spec('numpy') is not None
HAS_PYMUPDF = True

# Rotation steps always tried (rotation_mode='none') — lossless (no
# interpolation) via np.rot90, matching the vector path's own claim that
# symbols rotated in 90-degree steps are already found automatically.
_BASE_ROTATIONS = (0, 90, 180, 270)
# Extra, coarser steps added only for rotation_mode='any' (explicitly
# labeled "experimental, slower" in the UI, same framing as the existing
# vector-mode checkbox) — arbitrary-angle rotation needs an expanded,
# interpolated canvas and is meaningfully more expensive.
_EXTRA_ROTATIONS = (30, 60, 120, 150, 210, 240, 300, 330)

_SCALE_FACTORS = (0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3)

# 2026-08-15 follow-up (see NOTES.md "Bildbaserad 'hitta liknande symbol'"
# — real-file verification): confirmed on the real Shell/St1 PEFS 1500
# file that 150 DPI renders a typical small valve reference (roughly
# 5-15pt across) as only ~11-30px — too coarse to be visually distinctive,
# so cv2.matchTemplate scored huge numbers of unrelated page content at
# a similar, unhelpfully generic ~0.7 "confidence" (200+ candidates above
# the UI's default 60% threshold on a single A4 page). Raising to 300 DPI
# measurably reduced that same search's >=60%-confidence count from 200
# to 7 on the same file — and, counterintuitively, ran FASTER (4s vs 14s
# for the same page), apparently because cv2.matchTemplate's internal
# algorithm selection is less efficient for extremely tiny kernels than
# for a merely small one. 450/600 DPI reduced false positives further but
# cost far more (35-43s for one A4 page) for diminishing returns — 300 is
# the empirically-grounded sweet spot, not an arbitrary guess.
_DEFAULT_DPI = 300
_NMS_IOU_THRESHOLD = 0.3
# A candidate on the reference's own page overlapping the reference region
# at least this much is treated as "the reference matching itself" and
# excluded — mirrors _scan_candidates' exact-index-group self-exclusion,
# just via bbox overlap since there is no vector index group here.
_SELF_MATCH_IOU = 0.5


def render_gray(fitz_page, bbox=None, dpi=_DEFAULT_DPI):
    """Render `fitz_page` (or just `bbox`, a (x0,y0,x1,y1) PDF-space
    rect) to a 2D uint8 numpy grayscale array. Reused for both the
    reference crop and each full candidate page — same page.get_pixmap()
    pattern already used elsewhere (pid_viewer.py, equipment_detection.py),
    just with colorspace=fitz.csGRAY so callers don't pay for a color
    conversion nothing here needs."""
    import numpy as np
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    clip = fitz.Rect(*bbox) if bbox is not None else None
    pix = fitz_page.get_pixmap(matrix=mat, clip=clip, colorspace=fitz.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return arr.copy()   # pix's buffer is freed once pix goes out of scope


def _rotate_gray(img, angle):
    """Rotate a 2D uint8 array by `angle` degrees. Lossless (no
    interpolation, no clipped corners) for exact 90-degree multiples via
    np.rot90; any other angle uses cv2.warpAffine on a canvas expanded to
    fit the whole rotated image, background-filled white (255) to match a
    normal P&ID page's paper background rather than introducing spurious
    dark corners."""
    import numpy as np
    if angle % 90 == 0:
        return np.rot90(img, k=(angle // 90) % 4)
    import cv2
    h, w = img.shape
    diag = int(math.ceil(math.hypot(h, w)))
    center = (w / 2.0, h / 2.0)
    rot_mat = cv2.getRotationMatrix2D(center, angle, 1.0)
    rot_mat[0, 2] += (diag - w) / 2.0
    rot_mat[1, 2] += (diag - h) / 2.0
    return cv2.warpAffine(img, rot_mat, (diag, diag), borderValue=255)


def _nms(matches, iou_threshold=_NMS_IOU_THRESHOLD):
    """Greedy non-max suppression over (score, bbox) pairs — the same
    physical symbol is found repeatedly across neighboring scale/rotation
    steps and must collapse to its single best-scoring detection. Reuses
    the already-existing, already-tested symbol_geometry.bbox_iou()
    instead of a new IoU implementation."""
    kept = []
    for score, bbox in sorted(matches, key=lambda m: -m[0]):
        if all(symbol_geometry.bbox_iou(bbox, k_bbox) < iou_threshold for _k_score, k_bbox in kept):
            kept.append((score, bbox))
    return kept


def _match_page(page_gray, template_gray, min_similarity, scales, rotations, should_cancel=None):
    """All (score, bbox_px) matches of `template_gray` inside
    `page_gray` at >= min_similarity, across every (scale, rotation)
    combination — NOT yet de-duplicated (see _nms).

    should_cancel, if given, is checked once per (scale, rotation)
    combination — NOT just once per page like find_similar_shapes_visual's
    own check. Found necessary (2026-08-16, see NOTES.md "Bildmatchning
    visar ingen symbol och kraschar lätt"): with ignore_scale=True and
    rotation_mode='any', one page's own full scan is up to 7 scales x 12
    rotations = 84 cv2.matchTemplate calls, each potentially taking a
    meaningful fraction of a second on a large page — a per-page-only
    cancellation check meant closing the search dialog mid-scan could
    leave SimilarSymbolSearchDialog._on_dialog_finished's un-timed
    `self._worker.wait()` blocking the UI thread for however long that
    one page's REMAINING combinations took to finish, which reads as a
    hang/crash to a user who then force-closes the app."""
    import numpy as np
    import cv2
    page_h, page_w = page_gray.shape
    raw = []
    for scale in scales:
        if should_cancel and should_cancel():
            break
        th, tw = template_gray.shape
        new_w, new_h = max(1, round(tw * scale)), max(1, round(th * scale))
        if new_w < 3 or new_h < 3:
            continue
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        resized = cv2.resize(template_gray, (new_w, new_h), interpolation=interp)
        for angle in rotations:
            if should_cancel and should_cancel():
                break
            rotated = _rotate_gray(resized, angle)
            rh, rw = rotated.shape
            if rh > page_h or rw > page_w:
                continue
            result = cv2.matchTemplate(page_gray, rotated, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(result >= min_similarity)
            for y, x in zip(ys.tolist(), xs.tolist()):
                raw.append((float(result[y, x]), (x, y, x + rw, y + rh)))
    return _nms(raw)


def find_similar_shapes_visual(pdf_doc, ref_page, ref_bbox, pages=None,
                                min_similarity=0.6, ignore_scale=False,
                                rotation_mode='none', dpi=_DEFAULT_DPI,
                                progress_callback=None, should_cancel=None):
    """Image/pixel-based counterpart to
    equipment_detection._scan_candidates() — same
    (sim, page_num, x, y, outline) tuple contract, un-thresholded (the
    caller, e.g. SimilarSymbolSearchDialog, filters by min_similarity
    itself for live re-filtering without a second scan), so either
    matching method can fill pid_viewer.py's shared results pipeline.

    ref_bbox: PDF-space (x0,y0,x1,y1) of the reference region on
    ref_page — cropped and rendered once as the template.
    should_cancel: optional zero-arg callable, checked once per page AND
    (see _match_page) once per (scale, rotation) combination within a
    page — a page-only check left cancellation unresponsive for however
    long that one page's remaining scale/rotation combinations took.
    """
    if not (HAS_CV2 and HAS_NUMPY and HAS_PYMUPDF) or pdf_doc is None:
        return []
    ref_fitz_page = pdf_doc[ref_page]
    template_gray = render_gray(ref_fitz_page, bbox=ref_bbox, dpi=dpi)
    if template_gray.size == 0 or min(template_gray.shape) < 3:
        return []

    scales = _SCALE_FACTORS if ignore_scale else (1.0,)
    rotations = _BASE_ROTATIONS + (_EXTRA_ROTATIONS if rotation_mode == 'any' else ())

    if pages is None:
        pages = range(pdf_doc.page_count)
    pages = list(pages)
    total = len(pages)
    pdf_scale = dpi / 72.0
    candidates = []
    for page_num in pages:
        if should_cancel and should_cancel():
            break
        if progress_callback:
            progress_callback(page_num, total, f"Sida {page_num + 1}/{total} — bildmatchning…")
        page = pdf_doc[page_num]
        page_gray = render_gray(page, bbox=None, dpi=dpi)
        origin_x, origin_y = page.rect.x0, page.rect.y0
        for score, (px0, py0, px1, py1) in _match_page(
                page_gray, template_gray, min_similarity, scales, rotations,
                should_cancel=should_cancel):
            x0 = px0 / pdf_scale + origin_x
            y0 = py0 / pdf_scale + origin_y
            x1 = px1 / pdf_scale + origin_x
            y1 = py1 / pdf_scale + origin_y
            if page_num == ref_page and symbol_geometry.bbox_iou(
                    (x0, y0, x1, y1), ref_bbox) >= _SELF_MATCH_IOU:
                continue
            outline = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
            candidates.append((score, page_num, (x0 + x1) / 2, (y0 + y1) / 2, outline))
    return candidates
