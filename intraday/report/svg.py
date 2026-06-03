"""Dependency-free inline-SVG chart primitives for the HTML dashboard.

Every function is **pure** and returns a self-contained ``<svg>...</svg>`` string
with no external references — the dashboard works fully offline (open the file
with no network). Output is **deterministic**: no randomness and no clock, so the
same data always renders byte-identical SVG (the dashboard tests rely on this).

The vocabulary is what an intraday equity report needs:

- :func:`line_chart`   — the equity curve (with an optional capital baseline)
- :func:`area_chart`   — the underwater / drawdown curve (values <= 0)
- :func:`bar_chart`    — signed daily PnL (green up / red down, zero line)
- :func:`waterfall`    — gross PnL -> costs -> net PnL attribution
- :func:`heatmap`      — a per-symbol x per-strategy diverging matrix
- :func:`sparkline`    — a tiny inline trend line for KPI cards

No matplotlib, no JS charting library: just numbers in, SVG markup out.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from html import escape

# --------------------------------------------------------------------------- #
# Palette (kept in sync with the dashboard's CSS custom properties)
# --------------------------------------------------------------------------- #
UP = "#3fb950"        # gains / positive
DOWN = "#f85149"      # losses / negative
ACCENT = "#4f9cff"    # primary line
GRID = "#2b3140"      # gridlines
AXIS = "#3d4452"      # axis / zero line
TEXT = "#9aa4b2"      # tick labels
MUTED = "#6e7681"

# Distinct, reasonably colour-blind-friendly colours for overlaying N series
# (multi_line_chart) — the matching HTML legend in comparison.py reads this list.
SERIES_PALETTE: tuple[str, ...] = (
    # ordered so the first colours have similar luminance on the dark background;
    # the dimmest red (#f85149) is last so it only appears when 8 series overlay.
    "#4f9cff", "#f0883e", "#3fb950", "#bc8cff",
    "#ff7b72", "#56d4dd", "#e3b341", "#f85149",
)


def _isfinite(x) -> bool:
    """True iff ``x`` is a finite real number (rejects NaN, ±inf, and non-numbers)."""
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _finite(values: Sequence[float]) -> list[float]:
    """Drop NaN/±inf so degenerate inputs render empty instead of collapsing to the
    origin (a NaN in min/max would otherwise poison every coordinate)."""
    return [float(v) for v in values if _isfinite(v)]


def _f(x: float) -> str:
    """Compact, deterministic float formatting for SVG coordinates. Any non-finite
    value (NaN/±inf) collapses to ``"0"`` so a stray value can never crash the
    generator (``int(inf)`` raises ``OverflowError``)."""
    if not _isfinite(x):
        return "0"
    r = round(float(x), 2)
    if r == int(r):
        return str(int(r))
    return f"{r:.2f}"


def _empty(width: int, height: int, msg: str = "no data") -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'xmlns="http://www.w3.org/2000/svg" class="chart chart--empty">'
        f'<rect width="{width}" height="{height}" fill="none"/>'
        f'<text x="{width / 2:.0f}" y="{height / 2:.0f}" fill="{MUTED}" '
        f'font-size="13" text-anchor="middle" dominant-baseline="middle">'
        f"{escape(msg)}</text></svg>"
    )


def _open(width: int, height: int, cls: str) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'xmlns="http://www.w3.org/2000/svg" class="chart {cls}">'
    )


def _y_gridlines(
    lo: float, hi: float, x0: float, x1: float, plot_top: float, plot_bot: float,
    fmt, *, n: int = 4,
) -> str:
    """Horizontal gridlines + right-edge value labels at ``n`` even levels."""
    if hi <= lo:
        return ""
    span = hi - lo
    parts: list[str] = []
    for i in range(n + 1):
        v = lo + span * i / n
        y = plot_bot - (v - lo) / span * (plot_bot - plot_top)
        parts.append(
            f'<line x1="{_f(x0)}" y1="{_f(y)}" x2="{_f(x1)}" y2="{_f(y)}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{_f(x1 + 4)}" y="{_f(y + 3)}" fill="{TEXT}" '
            f'font-size="10">{escape(fmt(v))}</text>'
        )
    return "".join(parts)


def _x_labels(labels: Sequence[str] | None, n_points: int, x0: float, x1: float, y: float) -> str:
    if not labels or n_points < 2:
        return ""
    picks = sorted({0, n_points // 2, n_points - 1})
    parts: list[str] = []
    for idx in picks:
        if idx >= len(labels):
            continue
        x = x0 + (x1 - x0) * idx / (n_points - 1)
        anchor = "start" if idx == 0 else "end" if idx == n_points - 1 else "middle"
        parts.append(
            f'<text x="{_f(x)}" y="{_f(y)}" fill="{TEXT}" font-size="10" '
            f'text-anchor="{anchor}">{escape(str(labels[idx]))}</text>'
        )
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Line chart (equity curve)
# --------------------------------------------------------------------------- #
def line_chart(
    values: Sequence[float],
    *,
    width: int = 760,
    height: int = 240,
    baseline: float | None = None,
    color: str = ACCENT,
    labels: Sequence[str] | None = None,
    value_fmt=lambda v: f"{v:,.0f}",
) -> str:
    """A filled line chart. ``baseline`` (e.g. starting capital) draws a dashed
    reference line and tints the area above/below it."""
    vals = _finite(values)
    if len(vals) < 2:
        return _empty(width, height)

    pad_l, pad_r, pad_t, pad_b = 8, 52, 12, 22
    x0, x1 = pad_l, width - pad_r
    y_top, y_bot = pad_t, height - pad_b

    lo, hi = min(vals), max(vals)
    if baseline is not None:
        lo, hi = min(lo, baseline), max(hi, baseline)
    if hi == lo:
        hi += 1.0
        lo -= 1.0

    def sx(i: int) -> float:
        return x0 + (x1 - x0) * i / (len(vals) - 1)

    def sy(v: float) -> float:
        return y_bot - (v - lo) / (hi - lo) * (y_bot - y_top)

    pts = " ".join(f"{_f(sx(i))},{_f(sy(v))}" for i, v in enumerate(vals))
    last_up = vals[-1] >= (baseline if baseline is not None else vals[0])
    stroke = UP if last_up else DOWN
    if color != ACCENT:
        stroke = color

    area = (
        f'<polygon points="{_f(x0)},{_f(y_bot)} {pts} {_f(x1)},{_f(y_bot)}" '
        f'fill="{stroke}" fill-opacity="0.10"/>'
    )
    line = f'<polyline points="{pts}" fill="none" stroke="{stroke}" stroke-width="2"/>'

    base_line = ""
    if baseline is not None:
        by = sy(baseline)
        base_line = (
            f'<line x1="{_f(x0)}" y1="{_f(by)}" x2="{_f(x1)}" y2="{_f(by)}" '
            f'stroke="{MUTED}" stroke-width="1" stroke-dasharray="4 3"/>'
            f'<text x="{_f(x1 + 4)}" y="{_f(by - 3)}" fill="{MUTED}" font-size="10">'
            f"{escape(value_fmt(baseline))}</text>"
        )

    grid = _y_gridlines(lo, hi, x0, x1, y_top, y_bot, value_fmt, n=4)
    xlab = _x_labels(labels, len(vals), x0, x1, height - 6)

    return (
        _open(width, height, "chart--line")
        + grid + area + base_line + line + xlab + "</svg>"
    )


# --------------------------------------------------------------------------- #
# Multi-line chart (overlay N series — e.g. strategy equity comparison)
# --------------------------------------------------------------------------- #
def multi_line_chart(
    series: Sequence[Sequence[float]],
    *,
    width: int = 760,
    height: int = 300,
    baseline: float | None = None,
    labels: Sequence[str] | None = None,
    colors: Sequence[str] = SERIES_PALETTE,
    value_fmt=lambda v: f"{v:,.2f}",
) -> str:
    """Overlay several series on one set of axes (no fill, for clarity). Each series
    is drawn in ``colors[i]``; the caller renders a matching legend. Series may have
    different lengths — each is mapped across the full width by its own length."""
    clean = [_finite(s) for s in series]
    drawable = [s for s in clean if len(s) >= 2]
    if not drawable:
        return _empty(width, height)

    pad_l, pad_r, pad_t, pad_b = 8, 56, 12, 22
    x0, x1 = pad_l, width - pad_r
    y_top, y_bot = pad_t, height - pad_b

    flat = [v for s in drawable for v in s]
    lo, hi = min(flat), max(flat)
    if baseline is not None:
        lo, hi = min(lo, baseline), max(hi, baseline)
    if hi == lo:
        hi += 1.0
        lo -= 1.0

    def sy(v: float) -> float:
        return y_bot - (v - lo) / (hi - lo) * (y_bot - y_top)

    grid = _y_gridlines(lo, hi, x0, x1, y_top, y_bot, value_fmt, n=4)
    base_line = ""
    if baseline is not None:
        by = sy(baseline)
        base_line = (
            f'<line x1="{_f(x0)}" y1="{_f(by)}" x2="{_f(x1)}" y2="{_f(by)}" '
            f'stroke="{MUTED}" stroke-width="1" stroke-dasharray="4 3"/>'
        )

    lines: list[str] = []
    longest = 0
    for idx, s in enumerate(clean):
        if len(s) < 2:
            continue
        longest = max(longest, len(s))
        color = colors[idx % len(colors)]
        pts = " ".join(
            f"{_f(x0 + (x1 - x0) * i / (len(s) - 1))},{_f(sy(v))}"
            for i, v in enumerate(s)
        )
        lines.append(
            f'<polyline points="{pts}" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-opacity="0.92"/>'
        )

    xlab = _x_labels(labels, longest, x0, x1, height - 6)
    return (
        _open(width, height, "chart--multiline")
        + grid + base_line + "".join(lines) + xlab + "</svg>"
    )


# --------------------------------------------------------------------------- #
# Area chart (underwater drawdown; values <= 0)
# --------------------------------------------------------------------------- #
def area_chart(
    values: Sequence[float],
    *,
    width: int = 760,
    height: int = 160,
    color: str = DOWN,
    labels: Sequence[str] | None = None,
    value_fmt=lambda v: f"{v:.1%}",
) -> str:
    """Underwater curve: ``values`` are typically <= 0 (drawdown fractions) and the
    fill drops from the zero line. The scale is adaptive, so a stray positive value
    is shown faithfully (above zero) rather than silently squashed into [-1, 0]."""
    vals = _finite(values)
    if len(vals) < 2:
        return _empty(width, height)

    pad_l, pad_r, pad_t, pad_b = 8, 52, 12, 22
    x0, x1 = pad_l, width - pad_r
    y_top, y_bot = pad_t, height - pad_b

    lo = min(min(vals), 0.0)
    hi = max(max(vals), 0.0)
    if lo == hi:           # all zero (no drawdown): show a small band below the line
        lo = -1.0

    def sx(i: int) -> float:
        return x0 + (x1 - x0) * i / (len(vals) - 1)

    def sy(v: float) -> float:
        return y_top + (hi - v) / (hi - lo) * (y_bot - y_top)

    pts = " ".join(f"{_f(sx(i))},{_f(sy(v))}" for i, v in enumerate(vals))
    zero_y = sy(0.0)
    area = (
        f'<polygon points="{_f(x0)},{_f(zero_y)} {pts} {_f(x1)},{_f(zero_y)}" '
        f'fill="{color}" fill-opacity="0.18"/>'
    )
    line = f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5"/>'
    zero = (
        f'<line x1="{_f(x0)}" y1="{_f(zero_y)}" x2="{_f(x1)}" y2="{_f(zero_y)}" '
        f'stroke="{AXIS}" stroke-width="1"/>'
    )
    grid = _y_gridlines(lo, hi, x0, x1, y_top, y_bot, value_fmt, n=3)
    xlab = _x_labels(labels, len(vals), x0, x1, height - 6)
    return _open(width, height, "chart--area") + grid + zero + area + line + xlab + "</svg>"


# --------------------------------------------------------------------------- #
# Bar chart (signed daily PnL)
# --------------------------------------------------------------------------- #
def bar_chart(
    values: Sequence[float],
    *,
    width: int = 760,
    height: int = 200,
    labels: Sequence[str] | None = None,
    value_fmt=lambda v: f"{v:,.0f}",
) -> str:
    """Signed bars around a zero line; positive green, negative red."""
    vals = _finite(values)
    if not vals:
        return _empty(width, height)

    pad_l, pad_r, pad_t, pad_b = 8, 52, 12, 22
    x0, x1 = pad_l, width - pad_r
    y_top, y_bot = pad_t, height - pad_b

    hi = max(max(vals), 0.0)
    lo = min(min(vals), 0.0)
    if hi == lo:
        hi, lo = 1.0, -1.0

    def sy(v: float) -> float:
        return y_bot - (v - lo) / (hi - lo) * (y_bot - y_top)

    zero_y = sy(0.0)
    n = len(vals)
    slot = (x1 - x0) / n
    bw = max(slot * 0.7, 0.5)
    bars: list[str] = []
    for i, v in enumerate(vals):
        cx = x0 + slot * (i + 0.5)
        y = sy(v)
        top = min(y, zero_y)
        h = abs(y - zero_y)
        fill = UP if v >= 0 else DOWN
        bars.append(
            f'<rect x="{_f(cx - bw / 2)}" y="{_f(top)}" width="{_f(bw)}" '
            f'height="{_f(max(h, 0.5))}" fill="{fill}"/>'
        )
    zero = (
        f'<line x1="{_f(x0)}" y1="{_f(zero_y)}" x2="{_f(x1)}" y2="{_f(zero_y)}" '
        f'stroke="{AXIS}" stroke-width="1"/>'
    )
    grid = _y_gridlines(lo, hi, x0, x1, y_top, y_bot, value_fmt, n=4)
    xlab = _x_labels(labels, n, x0, x1, height - 6)
    return _open(width, height, "chart--bars") + grid + "".join(bars) + zero + xlab + "</svg>"


# --------------------------------------------------------------------------- #
# Waterfall (gross -> costs -> net)
# --------------------------------------------------------------------------- #
def waterfall(
    steps: Sequence[tuple[str, float, str]],
    *,
    width: int = 480,
    height: int = 240,
    value_fmt=lambda v: f"${v:,.0f}",
) -> str:
    """Cost-attribution waterfall.

    ``steps`` is a list of ``(label, value, kind)`` where ``kind`` is one of
    ``"total"`` (a bar anchored at zero), ``"delta"`` (a floating change from the
    running cumulative), with sign driving colour. The classic call is::

        waterfall([("gross", g, "total"), ("costs", -c, "delta"), ("net", n, "total")])
    """
    if not steps:
        return _empty(width, height)
    # Non-finite deltas would poison the running total; treat them as 0.
    steps = [(label, (float(val) if _isfinite(val) else 0.0), kind) for label, val, kind in steps]

    pad_l, pad_r, pad_t, pad_b = 8, 56, 16, 28
    x0, x1 = pad_l, width - pad_r
    y_top, y_bot = pad_t, height - pad_b

    # Resolve each bar's (bottom, top) in value-space and track the running total.
    running = 0.0
    spans: list[tuple[float, float]] = []
    for _, val, kind in steps:
        if kind == "total":
            spans.append((min(0.0, val), max(0.0, val)))
            running = val
        else:  # delta
            new = running + val
            spans.append((min(running, new), max(running, new)))
            running = new

    hi = max(t for _, t in spans)
    lo = min(b for b, _ in spans)
    hi = max(hi, 0.0)
    lo = min(lo, 0.0)
    if hi == lo:
        hi, lo = 1.0, -1.0

    def sy(v: float) -> float:
        return y_bot - (v - lo) / (hi - lo) * (y_bot - y_top)

    zero_y = sy(0.0)
    n = len(steps)
    slot = (x1 - x0) / n
    bw = max(slot * 0.55, 1.0)
    parts: list[str] = []
    grid = _y_gridlines(lo, hi, x0, x1, y_top, y_bot, value_fmt, n=4)
    parts.append(grid)

    prev_top_x = None
    prev_running = 0.0
    for i, ((label, val, kind), (b, t)) in enumerate(zip(steps, spans)):
        cx = x0 + slot * (i + 0.5)
        y_t, y_b = sy(t), sy(b)
        if kind == "total":
            fill = UP if val >= 0 else DOWN
        else:
            fill = UP if val >= 0 else DOWN
        parts.append(
            f'<rect x="{_f(cx - bw / 2)}" y="{_f(y_t)}" width="{_f(bw)}" '
            f'height="{_f(max(y_b - y_t, 0.5))}" fill="{fill}" fill-opacity="0.9"/>'
        )
        # connector line from previous cumulative level
        if prev_top_x is not None:
            cy = sy(prev_running)
            parts.append(
                f'<line x1="{_f(prev_top_x)}" y1="{_f(cy)}" x2="{_f(cx - bw / 2)}" '
                f'y2="{_f(cy)}" stroke="{MUTED}" stroke-width="1" stroke-dasharray="3 2"/>'
            )
        prev_running = val if kind == "total" else prev_running + val
        prev_top_x = cx + bw / 2
        # label + value
        parts.append(
            f'<text x="{_f(cx)}" y="{_f(height - 14)}" fill="{TEXT}" font-size="10" '
            f'text-anchor="middle">{escape(label)}</text>'
        )
        vy = y_t - 4 if val >= 0 else y_b + 11
        parts.append(
            f'<text x="{_f(cx)}" y="{_f(vy)}" fill="{TEXT}" font-size="9.5" '
            f'text-anchor="middle">{escape(value_fmt(val))}</text>'
        )
    zero = (
        f'<line x1="{_f(x0)}" y1="{_f(zero_y)}" x2="{_f(x1)}" y2="{_f(zero_y)}" '
        f'stroke="{AXIS}" stroke-width="1"/>'
    )
    parts.append(zero)
    return _open(width, height, "chart--waterfall") + "".join(parts) + "</svg>"


# --------------------------------------------------------------------------- #
# Heatmap (per-symbol x per-strategy)
# --------------------------------------------------------------------------- #
def _diverging(t: float) -> str:
    """Map ``t`` in [-1, 1] to a red(-)/slate(0)/green(+) hex colour."""
    # NaN/inf would survive min/max clamping (NaN comparisons are falsy) and round()
    # to an unpredictable channel value — treat a non-finite ratio as the neutral mid.
    if not _isfinite(t):
        t = 0.0
    t = max(-1.0, min(1.0, t))
    neg = (248, 81, 73)     # DOWN
    mid = (45, 51, 64)      # slate
    pos = (63, 185, 80)     # UP
    if t >= 0:
        a, b, f = mid, pos, t
    else:
        a, b, f = mid, neg, -t
    r = round(a[0] + (b[0] - a[0]) * f)
    g = round(a[1] + (b[1] - a[1]) * f)
    bl = round(a[2] + (b[2] - a[2]) * f)
    return f"#{r:02x}{g:02x}{bl:02x}"


def heatmap(
    matrix: Sequence[Sequence[float | None]],
    row_labels: Sequence[str],
    col_labels: Sequence[str],
    *,
    cell: int = 54,
    row_label_w: int = 84,
    col_label_h: int = 26,
    value_fmt=lambda v: f"{v:.2f}",
    vmax: float | None = None,
) -> str:
    """A labelled diverging heatmap. Colour is scaled by ``vmax`` (or the matrix's
    max absolute value), centred at zero. Empty cells (``None``) render blank."""
    if not matrix or not matrix[0]:
        return _empty(360, 120, "no matrix")

    n_rows, n_cols = len(matrix), len(matrix[0])
    width = row_label_w + n_cols * cell + 8
    height = col_label_h + n_rows * cell + 8

    # A non-finite vmax (NaN/inf) is truthy, so it would slip past ``scale or 1.0``
    # and poison every cell's colour ratio — fall back to the data-derived scale.
    if not _isfinite(vmax):
        vmax = None
    flat = [abs(float(v)) for row in matrix for v in row if v is not None and v == v]
    scale = vmax if vmax is not None else (max(flat) if flat else 1.0)
    scale = scale or 1.0

    parts: list[str] = [_open(width, height, "chart--heatmap")]
    # column headers
    for j, c in enumerate(col_labels):
        x = row_label_w + j * cell + cell / 2
        parts.append(
            f'<text x="{_f(x)}" y="{_f(col_label_h - 8)}" fill="{TEXT}" '
            f'font-size="11" text-anchor="middle">{escape(str(c))}</text>'
        )
    for i, r in enumerate(row_labels):
        y = col_label_h + i * cell
        parts.append(
            f'<text x="{_f(row_label_w - 8)}" y="{_f(y + cell / 2 + 4)}" fill="{TEXT}" '
            f'font-size="11" text-anchor="end">{escape(str(r))}</text>'
        )
        for j in range(n_cols):
            v = matrix[i][j] if j < len(matrix[i]) else None
            x = row_label_w + j * cell
            if v is None or v != v:
                parts.append(
                    f'<rect x="{_f(x)}" y="{_f(y)}" width="{cell - 2}" '
                    f'height="{cell - 2}" fill="{GRID}" fill-opacity="0.4"/>'
                )
                continue
            color = _diverging(float(v) / scale)
            parts.append(
                f'<rect x="{_f(x)}" y="{_f(y)}" width="{cell - 2}" height="{cell - 2}" '
                f'rx="3" fill="{color}"/>'
            )
            parts.append(
                f'<text x="{_f(x + cell / 2)}" y="{_f(y + cell / 2 + 4)}" fill="#e6edf3" '
                f'font-size="10.5" text-anchor="middle">{escape(value_fmt(float(v)))}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Sparkline (tiny KPI-card trend)
# --------------------------------------------------------------------------- #
def sparkline(
    values: Sequence[float], *, width: int = 120, height: int = 32, color: str = ACCENT
) -> str:
    vals = _finite(values)
    if len(vals) < 2:
        return _empty(width, height, "")
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi += 1.0
    pad = 2

    def sx(i: int) -> float:
        return pad + (width - 2 * pad) * i / (len(vals) - 1)

    def sy(v: float) -> float:
        return (height - pad) - (v - lo) / (hi - lo) * (height - 2 * pad)

    pts = " ".join(f"{_f(sx(i))},{_f(sy(v))}" for i, v in enumerate(vals))
    stroke = UP if vals[-1] >= vals[0] else DOWN
    if color != ACCENT:
        stroke = color
    return (
        _open(width, height, "chart--spark")
        + f'<polyline points="{pts}" fill="none" stroke="{stroke}" stroke-width="1.5"/>'
        + "</svg>"
    )
