"""
fetch_movers.py — the unusual moves across the whole tagging universe, for
regime-desk's News tab ("Why it moved", 2026-10-13, owner Q55 = c).

A move is sized against the stock's own last 20 daily returns (see
fetch_stocks.move_size), so a 4% day is ordinary for a biotech and a
three-sigma day for a utility. Writes docs/data/movers.json:

  { "generated", "as_of", "universe", "priced", "threshold",
    "movers": [{ticker, name, price, change_pct, vol_20d, sigma}, ...] }

The universe is universe_tickers.csv, the same set fetch_news.py tags
headlines with, so a mover with no headline genuinely had none in our feeds
that we could tag; it does not mean there was no news.

Heavier than the per-headline quotes (about 1,750 tickers), so it runs at
most every MIN_AGE_MIN minutes; pass --force to run regardless.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fetch_stocks import BATCH_SIZE, fetch_batch

ROOT = Path(__file__).parent.parent
OUT = ROOT / "docs" / "data" / "movers.json"
TICKERS_FILE = Path(__file__).parent / "universe_tickers.csv"
NAMES_FILE = Path(__file__).parent / "universe_names.csv"

MIN_AGE_MIN = 30
THRESHOLD = 2.5      # |move| in 20-day standard deviations
MIN_PRICE = 3.0      # below this a "move" is mostly tick size
MIN_ABS_PCT = 2.0    # a pinned stock (a pending deal) makes -0.8% look like 2.7 sd
KEEP = 40
# Top movers by plain percent (owner, 2026-10-13: "a top movers section with
# articles"): the day's biggest gainers and losers among operating companies.
TOP_N = 15
TOP_MIN_PRICE = 5.0
FUND = re.compile(r"\b(?:ETF|ETN|Fund|Trust|Index|Shares|iShares|SPDR|Portfolio|ProShares|Direxion|Defiance|Daily|Ultra|Leveraged|Inverse|Bull|Bear|[23]X)\b", re.I)  # funds and leveraged/inverse products


def universe() -> list[str]:
    return sorted({s.strip().upper() for s in TICKERS_FILE.read_text(encoding="utf-8").splitlines()
                   if s.strip() and not s.startswith("#")})


def names() -> dict[str, str]:
    out = {}
    for line in NAMES_FILE.read_text(encoding="utf-8").splitlines():
        if "," in line and not line.startswith("#"):
            t, n = (x.strip() for x in line.split(",", 1))
            out.setdefault(t.upper(), n)      # first entry is the canonical name
    return out


def fresh_enough() -> bool:
    try:
        doc = json.loads(OUT.read_text(encoding="utf-8"))
        made = datetime.fromisoformat(doc["generated"])
        return datetime.now(timezone.utc) - made < timedelta(minutes=MIN_AGE_MIN)
    except Exception:
        return False


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--force" not in argv and fresh_enough():
        print(f"fetch_movers: movers.json is under {MIN_AGE_MIN} min old — skipped", flush=True)
        return 0
    tickers = universe()
    quotes = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        quotes.update(fetch_batch(tickers[i:i + BATCH_SIZE]))
    nm = names()
    sized = [(t, q) for t, q in quotes.items() if "sigma" in q and q["price"] >= MIN_PRICE]
    # The newest session in the batch: a ticker whose last bar is older did not trade.
    as_of = max((q["as_of"] for _, q in sized), default=None)
    movers = [{"ticker": t, "name": nm.get(t, ""), "price": q["price"], "change_pct": q["change_pct"],
               "vol_20d": q["vol_20d"], "sigma": q["sigma"]}
              for t, q in sized if q["as_of"] == as_of and abs(q["sigma"]) >= THRESHOLD
              and abs(q["change_pct"]) >= MIN_ABS_PCT]
    movers.sort(key=lambda m: -abs(m["sigma"]))
    payload = {"generated": datetime.now(timezone.utc).isoformat(), "as_of": as_of,
               "universe": len(tickers), "priced": len(sized), "threshold": THRESHOLD, "min_abs_pct": MIN_ABS_PCT,
               "movers": movers[:KEEP]}
    # Gainers / losers: same session, priced, not a fund (leveraged ETFs would
    # crowd out companies), at least $5.
    pool = [(t, q) for t, q in sized if q["as_of"] == as_of and q["price"] >= TOP_MIN_PRICE
            and not FUND.search(nm.get(t, ""))]
    row = lambda t, q: {"ticker": t, "name": nm.get(t, ""), "price": q["price"], "change_pct": q["change_pct"],
                        "vol_20d": q.get("vol_20d"), "sigma": q.get("sigma")}
    pool.sort(key=lambda tq: tq[1]["change_pct"])
    payload["losers"] = [row(t, q) for t, q in pool[:TOP_N] if q["change_pct"] < 0]
    payload["gainers"] = [row(t, q) for t, q in reversed(pool[-TOP_N:]) if q["change_pct"] > 0]
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"fetch_movers: {len(sized)}/{len(tickers)} priced, {len(movers)} at |move| >= {THRESHOLD} sd "
          f"on {as_of}; kept {len(payload['movers'])}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
