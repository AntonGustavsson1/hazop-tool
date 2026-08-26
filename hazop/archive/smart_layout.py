"""Retired "Smart layout" feature (removed from the active app 2026-08-26,
see NOTES.md "Riv Smart Layout ur den aktiva applikationen").

This is the ENTIRE, UNMODIFIED implementation of the "Smart layout"
button that used to live in the P&ID study-board toolbar (PIDPanel, see
pid_panel_mod.py): it analyzed off-page connectors across every sheet in
the PDF, matched them into sheet-to-sheet connections, and auto-arranged
the board using a Sugiyama-style layered process-flow layout
(_propose_layout). Anton asked for the feature to be torn out of the
active application but the code preserved rather than deleted, in case
it's ever wanted again.

`ConnectorAnalyzer` (QThread) and `_propose_layout` are copied verbatim
out of pid_viewer.py, where they lived from 2026-08-06 ("Generisk
connectoranalys") through 2026-08-24 ("Smart layout — lagerbaserad
processflödeslayout") — see NOTES.md for their original history. Nothing
below has been edited beyond adding this docstring and the imports that
follow (pid_viewer.py used to provide these as plain module globals).

What did NOT move here, and why: `_sheet_ref_variants`, `_detect_dialect`,
`_DIALECTS` (including `_RE_SHEET_NUM`, referenced directly inside
`_DIALECTS['classic']`) stayed in pid_viewer.py because they are still
used by the ACTIVE code that draws connection arcs for already-saved
connections when a P&ID loads (PIDPanel._load_overlays, see
pid_panel_mod.py) -- that display path is independent of whether the
data was ever produced by this retired analyzer, so removing those would
have silently broken it. Only the regex patterns/dicts used EXCLUSIVELY
by ConnectorAnalyzer (`_RE_RDS_SHEET`, `_RE_ITS_CONN`, `_RE_GRYAAB_CONN`,
`_RE_TO_FROM`, `_RE_DIR_KW`, `_RE_LINE_ID`, `_MEDIA_PATTERNS`,
`_MEDIA_WEIGHTS`) were confirmed (via a repo-wide grep) to have no other
caller and moved down here with the class/function that used them.

To reconnect this feature to the app again:
1. Re-add the "Smart layout" QPushButton + its three init attrs
   (`_smart_layout_prev`, `_analyzer_thread`, `_analyzer_progress_dlg`)
   and the `_run_smart_layout`/`_on_smart_layout_done`/
   `_undo_smart_layout` methods to `PIDPanel` in pid_panel_mod.py (see
   git history around the 2026-08-26 removal commit for the exact code).
2. Change `from archive.smart_layout import ConnectorAnalyzer` back to
   `from pid_viewer import ConnectorAnalyzer` in pid_panel_mod.py (or
   just import it from here -- both work, this module is self-contained).

Dev tooling used to test/tune this feature against the real `P&ID ref/`
library (`archive/dev_scripts/analyze_refs.py`,
`archive/dev_scripts/render_layout.py`) moved alongside this module, with
their own imports updated to match.
"""

import re

import fitz
from PyQt6.QtCore import QThread, pyqtSignal

from pid_viewer import (
    HAS_PYMUPDF, _DIALECTS, _detect_dialect, _sheet_ref_variants,
    _RE_RDS_SHEET, _RE_ITS_CONN, _RE_GRYAAB_CONN, _RE_SHEET_NUM,
    _RE_TO_FROM, _RE_DIR_KW, _RE_LINE_ID, _MEDIA_PATTERNS, _MEDIA_WEIGHTS,
)
from equipment_detection import _get_easyocr_reader


