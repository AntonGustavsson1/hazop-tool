"""Retired "Smart polylinje" (a.k.a. "Smart Polygon") pipe-tracing feature
(removed from the active app 2026-08-26, see NOTES.md "Riv Smart
Polylinje/SmartPipeTracer ur den aktiva applikationen").

This is the ENTIRE, UNMODIFIED implementation of `SmartPipeTracer`: the
class that used to back the "Smart polylinje" markup tool in the P&ID
node-markup toolbar (`PropertiesRibbon._MARKUP_TOOLS`, see node_markup.py)
and red-markup toolbar (already trimmed away from `RedMarkupPanel._TOOLS`
before this removal, 2026-08-17). The user clicked a start point then an
end point on the P&ID canvas; `SmartPipeTracer.trace()` ran an A* search
over the rendered page's dark pixels to propose one or more candidate
pipe-path polylines between the two points, shown as a dashed preview the
user could cycle through with the left/right arrow keys and commit with
Enter (saved as a regular polyline markup) or cancel with Escape/right
click.

`SmartPipeTracer` is copied verbatim out of pid_viewer.py, where it lived
since before the 2026-08-17/18 module split. Nothing below has been
edited beyond adding this docstring and the `import fitz` it needs (it
used to get `fitz` as a plain module global from pid_viewer.py).

What drove the removal, per Anton's request ("Ta bort Smart Polygon —
Riv den befintliga Smart Polygon-funktionen som används när objekt
markeras/kopplas på P&ID"): "Smart Polygon" does not exist as a literal
name anywhere in the codebase — the only "smart" + shape/path feature
that matches "used when objects are marked/linked on the P&ID" is this
one, informally named ("Smart polylinje" in the UI, polyline not
polygon). Confirmed via a repo-wide grep before anything was touched.
Note this is unrelated to the separately-retired "Smart layout"
(`archive/smart_layout.py`, sheet-to-sheet board auto-arrangement,
removed the same day) — that removal's own NOTES.md entry explicitly
called out that `SmartPipeTracer` was NOT touched at the time, since the
two "Smart ..." names refer to completely different features.

Removed from the active app along with SmartPipeTracer:
- `MODE_SMART_POLYLINE` (pid_viewer.py) — the draw-mode constant that
  selected this tool.
- All `_smart_*` state/methods on `PIDGraphicsView`
  (pid_graphics_view.py): `_smart_start_pdf`/`_smart_end_pdf`/
  `_smart_paths`/`_smart_path_idx`/`_smart_preview`/`_smart_tracer`/
  `_smart_tracer_page` init state, `_clear_smart_preview`/
  `_draw_smart_marker`/`_run_smart_trace`/`_show_smart_path`/
  `_confirm_smart`/`_cancel_smart`, the `MODE_SMART_POLYLINE` branches in
  `set_mode`/`mousePressEvent`/`keyPressEvent`.
- The `'smart'` toolbar entry in `PropertiesRibbon._MARKUP_TOOLS` and
  `_StylePopup._TOOL_NAMES` (node_markup.py) — this was the only
  clickable button exposing the tool.
- The `elif name == 'smart':` icon-drawing branch in `_mk_pm()`
  (pid_viewer.py) — a repo-wide grep confirmed nothing else called
  `_mk_pm`/`_mk_icon` with `'smart'` once the toolbar entry above was
  gone, so the glyph-drawing code became unreachable too.
- The `'smart': MODE_SMART_POLYLINE` entries in both mode-maps of
  `PIDPanel._set_markup_tool` (pid_panel_mod.py) — including the 'red'
  map entry, which was already dead code before this removal since
  `RedMarkupPanel._TOOLS` had been trimmed to just select/symbol back on
  2026-08-17.

To reconnect this feature to the app again:
1. Re-add `MODE_SMART_POLYLINE = 12` to pid_viewer.py (or reuse this
   module's own class directly — it's self-contained, only needs
   `fitz`).
2. Re-add the `_smart_*` state/methods to `PIDGraphicsView` and the
   `MODE_SMART_POLYLINE` branches in `set_mode`/`mousePressEvent`/
   `keyPressEvent` (pid_graphics_view.py) — see git history around the
   2026-08-26 removal commit for the exact code.
3. Re-add the `'smart'` entry to `PropertiesRibbon._MARKUP_TOOLS`/
   `_StylePopup._TOOL_NAMES` (node_markup.py) and to
   `PIDPanel._set_markup_tool`'s mode-map(s) (pid_panel_mod.py).
"""

import fitz


