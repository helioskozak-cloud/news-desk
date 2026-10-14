"""
fetch_indices.py — the market strip on every regime-desk tab (owner, 2026-10-14,
Q65 = a: "a proper display of top indices ... like tipranks.com").

Ten instruments, one Yahoo request for the daily closes and one for today's
5-minute path, every refresh (about every 5 minutes). Writes
docs/data/indices.json:

  { "generated", "indices": [{key, label, kind, last, prev, change, change_pct,
                              as_of, path: [[epoch_s, price], ...]}, ...] }

kind "yield" is the 10-year Treasury in percent: its change is in basis
points (change_bp) and it has no percent change. An instrument Yahoo did not
return is left out rather than shown at a stale or zero level.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

OUT = Path(__file__).parent.parent / "docs" / "data" / "indices.json"
STRIP = [  # (Yahoo symbol, label, kind)
    ("^GSPC", "S&P 500", "index"),
    ("^DJI", "Dow", "index"),
    ("^IXIC", "Nasdaq", "index"),
    ("^RUT", "Russell 2K", "index"),
    ("^VIX", "VIX", "index"),
    ("^TNX", "10-yr yield", "yield"),
    ("CL=F", "Oil (WTI)", "price"),
    ("GC=F", "Gold", "price"),
    ("BTC-USD", "Bitcoin", "price"),
    ("DX-Y.NYB", "Dollar", "index"),
]
MAX_POINTS = 80


def thin(s: pd.Series) -> list:
    s = s.dropna()
    if len(s) > MAX_POINTS:
        step = -(-len(s) // MAX_POINTS)
        s = pd.concat([s.iloc[::step], s.iloc[-1:]])
        s = s[~s.index.duplicated(keep="last")]
    return [[int(ts.timestamp()), round(float(v), 4)] for ts, v in s.items()]


def build(daily: pd.DataFrame, intraday: pd.DataFrame) -> list[dict]:
    out = []
    for sym, label, kind in STRIP:
        if sym not in daily.columns:
            continue
        d = daily[sym].dropna()
        i = intraday[sym].dropna() if sym in intraday.columns else pd.Series(dtype=float)
        if d.empty and i.empty:
            continue
        last = float(i.iloc[-1]) if not i.empty else float(d.iloc[-1])
        when = i.index[-1] if not i.empty else d.index[-1]
        # The previous close is the last daily bar BEFORE the session the
        # latest price belongs to.
        session = pd.Timestamp(when).tz_localize(None).normalize() if pd.Timestamp(when).tzinfo else pd.Timestamp(when).normalize()
        earlier = d[d.index.tz_localize(None).normalize() < session] if d.index.tz is not None else d[d.index.normalize() < session]
        if earlier.empty:
            continue
        prev = float(earlier.iloc[-1])
        row = {"key": sym, "label": label, "kind": kind, "last": round(last, 4), "prev": round(prev, 4),
               "as_of": pd.Timestamp(when).isoformat(), "path": thin(i)}
        if kind == "yield":
            row["change_bp"] = round((last - prev) * 100, 1)
        else:
            row["change"] = round(last - prev, 4)
            row["change_pct"] = round((last / prev - 1) * 100, 2) if prev else None
        out.append(row)
    return out


def main() -> int:
    syms = [s for s, _, _ in STRIP]
    try:
        daily = yf.download(syms, period="7d", interval="1d", progress=False, auto_adjust=False)["Close"]
        intraday = yf.download(syms, period="1d", interval="5m", progress=False, auto_adjust=False)["Close"]
    except Exception as e:
        print(f"fetch_indices: Yahoo failed ({e}); indices.json left as it was", flush=True)
        return 0
    rows = build(daily, intraday)
    if not rows:
        print("fetch_indices: nothing priced; indices.json left as it was", flush=True)
        return 0
    OUT.write_text(json.dumps({"generated": datetime.now(timezone.utc).isoformat(), "indices": rows},
                              ensure_ascii=False), encoding="utf-8")
    print(f"fetch_indices: {len(rows)}/{len(STRIP)} priced", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