class ConnectorAnalyzer(QThread):
    """Scans PDF pages for off-page connectors and proposes a board layout."""
    progress   = pyqtSignal(str)
    # connectors, connections, layout, page_sheet_nums
    finished_analysis = pyqtSignal(list, list, dict, dict)

    def __init__(self, pdf_path, page_count, page_widths_pdf, page_heights_pdf,
                 render_scale, active_pages=None, parent=None):
        super().__init__(parent)
        self._pdf_path        = str(pdf_path)
        self._page_count      = page_count
        self._page_widths_pdf = dict(page_widths_pdf)
        self._page_heights_pdf = dict(page_heights_pdf)
        self._render_scale    = render_scale
        # active_pages: only these get laid out (others are scanned for connectors only)
        self._active_pages    = list(active_pages) if active_pages is not None else None
        self._deadline        = 0.0

    def run(self):
        import time
        self._deadline = time.time() + 45.0   # longer for OCR-heavy sets
        all_connectors = []
        page_sheet_nums = {}  # pn → sheet number string (best guess)
        doc = None

        try:
            try:
                doc = fitz.open(self._pdf_path)
            except Exception as e:
                self.progress.emit(f"Kunde inte öppna PDF: {e}")
                self.finished_analysis.emit([], [], {}, {})
                return

            # ── Auto-detect dialect from first 5 pages ────────────────────────────
            sample_texts = []
            for pn in range(min(5, doc.page_count)):
                sample_texts.append(doc.load_page(pn).get_text("text"))
            self._dialect = _detect_dialect(sample_texts)
            dialect_conf  = _DIALECTS[self._dialect]
            self.progress.emit(f"Dialekt: {dialect_conf['name']}")

            for pn in range(doc.page_count):
                if time.time() > self._deadline:
                    self.progress.emit(f"Tidsgräns — {pn}/{doc.page_count} blad klara")
                    break
                self.progress.emit(f"Blad {pn + 1}/{doc.page_count}…")
                page = doc.load_page(pn)
                pw = float(page.rect.width)
                ph = float(page.rect.height)

                # ── Extract sheet number using dialect title area ──────────────────
                ta = dialect_conf['title_area']
                title_rect = fitz.Rect(pw*ta[0], ph*ta[1], pw*ta[2], ph*ta[3])
                title_text = page.get_text("text", clip=title_rect)
                m = dialect_conf['sheet_num_re'].search(title_text)
                if m:
                    page_sheet_nums[pn] = m.group(1).upper().strip()

                # ── Native text in edge zones ──────────────────────────────────────
                spans = self._get_spans(page)
                native_word_count = len(spans)
                connectors = self._find_in_zones(spans, pn, pw, ph,
                                                 ocr_used=False, page=page)

                # ── OCR: trigger when page has few native words (scanned PDF) ──────
                needs_ocr = (not connectors or native_word_count < 30)
                if needs_ocr and HAS_PYMUPDF and time.time() < self._deadline - 2.0:
                    ocr_text = self._ocr_edges(page, pw, ph)
                    if ocr_text:
                        ocr_spans = self._text_to_spans(ocr_text, pw, ph)
                        ocr_conns = self._find_in_zones(ocr_spans, pn, pw, ph,
                                                        ocr_used=True, page=page)
                        if ocr_conns:
                            connectors = ocr_conns
                        # Also try to extract sheet number from OCR if not found yet
                        if pn not in page_sheet_nums:
                            all_ocr = ' '.join(ocr_text.values())
                            m2 = dialect_conf['sheet_num_re'].search(all_ocr)
                            if m2:
                                page_sheet_nums[pn] = m2.group(1).upper().strip()

                all_connectors.extend(connectors)

            # ── Build sheet-number lookup: sheet_str (and variants) → pn ──
            sheet_lookup = {}
            for k, v in page_sheet_nums.items():
                for variant in _sheet_ref_variants(v.upper()):
                    sheet_lookup.setdefault(variant, k)

            # ── Match connectors into connections ──
            connections = self._match_connections(all_connectors, sheet_lookup,
                                                  self._page_count, page_sheet_nums)

            # ── Propose layout (active pages only) ──
            layout_pages = (self._active_pages if self._active_pages is not None
                            else list(range(self._page_count)))
            layout = _propose_layout(connections, layout_pages,
                                     self._page_widths_pdf, self._page_heights_pdf,
                                     self._render_scale)

            # Convert int keys to str for JSON serialisation
            sheet_num_map_str = {str(k): v for k, v in page_sheet_nums.items()}
            self.finished_analysis.emit(all_connectors, connections, layout, sheet_num_map_str)
        except Exception as e:
            import logging
            logging.error(f"ConnectorAnalyzer.run() failed: {e}", exc_info=True)
            # CRITICAL: still emit finished_analysis so the UI unblocks and
            # the modal progress dialog closes instead of hanging forever.
            self.finished_analysis.emit([], [], {}, {})
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_spans(self, page):
        spans = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    t = span.get("text", "").strip()
                    if not t:
                        continue
                    b = span["bbox"]
                    spans.append({"text": t, "x": (b[0]+b[2])/2, "y": (b[1]+b[3])/2,
                                  "x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]})
        return spans

    def _text_to_spans(self, text_dict, pw, ph):
        """Convert OCR output dict {edge: str} into pseudo-spans with edge positions."""
        spans = []
        for edge, text in text_dict.items():
            if not text.strip():
                continue
            if edge == 'left':
                cx, cy = pw * 0.12, ph * 0.5
            elif edge == 'right':
                cx, cy = pw * 0.88, ph * 0.5
            elif edge == 'top':
                cx, cy = pw * 0.5, ph * 0.06
            else:
                cx, cy = pw * 0.5, ph * 0.94
            for line in text.split('\n'):
                line = line.strip()
                if line:
                    spans.append({"text": line, "x": cx, "y": cy,
                                  "x0": cx-10, "y0": cy-5, "x1": cx+10, "y1": cy+5})
        return spans

    def _find_arrow_shapes(self, page, pw, ph):
        """Detect pentagon/arrow connector shapes near page edges using vector graphics.
        Returns list of (cx, cy, edge) tuples in PDF coordinates."""
        results = []
        try:
            drawings = page.get_drawings()
        except Exception:
            return results
        for d in drawings:
            r = d.get('rect')
            if r is None:
                continue
            w = r.x1 - r.x0
            h = r.y1 - r.y0
            # Skip tiny marks and huge blocks (title boxes etc.)
            if w < 15 or h < 6 or w > pw * 0.55 or h > ph * 0.25:
                continue
            # Must be near an edge
            at_left   = r.x1 < pw * 0.32
            at_right  = r.x0 > pw * 0.68
            at_top    = r.y1 < ph * 0.26
            at_bottom = r.y0 > ph * 0.74
            if not (at_left or at_right or at_top or at_bottom):
                continue
            # Arrow-like aspect ratio: for left/right edges wide>tall; top/bottom tall>wide
            if (at_left or at_right) and not (1.5 < w / max(h, 1) < 20):
                continue
            if (at_top or at_bottom) and not (0.3 < w / max(h, 1) < 3):
                continue
            n_items = len(d.get('items', []))
            if not (3 <= n_items <= 10):
                continue
            cx = (r.x0 + r.x1) / 2
            cy = (r.y0 + r.y1) / 2
            if at_left:
                edge = 'left'
            elif at_right:
                edge = 'right'
            elif at_top:
                edge = 'top'
            else:
                edge = 'bottom'
            results.append((cx, cy, edge, r))
        return results

    def _find_in_zones(self, spans, pn, pw, ph, ocr_used, page=None):
        """Find off-page connectors in the page's edge band.

        Each candidate is assigned to its NEAREST page edge — a connector in
        the top-left corner belongs to whichever edge it is closest to — and
        candidates are deduplicated by (ref_sheet, position) across all
        detection passes, so the same physical symbol can never appear as two
        connectors on different edges (the old per-zone scan produced corner
        duplicates: one 'left'/'in' + one 'top'/'unknown' for the same symbol).
        """
        accepted = []

        ta = _DIALECTS.get(getattr(self, '_dialect', 'classic'), {}).get('title_area')

        def in_title_area(x, y):
            return (ta is not None
                    and pw*ta[0] <= x <= pw*ta[2]
                    and ph*ta[1] <= y <= ph*ta[3])

        def nearest_edge(x, y):
            d = {'left': x, 'right': pw - x, 'top': y, 'bottom': ph - y}
            return min(d, key=d.get)

        def consider(text, x, y):
            conn = self._parse_connector(text, nearest_edge(x, y), pn, x, y,
                                         pw, ph, ocr_used)
            if not conn:
                return
            # Directionless refs inside the title block are almost always the
            # drawing-reference list / title text, not flow connectors.
            if conn['direction'] == 'unknown' and in_title_area(x, y):
                return
            for i, old in enumerate(accepted):
                if (old['ref_sheet'] == conn['ref_sheet']
                        and abs(old['x_pdf'] - x) < pw * 0.05
                        and abs(old['y_pdf'] - y) < ph * 0.05):
                    if conn['confidence'] > old['confidence']:
                        accepted[i] = conn
                    return
            accepted.append(conn)

        # ── Pass 1: text clusters in the edge band ────────────────────────────
        band = [s for s in spans
                if s["x"] <= pw*0.32 or s["x"] >= pw*0.68
                or s["y"] <= ph*0.26 or s["y"] >= ph*0.74]
        for cluster in self._cluster_spans(band, gap=60.0):
            combined = ' '.join(s["text"] for s in cluster)
            cx = sum(s["x"] for s in cluster) / len(cluster)
            cy = sum(s["y"] for s in cluster) / len(cluster)
            consider(combined, cx, cy)

        # ── Pass 2: vector-shape anchored search ──────────────────────────────
        if page is not None:
            for shape_cx, shape_cy, _edge, shape_rect in self._find_arrow_shapes(page, pw, ph):
                margin_x = max(shape_rect.width * 1.5, 40)
                margin_y = max(shape_rect.height * 2.0, 40)
                nearby = [s for s in spans
                          if abs(s["x"] - shape_cx) < margin_x + shape_rect.width
                          and abs(s["y"] - shape_cy) < margin_y + shape_rect.height]
                if nearby:
                    consider(' '.join(s["text"] for s in nearby),
                             shape_cx, shape_cy)
        return accepted

    def _cluster_spans(self, spans, gap=60.0):
        if not spans:
            return []
        spans = sorted(spans, key=lambda s: (s["y"], s["x"]))
        clusters, current = [], [spans[0]]
        for s in spans[1:]:
            prev = current[-1]
            dy = abs(s["y"] - prev["y"])
            dx = abs(s["x"] - prev["x"])
            if dy < gap and dx < gap * 3:
                current.append(s)
            else:
                clusters.append(current)
                current = [s]
        clusters.append(current)
        return clusters

    def _parse_connector(self, text, edge, pn, cx, cy, pw, ph, ocr_used):
        dialect = getattr(self, '_dialect', 'classic')
        keyword = ref_sheet = rds_code = None
        lkab_format = its_format = gryaab_format = False
        ref_span = None          # char span of the sheet ref inside text
        pattern_hit = False      # ref found via dialect pattern (not TO/FROM capture)

        # ── Sheet reference: dialect pattern FIRST ────────────────────────────
        # The drawing number itself (S0000155, 346-0000-001, XFB_40208…) is far
        # more reliable than the word after TILL/FRÅN — Hybrit connectors say
        # "346-0000-001-PS / TILL FACKLA" where FACKLA is equipment, not a sheet.
        if dialect == 'lkab':
            m_rds = _RE_RDS_SHEET.search(text)
            if m_rds:
                rds_code    = m_rds.group(1).upper()
                ref_sheet   = ('S' + m_rds.group(2)).upper()
                ref_span    = m_rds.span()
                lkab_format = pattern_hit = True
            else:
                m2 = re.search(r'\bS(\d{6,8})\b', text, re.I)
                if m2:
                    ref_sheet   = ('S' + m2.group(1)).upper()
                    ref_span    = m2.span()
                    pattern_hit = True

        elif dialect == 'its':
            m_its = _RE_ITS_CONN.search(text)
            if m_its:
                ref_sheet  = m_its.group(1).upper()   # e.g. XFB_40208
                ref_span   = m_its.span()
                its_format = pattern_hit = True
            else:
                m2 = _DIALECTS['its']['sheet_num_re'].search(text)
                if m2:
                    ref_sheet   = m2.group(1).upper()
                    ref_span    = m2.span()
                    pattern_hit = True

        elif dialect == 'gryaab':
            m_gr = _RE_GRYAAB_CONN.search(text)
            if m_gr:
                ref_sheet     = m_gr.group(1).upper()
                ref_span      = m_gr.span()
                gryaab_format = pattern_hit = True

        elif dialect == 'hybrit':
            m2 = _DIALECTS['hybrit']['sheet_num_re'].search(text)
            if m2:
                ref_sheet   = m2.group(1).upper()
                ref_span    = m2.span()
                pattern_hit = True

        else:  # classic / fallback
            m_rds = _RE_RDS_SHEET.search(text)
            if m_rds:
                rds_code    = m_rds.group(1).upper()
                ref_sheet   = ('S' + m_rds.group(2)).upper()
                ref_span    = m_rds.span()
                lkab_format = pattern_hit = True
            else:
                m2 = _RE_SHEET_NUM.search(text)
                if m2:
                    ref_sheet   = m2.group(1).upper().strip()
                    ref_span    = m2.span()
                    pattern_hit = True

        # Fallback: capture the word after TO/FROM/TILL/FRÅN as the reference
        if ref_sheet is None:
            m_kw = _RE_TO_FROM.search(text)
            if m_kw:
                keyword   = m_kw.group(1).upper()
                ref_sheet = m_kw.group(2).upper().strip()
                ref_span  = m_kw.span(2)

        if not ref_sheet:
            return None

        # ── Direction ─────────────────────────────────────────────────────────
        # A TILL/FRÅN keyword decides direction only when it clearly belongs to
        # the reference: first word of the connector text, or directly adjacent
        # to the sheet ref ("TO S0000162", "258-0000-001-PS TILL FACKLA").
        # A keyword inside a service description ("KVÄVE TILL ELFILTER") refers
        # to equipment, not the sheet — there the edge convention decides.
        if keyword is None and ref_span is not None:
            for m_kw in _RE_DIR_KW.finditer(text):
                at_start   = m_kw.start() <= 1
                before_ref = 0 <= ref_span[0] - m_kw.end() <= 2
                after_ref  = 0 <= m_kw.start() - ref_span[1] <= 2
                if at_start or before_ref or after_ref:
                    keyword = m_kw.group(1).upper()
                    break

        kw_upper = (keyword or '').upper().replace('Å', 'A').replace('Ä', 'A')
        if kw_upper in ('TO', "CONT'D", 'CONTD', 'TILL'):
            dir_kw = 'out'
        elif kw_upper in ('FROM', 'FRAN', 'FRAAN'):
            dir_kw = 'in'
        else:
            dir_kw = None

        edge_dir_map = {'right': 'out', 'left': 'in', 'top': 'unknown', 'bottom': 'unknown'}
        direction  = dir_kw or edge_dir_map.get(edge, 'unknown')
        dir_factor = {'out': 1.0, 'in': 1.0, 'unknown': 0.4}.get(direction, 0.4)
        if edge in ('top', 'bottom'):
            dir_factor = 0.5

        # ── Line ID & media ───────────────────────────────────────────────────
        lm = _RE_LINE_ID.search(text)
        ref_line_id = lm.group(1) if lm else None

        media_type = 'unknown'
        for mname, pat in _MEDIA_PATTERNS:
            if pat.search(text):
                media_type = mname
                break

        # ── Confidence ────────────────────────────────────────────────────────
        if lkab_format or its_format:
            conf = 0.85
        elif keyword and pattern_hit:
            conf = 0.90
        elif keyword:
            conf = 0.85          # ref captured via TO/FROM keyword
        elif gryaab_format:
            conf = 0.70
        elif pattern_hit:
            conf = 0.75          # dialect-specific drawing number, no keyword
        else:
            conf = 0.55
        if ref_line_id is None:
            conf -= 0.08
        if ocr_used:
            conf -= 0.10
        if media_type == 'unknown':
            conf -= 0.08
        conf = max(0.10, conf)

        mw = _MEDIA_WEIGHTS.get(media_type, 0.05)
        weight = round(mw * conf * dir_factor, 3)

        import datetime
        return {
            "pid_page": pn,
            "x_pdf": cx, "y_pdf": cy,
            "direction": direction,
            "edge": edge,
            "ref_text": text[:200],
            "ref_sheet": ref_sheet,
            "ref_line_id": ref_line_id,
            "media_type": media_type,
            "weight": weight,
            "confidence": conf,
            "raw_text": text[:500],
            "ocr_used": int(ocr_used),
            "analyzed_at": datetime.datetime.now().isoformat(),
        }

    def _ocr_edges(self, page, pw, ph):
        """OCR all four edges. Returns {edge: text}. Uses 3× scale + preprocessing."""
        if not HAS_PYMUPDF:
            return {}
        strips = {
            'left':   fitz.Rect(0,       0,       pw*0.28, ph),
            'right':  fitz.Rect(pw*0.72, 0,       pw,      ph),
            'top':    fitz.Rect(0,       0,       pw,      ph*0.22),
            'bottom': fitz.Rect(0,       ph*0.78, pw,      ph),
        }
        mat = fitz.Matrix(3.0, 3.0)
        result = {}
        for edge, clip in strips.items():
            try:
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                text = self._ocr_strip(pix)
                if text:
                    result[edge] = text
            except Exception:
                pass
        return result

    def _ocr_strip(self, pix):
        """OCR one pixmap strip. Tries tesseract (multi-PSM + preprocessing) then easyocr."""
        try:
            from PIL import Image as _PILImg, ImageEnhance, ImageFilter
            pil_img = _PILImg.frombytes("RGB", [pix.width, pix.height], pix.samples)
            gray    = pil_img.convert('L')
            gray    = ImageEnhance.Contrast(gray).enhance(2.5)
            gray    = gray.filter(ImageFilter.SHARPEN)

            try:
                import pytesseract
                for psm in (6, 11, 3):
                    text = pytesseract.image_to_string(
                        gray, config=f'--psm {psm} --oem 1').strip()
                    if text:
                        return text
            except Exception:
                pass

            # easyocr fallback (uses centralized lifecycle manager)
            try:
                reader = _get_easyocr_reader()
                if reader is not None:
                    hits = reader.readtext(pil_img,
                                          detail=0,
                                          paragraph=True)
                    text = ' '.join(hits).strip()
                    if text:
                        return text
            except Exception:
                pass
        except Exception:
            pass
        return ''

    def _match_connections(self, connectors, sheet_lookup, page_count,
                          page_sheet_nums=None):
        """Build page→page connections from BOTH ends of each documented flow.

        A physical flow A→B is documented twice on the drawings: an OUT
        connector on sheet A referencing B, and an IN connector on sheet B
        referencing A.  Both ends are used — if extraction missed one end the
        other still creates the connection, and when both were found the
        confidence is boosted (flow confirmed from both sheets).
        """
        # Build own-sheet reverse map: pn → own sheet num (upper)
        own_sheet = {}
        if page_sheet_nums:
            own_sheet = {int(k): v.upper() for k, v in page_sheet_nums.items()}

        def resolve_page(ref):
            for v in _sheet_ref_variants(ref):
                if v in sheet_lookup:
                    return sheet_lookup[v]
            # Fuzzy: try suffix match
            for k, v in sheet_lookup.items():
                if k.endswith(ref) or ref.endswith(k):
                    return v
            return None

        flows = {}   # ('ghost', page, ref) or (from_page, to_page) → record

        for i, c in enumerate(connectors):
            c['_idx'] = i
            ref = (c.get('ref_sheet') or '').upper().strip()
            if not ref:
                continue
            # Skip self-referential connectors (sheet references its own number)
            own = own_sheet.get(c['pid_page'], '__NONE__')
            if ref in _sheet_ref_variants(own):
                continue

            tp = resolve_page(ref)
            # Store resolved page directly on the connector so draw code can
            # look up by (pid_page, ref_page) without the sheet_num_map
            c['ref_page'] = tp

            if tp is None:
                key = ('ghost', c['pid_page'], ref)
                rec = flows.get(key)
                if rec is None:
                    flows[key] = {
                        'from_page': c['pid_page'], 'to_page': None,
                        'from_connector': c['_idx'], 'to_connector': None,
                        'media_type': c['media_type'], 'weight': c['weight'],
                        'confidence': c['confidence'], 'is_bidirectional': 0,
                        'is_ghost': 1, 'ghost_ref': ref, 'warning': None,
                        'from_edge': c.get('edge'), 'to_edge': None,
                        'n_out': 0, 'n_in': 0,
                    }
                else:
                    rec['weight'] = round(
                        1.0 - (1.0 - rec['weight']) * (1.0 - c['weight']), 3)
                continue

            if tp == c['pid_page']:
                continue   # recirculation on the same sheet

            # IN connector on B referencing A means flow A→B
            is_in = (c['direction'] == 'in')
            a, b = (tp, c['pid_page']) if is_in else (c['pid_page'], tp)

            rec = flows.get((a, b))
            if rec is None:
                rec = flows[(a, b)] = {
                    'from_page': a, 'to_page': b,
                    'from_connector': None, 'to_connector': None,
                    'media_type': None, 'weight': 0.0, 'confidence': 0.0,
                    'is_bidirectional': 0, 'is_ghost': 0, 'ghost_ref': None,
                    'warning': None, 'from_edge': None, 'to_edge': None,
                    'n_out': 0, 'n_in': 0,
                }
            if is_in:
                rec['n_in'] += 1
                if rec['to_connector'] is None:
                    rec['to_connector'] = c['_idx']
                    rec['to_edge']      = c.get('edge')
            else:
                rec['n_out'] += 1
                if rec['from_connector'] is None:
                    rec['from_connector'] = c['_idx']
                    rec['from_edge']      = c.get('edge')
            # Accumulate weight: w = 1 - (1-w_old)*(1-w_new)
            rec['weight']     = round(
                1.0 - (1.0 - rec['weight']) * (1.0 - c['weight']), 3)
            rec['confidence'] = max(rec['confidence'], c['confidence'])
            if rec['media_type'] in (None, 'unknown') and c['media_type']:
                rec['media_type'] = c['media_type']

        connections = list(flows.values())
        real_keys = {(r['from_page'], r['to_page'])
                     for r in connections if not r['is_ghost']}
        for r in connections:
            if r['media_type'] is None:
                r['media_type'] = 'unknown'
            if r['is_ghost']:
                continue
            if r['n_out'] and r['n_in']:
                # Flow documented on both sheets — high trust
                r['confidence'] = round(min(0.99, r['confidence'] + 0.08), 3)
            if r['n_out'] > 1 or r['n_in'] > 1:
                r['warning'] = 'multiple'
            if (r['to_page'], r['from_page']) in real_keys:
                r['is_bidirectional'] = 1

        return connections


def _propose_layout(connections, active_pages, page_widths_pdf, page_heights_pdf, render_scale):
    """Layered process-flow layout (Sugiyama-style).

    P&ID sheets follow the convention that flow enters on the left and leaves
    on the right.  The board is arranged the same way so a human reads it like
    the process itself: every sheet sits one column to the right of the sheets
    that feed it, parallel branches stack vertically, and connected sheets are
    aligned so the connection lines run as straight as possible.

    Per connected component:
      1. break cycles            (DFS back-edge removal; recirculation loops)
      2. longest-path layering   → column per sheet (vertical-edge links may
                                   share a column and stack instead)
      3. barycenter sweeps       → row order within each column
      4. alignment passes        → connected sheets line up horizontally
    Very long chains wrap serpentine-style into bands.  Components are stacked
    below each other, largest first; sheets without any connections are placed
    in a grid at the bottom.
    """
    import math
    from collections import deque, defaultdict

    if not active_pages:
        return {}

    rs     = render_scale
    H_GAP  = 650.0    # gap between columns (scene px)
    V_GAP  = 420.0    # gap between sheets in a column
    C_GAP  = 1300.0   # gap between disconnected components
    MARGIN = 300.0
    page_set = set(active_pages)
    ws = {i: page_widths_pdf.get(i,  800) * rs for i in active_pages}
    hs = {i: page_heights_pdf.get(i, 600) * rs for i in active_pages}

    # ── Directed edges between active pages ───────────────────────────────────
    edge_w   = {}     # (a, b) → max weight
    vertical = set()  # pairs connected via top/bottom connectors
    for c in connections:
        a, b = c.get('from_page'), c.get('to_page')
        if a not in page_set or b is None or b not in page_set or a == b:
            continue
        w = float(c.get('weight', 0.5) or 0.5)
        edge_w[(a, b)] = max(edge_w.get((a, b), 0.0), w)
        if (c.get('from_edge') in ('top', 'bottom')
                or c.get('to_edge') in ('top', 'bottom')):
            vertical.add((a, b))

    # For mutual pairs (A⇄B) keep only the dominant direction for layering
    drop = set()
    for (a, b), w in edge_w.items():
        if (b, a) in edge_w and (a, b) not in drop and (b, a) not in drop:
            wr = edge_w[(b, a)]
            drop.add((a, b) if (wr > w or (wr == w and b < a)) else (b, a))

    succ = defaultdict(set)
    und  = defaultdict(set)
    for (a, b) in edge_w:
        und[a].add(b); und[b].add(a)
        if (a, b) not in drop:
            succ[a].add(b)

    # ── Utility hubs ───────────────────────────────────────────────────────────
    # Collection sheets (flare/vent header, effluent, drain) are wired to a
    # large share of the plant.  Kept in the flow they pull long lines through
    # everything; humans park them in a row below the process — so do we.
    und_full  = {i: set(und[i]) for i in active_pages}
    n_connected = sum(1 for i in active_pages if und[i])
    hub_thresh  = max(7, int(n_connected * 0.10))
    hubs = [i for i in sorted(active_pages) if len(und[i]) >= hub_thresh]
    hub_set = set(hubs)
    for h in hubs:
        for nb in list(und[h]):
            und[nb].discard(h)
            succ[nb].discard(h)
        und[h]  = set()
        succ[h] = set()

    def med(vals):
        vals = sorted(vals)
        m = len(vals) // 2
        return vals[m] if len(vals) % 2 else 0.5 * (vals[m - 1] + vals[m])

    def delta(a, b):
        # Vertical connectors may share a column (stacked); horizontal flow
        # always advances one column to the right.
        return 0 if (a, b) in vertical else 1

    # ── Connected components, largest first ───────────────────────────────────
    comps, seen = [], set()
    for start in sorted(active_pages):
        if start in seen or not und[start]:
            continue
        comp, q = [], deque([start])
        seen.add(start)
        while q:
            n = q.popleft()
            comp.append(n)
            for nb in sorted(und[n]):
                if nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        comps.append(sorted(comp))
    comps.sort(key=len, reverse=True)
    isolated = [i for i in sorted(active_pages)
                if not und[i] and i not in hub_set]

    pos = {}              # page → (x_topleft, y_topleft)
    cursor_y = MARGIN

    for comp in comps:
        cset = set(comp)
        dag  = {n: sorted(s for s in succ[n] if s in cset) for n in comp}

        # ── 1. Break cycles: greedy feedback-arc-set ordering (Eades) ─────────
        # Arrange the sheets in a linear order with as few edges as possible
        # pointing backwards; the backward edges are the recycle/return lines
        # and are cut for layering, so the MAIN flow always runs left→right.
        # (Plain DFS back-edge removal can cut the main line instead and push
        # the whole feed chain deep to the right.)
        g_succ = {n: set(dag[n]) for n in comp}
        g_pred = defaultdict(set)
        for n, ss in g_succ.items():
            for s in ss:
                g_pred[s].add(n)
        remaining = set(comp)
        s1, s2 = [], []
        while remaining:
            changed = True
            while changed:
                changed = False
                for n in sorted(remaining):           # sinks → right end
                    if not (g_succ[n] & remaining):
                        s2.append(n); remaining.discard(n); changed = True
                for n in sorted(remaining):           # sources → left end
                    if n in remaining and not (g_pred[n] & remaining):
                        s1.append(n); remaining.discard(n); changed = True
            if remaining:
                # Strongest net outflow first; page number breaks ties —
                # sheet numbering normally follows the process order.
                def net_out(n):
                    o = sum(edge_w.get((n, s), 0.5) for s in g_succ[n] & remaining)
                    i = sum(edge_w.get((p, n), 0.5) for p in g_pred[n] & remaining)
                    return o - i
                n = max(sorted(remaining), key=net_out)
                s1.append(n); remaining.discard(n)
        order_idx = {n: i for i, n in enumerate(s1 + list(reversed(s2)))}
        back = {(a, b) for a in comp for b in dag[a]
                if order_idx[a] > order_idx[b]}
        fwd   = {n: [s for s in dag[n] if (n, s) not in back] for n in comp}
        fpred = defaultdict(list)
        for n, ss in fwd.items():
            for s in ss:
                fpred[s].append(n)

        # ── 2. Longest-path layering (relaxation; components are small) ───────
        layer = {n: 0 for n in comp}
        for _ in range(len(comp)):
            changed = False
            for n in comp:
                for s in fwd[n]:
                    need = layer[n] + delta(n, s)
                    if layer[s] < need:
                        layer[s] = need
                        changed = True
            if not changed:
                break
        # Pull pure feeders right, next to their first consumer, so utility
        # sheets don't all pile up in column 0 with long lines across the board
        for n in sorted(comp, key=lambda v: -layer[v]):
            if fwd[n] and not fpred[n]:
                layer[n] = min(layer[s] - delta(n, s) for s in fwd[n])

        # ── 3. Serpentine wrap for very long chains ───────────────────────────
        max_cols = max(8, math.ceil(math.sqrt(len(comp)) * 2.2))
        band, col = {}, {}
        for n in comp:
            b  = layer[n] // max_cols
            c_ = layer[n] % max_cols
            if b % 2 == 1:                 # odd bands run right→left so the
                c_ = max_cols - 1 - c_     # chain stays adjacent at the fold
            band[n], col[n] = b, c_

        # ── 4. Row order within each column: barycenter sweeps ────────────────
        cols = defaultdict(list)
        for n in comp:
            cols[(band[n], col[n])].append(n)
        order = {}
        for key in cols:
            cols[key].sort()
            for i, n in enumerate(cols[key]):
                order[n] = i
        col_keys = sorted(cols.keys())
        for sweep in range(6):
            keys = col_keys if sweep % 2 == 0 else list(reversed(col_keys))
            for key in keys:
                members = cols[key]

                def bary(n, _key=key):
                    nbs = [order[m] for m in und[n]
                           if m in cset and (band[m], col[m]) != _key]
                    return med(nbs) if nbs else float(order[n])

                members.sort(key=lambda n: (bary(n), order[n]))
                for i, n in enumerate(members):
                    order[n] = i
        # Vertical links in the same column: source stacks above target
        for (a, b) in vertical:
            if (a in cset and b in cset
                    and (band[a], col[a]) == (band[b], col[b])
                    and order[a] > order[b]):
                members = cols[(band[a], col[a])]
                members[order[a]], members[order[b]] = \
                    members[order[b]], members[order[a]]
                order[a], order[b] = order[b], order[a]

        # ── 5. Coordinates ─────────────────────────────────────────────────────
        band_top = cursor_y
        for b in range(max(band.values()) + 1):
            bcols = sorted(c2 for (bb, c2) in cols if bb == b)
            x = MARGIN
            col_x, col_w = {}, {}
            for c2 in bcols:
                wmax = max(ws[n] for n in cols[(b, c2)])
                col_x[c2], col_w[c2] = x, wmax
                x += wmax + H_GAP
            # initial y: stack in barycenter order
            cy = {}
            for c2 in bcols:
                yy = band_top
                for n in sorted(cols[(b, c2)], key=lambda n: order[n]):
                    cy[n] = yy + hs[n] / 2
                    yy += hs[n] + V_GAP
            # alignment passes: move toward neighbour average, keep order,
            # resolve overlaps top-down
            for _pass in range(5):
                for c2 in bcols:
                    members = sorted(cols[(b, c2)], key=lambda n: order[n])
                    desired = []
                    for n in members:
                        nbs = [cy[m] for m in und[n]
                               if m in cy and (band[m], col[m]) != (b, c2)]
                        desired.append(med(nbs) if nbs else cy[n])
                    prev_bottom = None
                    for n, d in zip(members, desired):
                        top = d - hs[n] / 2
                        if prev_bottom is None:
                            top = max(top, band_top)
                        elif top < prev_bottom + V_GAP:
                            top = prev_bottom + V_GAP
                        cy[n] = top + hs[n] / 2
                        prev_bottom = top + hs[n]
            band_nodes = [n for n in comp if band[n] == b]
            for n in band_nodes:
                c2 = col[n]
                px = col_x[c2] + (col_w[c2] - ws[n]) / 2   # centred in column
                pos[n] = (px, cy[n] - hs[n] / 2)
            band_top = max(pos[n][1] + hs[n] for n in band_nodes) + C_GAP * 0.7
        cursor_y = band_top - C_GAP * 0.7 + C_GAP

    # ── 6. Utility-hub row below the process flow ──────────────────────────────
    if hubs:
        hub_y, row_h = cursor_y, 0.0
        entries = []
        for h in hubs:
            nb_x = [pos[m][0] + ws[m] / 2 for m in und_full[h] if m in pos]
            entries.append((med(nb_x) if nb_x else MARGIN, h))
        entries.sort()
        x_next = MARGIN
        for cx, h in entries:
            x = max(x_next, cx - ws[h] / 2)
            pos[h] = (x, hub_y)
            x_next = x + ws[h] + H_GAP
            row_h  = max(row_h, hs[h])
        cursor_y = hub_y + row_h + C_GAP

    # ── 7. Isolated sheets: grid at the bottom ─────────────────────────────────
    if isolated:
        board_right = max((pos[n][0] + ws[n] for n in pos), default=MARGIN)
        typ_w = sum(ws.values()) / len(ws)
        target_w = max(board_right, 3 * (typ_w + H_GAP) + MARGIN)
        x, y, row_h = MARGIN, cursor_y, 0.0
        for i in isolated:
            if x > MARGIN and x + ws[i] > target_w:
                x = MARGIN
                y += row_h + V_GAP
                row_h = 0.0
            pos[i] = (x, y)
            x += ws[i] + H_GAP
            row_h = max(row_h, hs[i])

    return {i: (round(pos[i][0]), round(pos[i][1])) for i in active_pages}
