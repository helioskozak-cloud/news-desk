"""
fetch_stocks.py — for every ticker that appears in the current headlines.json,
pull recent price data via yfinance and write docs/data/stocks.json with:

  { TICKER: {
      "price":      current close,
      "prev_close": previous close,
      "change":     price - prev_close (absolute),
      "change_pct": (price/prev_close - 1) * 100,
      "week_closes": [last 7 closes for sparkline],
      "wk_low":     min of last 7 closes,
      "wk_high":    max of last 7 closes,
    } }

Runs after fetch_news.py in the workflow.
"""
import json
import socket
import warnings
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

socket.setdefaulttimeout(15)
warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent.parent
DATA = ROOT / "docs" / "data"
HEADLINES = DATA / "headlines.json"
OUT = DATA / "stocks.json"

BATCH_SIZE = 80     # tickers per yfinance.download call


def collect_tickers() -> list[str]:
    if not HEADLINES.exists():
        return []
    try:
        doc = json.loads(HEADLINES.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARN: could not read headlines.json — {e}", flush=True)
        return []
    seen = set()
    for h in doc.get("headlines", []):
        for t in h.get("tickers") or []:
            if t and t.isascii() and 1 <= len(t) <= 6:
                seen.add(t.upper())
    return sorted(seen)


def move_size(closes: list[float]) -> dict:
    """How unusual the last move is: the stock's 20-day standard deviation of
    daily returns (the 20 returns BEFORE the last one, so the move does not
    dilute its own yardstick) and the last move in those units."""
    rets = [b / a - 1 for a, b in zip(closes[:-1], closes[1:]) if a]
    if len(rets) < 11:
        return {}
    last, base = rets[-1], rets[-21:-1]
    mean = sum(base) / len(base)
    sd = (sum((r - mean) ** 2 for r in base) / (len(base) - 1)) ** 0.5
    if not sd:
        return {}
    return {"vol_20d": round(sd * 100, 2), "sigma": round(last / sd, 1)}


def fetch_batch(tickers: list[str]) -> dict:
    """Return {ticker: row} dict for one batch using yfinance bulk download."""
    out = {}
    if not tickers:
        return out
    try:
        df = yf.download(
            " ".join(tickers),
            period="2mo",       # 20 daily returns for vol_20d, plus slack for holidays
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
            group_by="ticker",
        )
    except Exception as e:
        print(f"  batch {tickers[0]}..{tickers[-1]}: download failed — {e}", flush=True)
        return out

    if df is None or df.empty:
        return out

    # When only one ticker is passed, df doesn't have a MultiIndex
    multi = hasattr(df.columns, "levels")

    for tk in tickers:
        try:
            if multi:
                if tk not in df.columns.get_level_values(0):
                    continue
                sub = df[tk]
            else:
                sub = df
            series = sub["Close"].dropna() if "Close" in sub else None
            if series is None or len(series) < 2:
                continue
            full = [float(x) for x in series.tolist()]
            closes = [round(x, 2) for x in full][-7:]
            curr = closes[-1]
            prev = closes[-2]
            row = {
                "price":       curr,
                "prev_close":  prev,
                "change":      round(curr - prev, 2),
                "change_pct":  round((curr / prev - 1) * 100, 2) if prev else 0.0,
                "week_closes": closes,
                "wk_low":      round(min(closes), 2),
                "wk_high":     round(max(closes), 2),
                "as_of":       str(series.index[-1])[:10],
            }
            row.update(move_size(full))
            out[tk] = row
        except Exception:
            continue
    return out


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    tickers = collect_tickers()
    print(f"fetch_stocks: {len(tickers)} unique tickers in current feed", flush=True)
    if not tickers:
        OUT.write_text(json.dumps({"generated": datetime.now(timezone.utc).isoformat(), "stocks": {}}), encoding="utf-8")
        return

    stocks = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i : i + BATCH_SIZE]
        chunk = fetch_batch(batch)
        stocks.update(chunk)
        print(f"  batch {i+1}-{i+len(batch)}: {len(chunk)} resolved", flush=True)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "stocks":    stocks,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)} ({len(stocks)} tickers with prices)", flush=True)


if __name__ == "__main__":
    main()
