"""
fetch_movers.py — the unusual moves across the whole tagging universe, for
regime-desk's News tab ("Why it moved", 2026-10-13, owner Q55 = c).

A move is sized against the stock's own last 20 daily returns (see
fetch_stocks.move_size), so a 4% day is ordinary for a biotech and a
three-sigma day for a utility. Writes docs/data/movers.json:

  { "generated", "as_of", "universe", "priced", "threshold",
    "movers": [{ticker, name, price, change_pct, vol_20d, sigma}, ...] }

The universe is universe_tickers.csv, the same set fetch_news.py tags
headlines with. A shown mover with no tagged headline in our feeds gets one
from the open web instead (web_headline.py, Google News search), as
"web": {title, link, source, published, found}, or "web": null when nothing
found names the company (2026-10-14).

Heavier than the per-headline quotes (about 1,750 tickers), so it runs at
most every MIN_AGE_MIN minutes; pass --force to run regardless.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fetch_stocks import BATCH_SIZE, fetch_batch
import web_headline

ROOT = Path(__file__).parent.parent
OUT = ROOT / "docs" / "data" / "movers.json"
TICKERS_FILE = Path(__file__).parent / "universe_tickers.csv"
NAMES_FILE = Path(__file__).parent / "universe_names.csv"
HEADLINES = ROOT / "docs" / "data" / "headlines.json"
WEB_SHOWN = 10       # regime-desk shows the top ten gainers, losers and unusual moves
WEB_REUSE_H = 2      # a web headline found this session is re-searched after this long

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


def tagged_recently(now: datetime) -> set[str]:
    """Tickers with a feed headline inside the window regime-desk shows (36 h)."""
    try:
        doc = json.loads(HEADLINES.read_text(encoding="utf-8"))
    except Exception:
        return set()
    cut = now - timedelta(hours=web_headline.MAX_AGE_H)
    out = set()
    for h in doc.get("headlines", []):
        try:
            if datetime.fromisoformat(h["published"]) >= cut:
                out.update(t.upper() for t in h.get("tickers") or [])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def add_web_headlines(payload: dict, previous: dict, now: datetime, find=web_headline.find) -> None:
    """Give each shown mover our feeds missed a headline from the open web
    ("web": {title, link, source, published, found}), or "web": null when the
    search found nothing that names the company. A headline found earlier this
    session is reused for WEB_REUSE_H hours rather than searched again."""
    have = tagged_recently(now)
    prior = {}
    if previous.get("as_of") == payload["as_of"]:
        for key in ("gainers", "losers", "movers"):
            for m in previous.get(key) or []:
                if m.get("web"):
                    prior[m["ticker"]] = m["web"]
    found = {}
    for key in ("gainers", "losers", "movers"):
        for m in (payload.get(key) or [])[:WEB_SHOWN]:
            t = m["ticker"]
            if t in have:
                continue
            if t not in found:
                old = prior.get(t)
                try:
                    fresh = old and now - datetime.fromisoformat(old["found"]) < timedelta(hours=WEB_REUSE_H)
                except (KeyError, TypeError, ValueError):
                    fresh = False
                if fresh:
                    found[t] = old
                else:
                    hit = find(t, m.get("name", ""), now)
                    found[t] = dict(hit, found=now.isoformat()) if hit else None
            m["web"] = found[t]


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
    try:
        previous = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        previous = {}
    add_web_headlines(payload, previous, datetime.now(timezone.utc))
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"fetch_movers: {len(sized)}/{len(tickers)} priced, {len(movers)} at |move| >= {THRESHOLD} sd "
          f"on {as_of}; kept {len(payload['movers'])}; "
          f"{sum(1 for k in ('gainers', 'losers', 'movers') for m in payload[k] if m.get('web'))} web headlines", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
