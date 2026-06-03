"""Shared HTML shell + stylesheet for every report page (dashboard / comparison / index).

Centralising the CSS and the ``<!DOCTYPE>`` document wrapper here keeps the three
page builders visually consistent and DRY — one stylesheet, one shell, no drift.
All styles are inlined by :func:`document`; nothing is fetched from the network.
"""

from __future__ import annotations

from html import escape

CSS = """
:root{--bg:#0d1117;--card:#161b22;--card2:#1c222b;--bd:#2b3140;--fg:#e6edf3;
--mut:#9aa4b2;--pos:#3fb950;--neg:#f85149;--acc:#4f9cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 64px}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 14px;
border-bottom:1px solid var(--bd);padding-bottom:8px}
h3{font-size:13px;margin:18px 0 8px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--mut);margin:0 0 18px;font-size:13px}
.pos{color:var(--pos)}.neg{color:var(--neg)}.muted{color:var(--mut);font-size:12.5px}
.banner{padding:10px 14px;border-radius:8px;margin:0 0 18px;font-size:13px;font-weight:600}
.banner--synthetic{background:rgba(248,81,73,.12);border:1px solid var(--neg);color:#ffb3ae}
.banner--real{background:rgba(63,185,80,.10);border:1px solid var(--pos);color:#9be8a8}
.verdict{display:flex;align-items:center;gap:14px;padding:16px 18px;border-radius:10px;
margin:0 0 22px;flex-wrap:wrap}
.verdict--edge{background:rgba(63,185,80,.10);border:1px solid var(--pos)}
.verdict--noedge{background:rgba(248,81,73,.08);border:1px solid var(--neg)}
.verdict__tag{font-size:20px;font-weight:800;letter-spacing:.02em}
.verdict--edge .verdict__tag{color:var(--pos)}.verdict--noedge .verdict__tag{color:var(--neg)}
.verdict__detail{color:var(--mut);font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:0 0 24px}
@media(max-width:760px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:13px 14px}
.kpi__label{color:var(--mut);font-size:11.5px;text-transform:uppercase;letter-spacing:.04em}
.kpi__value{font-size:21px;font-weight:700;margin:3px 0 2px}
.kpi__sub{color:var(--mut);font-size:11.5px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;
padding:18px 20px;margin:0 0 22px}
.chart{display:block;margin:4px 0}
.split{display:grid;grid-template-columns:1.3fr 1fr;gap:20px;align-items:center}
@media(max-width:760px){.split{grid-template-columns:1fr}}
.facts{list-style:none;margin:0;padding:0}
.facts li{display:flex;justify-content:space-between;padding:6px 0;
border-bottom:1px solid var(--bd);font-size:13px}
.facts li span{font-weight:700}
.facts--wide{display:grid;grid-template-columns:repeat(4,1fr);gap:0 18px}
@media(max-width:760px){.facts--wide{grid-template-columns:repeat(2,1fr)}}
table{width:100%;border-collapse:collapse;font-size:12.5px}
.scorecard td,.scorecard th{text-align:left;padding:8px 10px;border-bottom:1px solid var(--bd)}
.scorecard th{color:var(--mut);font-weight:600;font-size:11.5px;text-transform:uppercase}
.sc__name{font-weight:600}.sc__val{font-weight:700;white-space:nowrap}
.sc__note{color:var(--mut);font-size:12px}
.sc__verdict td{border-top:2px solid var(--bd);font-size:13px}
.blotter{font-variant-numeric:tabular-nums}
.blotter td,.blotter th{padding:5px 8px;border-bottom:1px solid var(--bd)}
.blotter th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;position:sticky;top:0;background:var(--card)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.scroll-y{max-height:420px;overflow:auto;border:1px solid var(--bd);border-radius:8px}
.scroll-x{overflow-x:auto}
.exitreasons{list-style:none;margin:8px 0 0;padding:0}
.exitreasons li{display:grid;grid-template-columns:140px 1fr 44px;gap:10px;align-items:center;
padding:4px 0;font-size:12.5px}
.er__bar{background:var(--card2);border-radius:4px;height:10px;overflow:hidden}
.er__bar span{display:block;height:100%;background:var(--acc)}
.er__count{text-align:right;color:var(--mut)}
.foot{color:var(--mut);font-size:12px;margin-top:8px;border-top:1px solid var(--bd);padding-top:14px}
.foot code{background:var(--card2);padding:1px 5px;border-radius:4px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:22px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
/* legend (shared by multi-series charts) */
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:10px 0 2px;font-size:12.5px;color:var(--mut)}
.legend__item{display:flex;align-items:center;gap:6px}
.legend__swatch{width:11px;height:11px;border-radius:3px;display:inline-block}
/* comparison table */
.cmp td,.cmp th{padding:8px 10px;border-bottom:1px solid var(--bd);text-align:right;white-space:nowrap}
.cmp th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase}
.cmp td:first-child,.cmp th:first-child{text-align:left}
.cmp tr.cmp-best{background:rgba(63,185,80,.07)}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700}
.badge--edge{background:rgba(63,185,80,.15);color:var(--pos);border:1px solid var(--pos)}
.badge--noedge{background:rgba(248,81,73,.12);color:var(--neg);border:1px solid var(--neg)}
.badge--synthetic{background:rgba(248,81,73,.10);color:#ffb3ae;border:1px solid var(--neg)}
.badge--real{background:rgba(63,185,80,.10);color:#9be8a8;border:1px solid var(--pos)}
/* index page */
.idx{list-style:none;margin:0;padding:0}
.idx li{display:grid;grid-template-columns:1fr auto auto;gap:14px;align-items:center;
padding:11px 4px;border-bottom:1px solid var(--bd)}
.idx .idx__title{font-weight:600}
.idx .idx__meta{color:var(--mut);font-size:12px}
"""


def document(title: str, body: str, *, lang: str = "en") -> str:
    """Wrap ``body`` HTML in the standard offline document shell (inlined CSS)."""
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{escape(lang)}"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>{CSS}</style></head>\n"
        '<body><div class="wrap">\n'
        f"{body}\n"
        "</div></body></html>\n"
    )
