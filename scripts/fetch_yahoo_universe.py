"""Fetch free Yahoo Finance intraday bars for a universe → data_raw/yahoo/.

A polite, reads-only data-acquisition utility (no Theta, no broker). Yahoo's chart
API serves ~60 days of 5-minute bars and ~2 years of hourly, free. Timestamps are
bar-START epoch seconds (UTC) — the engine's IBKR/Yahoo remap shifts them to the
canonical CLOSE grid (see intraday.data._remap). Writes the raw JSON per symbol so
the deterministic ingest step (scripts.ingest_yahoo_universe) can replay offline.

Run::

    python -m scripts.fetch_yahoo_universe                       # default universe, 60d/5m
    python -m scripts.fetch_yahoo_universe --symbols SPY QQQ --range 60d --interval 5m

Politeness: a browser User-Agent (Yahoo 429s bare clients), exponential backoff on
429/5xx, and a small inter-symbol delay. Network-only; never imports the engine.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

# Liquid, high-volume cross-section: broad/sector ETFs + large-cap single names.
DEFAULT_UNIVERSE: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "SMH", "GLD", "TLT",
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "NFLX", "JPM", "XOM", "AVGO",
)
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def fetch_chart(symbol: str, rng: str, interval: str, *, retries: int = 5) -> dict:
    """Fetch one Yahoo chart payload with UA + exponential backoff."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range={rng}&interval={interval}&includePrePost=false"
    )
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:  # 429 / 5xx → back off
            last_err = e
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2.0 * (2 ** attempt))
                continue
            raise
        except Exception as e:  # transient network
            last_err = e
            if attempt < retries - 1:
                time.sleep(2.0 * (2 ** attempt))
                continue
            raise
    raise RuntimeError(f"failed to fetch {symbol}: {last_err}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fetch free Yahoo intraday bars for a universe.")
    p.add_argument("--symbols", nargs="*", default=list(DEFAULT_UNIVERSE))
    p.add_argument("--range", dest="rng", default="60d")
    p.add_argument("--interval", default="5m")
    p.add_argument("--out-dir", default="data_raw/yahoo", type=Path)
    p.add_argument("--delay", type=float, default=1.5, help="seconds between symbols (politeness)")
    args = p.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, sym in enumerate(args.symbols):
        rec = {"symbol": sym, "range": args.rng, "interval": args.interval}
        try:
            payload = fetch_chart(sym, args.rng, args.interval)
            res = payload["chart"]["result"][0]
            ts = res.get("timestamp") or []
            out = args.out_dir / f"{sym}_{args.interval}_{args.rng}.json"
            out.write_text(json.dumps(payload))
            rec.update(bars=len(ts), first=ts[0] if ts else None, last=ts[-1] if ts else None,
                       file=str(out))
            print(f"{sym:6s} {len(ts):5d} bars -> {out.name}")
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            print(f"{sym:6s} ERROR {rec['error']}")
        manifest.append(rec)
        if i < len(args.symbols) - 1:
            time.sleep(args.delay)

    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    ok = sum(1 for r in manifest if "error" not in r)
    print(f"\nfetched {ok}/{len(args.symbols)} symbols -> {args.out_dir} (manifest.json written)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