class SmartPipeTracer:
    """
    Traces pipe paths on a rendered P&ID page using A* on a greyscale image.
    Works on both colour and B&W P&IDs; detects dark pixels as pipe material.
    Gap-jumping handles crossings drawn with a small break between lines.
    """
    DARK_THRESHOLD = 110    # pixels darker than this count as "pipe"
    TRACE_SCALE    = 1.0    # render resolution multiplier for tracing
    MAX_GAP        = 7      # max white-pixel gap to jump across (crossing style)
    MAX_EXPLORE    = 300_000  # A* node limit (safety)
    GOAL_RADIUS_SQ = 25     # squared pixel distance that counts as "reached end"

    def __init__(self, pdf_doc, page_n):
        page = pdf_doc[page_n]
        mat  = fitz.Matrix(self.TRACE_SCALE, self.TRACE_SCALE)
        pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        self.width  = pix.width
        self.height = pix.height
        self._data  = bytearray(pix.samples)   # flat greyscale bytes
        self._tmask = self._build_text_mask(page)   # pixels inside text bboxes

    # ------------------------------------------------------------------ helpers

    def _build_text_mask(self, page):
        """
        Return a bytearray (same size as image) where 1 = inside a text bbox.
        Uses PyMuPDF text extraction — works on vector P&IDs natively.
        For scanned rasters with no text layer the result is all-zero (no effect).
        """
        mask = bytearray(self.width * self.height)
        pad  = max(2, int(4 * self.TRACE_SCALE))  # pixel padding around each bbox
        try:
            blocks = page.get_text("dict", flags=0)["blocks"]
            for block in blocks:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        bx0, by0, bx1, by1 = span["bbox"]
                        # Convert PDF coords → tracer pixel coords and expand
                        px0 = max(0,            int(bx0 * self.TRACE_SCALE) - pad)
                        py0 = max(0,            int(by0 * self.TRACE_SCALE) - pad)
                        px1 = min(self.width,   int(bx1 * self.TRACE_SCALE) + pad + 1)
                        py1 = min(self.height,  int(by1 * self.TRACE_SCALE) + pad + 1)
                        W = self.width
                        for y in range(py0, py1):
                            base = y * W
                            for x in range(px0, px1):
                                mask[base + x] = 1
        except Exception:
            pass  # if extraction fails, proceed without masking (graceful degradation)
        return mask

    def _is_dark(self, x, y):
        """Raw dark-pixel check (no text exclusion)."""
        if 0 <= x < self.width and 0 <= y < self.height:
            return self._data[y * self.width + x] < self.DARK_THRESHOLD
        return False

    def _is_pipe(self, x, y):
        """True only if pixel is dark AND not inside a text bounding box."""
        if 0 <= x < self.width and 0 <= y < self.height:
            idx = y * self.width + x
            return self._data[idx] < self.DARK_THRESHOLD and not self._tmask[idx]
        return False

    def _nearest_dark(self, x, y, radius=15):
        """Spiral-search for nearest dark pixel within radius."""
        x, y = int(x), int(y)
        if self._is_pipe(x, y):
            return (x, y)
        best, best_d2 = None, radius * radius + 1
        for r in range(1, radius + 1):
            for dx in range(-r, r + 1):
                for sign in (-1, 1):
                    nx, ny = x + dx, y + sign * r
                    if 0 <= nx < self.width and 0 <= ny < self.height:
                        d2 = dx * dx + r * r
                        if d2 < best_d2 and self._is_pipe(nx, ny):
                            best, best_d2 = (nx, ny), d2
                for dy in range(-r + 1, r):
                    ny, nx = y + dy, x + sign * r
                    if 0 <= nx < self.width and 0 <= ny < self.height:
                        d2 = r * r + dy * dy
                        if d2 < best_d2 and self._is_pipe(nx, ny):
                            best, best_d2 = (nx, ny), d2
        return best

    def _reconstruct(self, came_from, node):
        path = []
        cur  = node
        while cur is not None:
            path.append(cur)
            cur = came_from[cur]
        path.reverse()
        return path

    def _rdp(self, pts, eps=2.5):
        """Iterative Ramer-Douglas-Peucker path simplification."""
        if len(pts) < 3:
            return list(pts)
        keep  = {0, len(pts) - 1}
        stack = [(0, len(pts) - 1)]
        while stack:
            lo, hi = stack.pop()
            if hi - lo < 2:
                continue
            x1, y1 = pts[lo]
            x2, y2 = pts[hi]
            dx, dy  = x2 - x1, y2 - y1
            d2      = dx * dx + dy * dy
            max_d, max_i = 0.0, lo
            for i in range(lo + 1, hi):
                px, py = pts[i]
                if d2 == 0:
                    dist = ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
                else:
                    t    = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / d2))
                    dist = ((px - x1 - t * dx) ** 2 + (py - y1 - t * dy) ** 2) ** 0.5
                if dist > max_d:
                    max_d, max_i = dist, i
            if max_d > eps:
                keep.add(max_i)
                stack.append((lo, max_i))
                stack.append((max_i, hi))
        return [pts[i] for i in sorted(keep)]

    # ------------------------------------------------------------------ core A*

    def _astar(self, start, end, blocked):
        import heapq
        from itertools import count
        _cnt = count()

        ex, ey = end
        def h(x, y): return ((x - ex) ** 2 + (y - ey) ** 2) ** 0.5

        open_h    = [(h(*start), next(_cnt), start)]
        g         = {start: 0.0}
        came_from = {start: None}
        explored  = 0
        W = self.width
        data  = self._data
        tmask = self._tmask
        thr   = self.DARK_THRESHOLD

        while open_h and explored < self.MAX_EXPLORE:
            _, _, cur = heapq.heappop(open_h)
            explored += 1
            cx, cy = cur

            if (cx - ex) ** 2 + (cy - ey) ** 2 <= self.GOAL_RADIUS_SQ:
                return self._reconstruct(came_from, cur)

            gc = g[cur]
            prev = came_from.get(cur)

            # --- 8-directional dark neighbours ---
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == dy == 0:
                        continue
                    nx, ny = cx + dx, cy + dy
                    if nx < 0 or nx >= self.width or ny < 0 or ny >= self.height:
                        continue
                    if data[ny * W + nx] >= thr or tmask[ny * W + nx]:
                        continue
                    nb = (nx, ny)
                    if nb in blocked:
                        continue
                    step = 1.414 if dx and dy else 1.0
                    ng   = gc + step
                    if ng < g.get(nb, 1e18):
                        g[nb]         = ng
                        came_from[nb] = cur
                        f = ng + h(nx, ny)
                        heapq.heappush(open_h, (f, next(_cnt), nb))

            # --- gap jump: continue in last movement direction across short gap ---
            if prev is not None:
                pdx = cx - prev[0]
                pdy = cy - prev[1]
                ndx = (pdx // abs(pdx)) if pdx else 0
                ndy = (pdy // abs(pdy)) if pdy else 0
                if ndx or ndy:
                    # check if current pixel is at the edge of a dark region
                    step_base = 1.414 if ndx and ndy else 1.0
                    for gap in range(2, self.MAX_GAP + 1):
                        jx, jy = cx + ndx * gap, cy + ndy * gap
                        if not (0 <= jx < self.width and 0 <= jy < self.height):
                            break
                        if data[jy * W + jx] < thr and not tmask[jy * W + jx]:
                            jb = (jx, jy)
                            if jb not in blocked:
                                ng = gc + gap * step_base * 0.85  # slight bonus for jumping
                                if ng < g.get(jb, 1e18):
                                    g[jb]         = ng
                                    came_from[jb] = cur
                                    f = ng + h(jx, jy)
                                    heapq.heappush(open_h, (f, next(_cnt), jb))
                            break  # only jump to first dark pixel found

        return None  # path not found

    # ------------------------------------------------------------------ public

    def trace(self, start_pdf, end_pdf, n_alt=2):
        """
        Find up to (n_alt+1) paths from start_pdf to end_pdf.
        Returns list of paths; each path = list of [pdf_x, pdf_y].
        First element is the best path.  Empty list = nothing found.
        """
        sx = int(start_pdf[0] * self.TRACE_SCALE)
        sy = int(start_pdf[1] * self.TRACE_SCALE)
        ex = int(end_pdf[0]   * self.TRACE_SCALE)
        ey = int(end_pdf[1]   * self.TRACE_SCALE)

        start = self._nearest_dark(sx, sy, 15)
        end   = self._nearest_dark(ex, ey, 15)
        if not start or not end:
            return []

        results = []
        blocked = set()

        for _ in range(n_alt + 1):
            px_path = self._astar(start, end, frozenset(blocked))
            if not px_path:
                break
            simplified  = self._rdp(px_path, eps=2.5)
            pdf_path    = [[x / self.TRACE_SCALE, y / self.TRACE_SCALE]
                           for x, y in simplified]
            results.append(pdf_path)
            # Block the middle half of this path so next search takes a different route
            lo = len(px_path) // 4
            hi = 3 * len(px_path) // 4
            blocked.update(px_path[lo:hi])

        return results
