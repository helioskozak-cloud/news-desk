"""
fetch_news.py — pulls headlines from a curated set of free RSS feeds,
dedupes, tags with tickers, and writes docs/data/headlines.json.

Designed to be idempotent: re-running merges with the existing log so
nothing is lost between cron runs.
"""
import json
import re
import sys
import time
import hashlib
import warnings
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent.parent
DATA = ROOT / "docs" / "data"
TICKERS_FILE = Path(__file__).parent / "universe_tickers.csv"
HEADLINES_PATH = DATA / "headlines.json"

MAX_KEEP = 400          # cap on stored headlines (~ 2-3 days at current volume)
USER_AGENT = "news-desk/0.1 (https://github.com/helioskozak-cloud/news-desk)"

# ── Feed list ────────────────────────────────────────────────────────────────
# Each entry: (display_name, category, url)
FEEDS = [
    ("MarketWatch",      "markets",    "http://feeds.marketwatch.com/marketwatch/topstories/"),
    ("MarketWatch RT",   "markets",    "http://feeds.marketwatch.com/marketwatch/marketpulse/"),
    ("Yahoo Finance",    "markets",    "https://finance.yahoo.com/news/rssindex"),
    ("CNBC Top News",    "markets",    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("CNBC Earnings",    "earnings",   "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135"),
    ("CNBC Economy",     "macro",      "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("Investing.com",    "markets",    "https://www.investing.com/rss/news.rss"),
    ("Investing Stock",  "stocks",     "https://www.investing.com/rss/news_25.rss"),
    ("Seeking Alpha",    "stocks",     "https://seekingalpha.com/feed.xml"),
    ("SEC 8-K Filings",  "filings",    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=8-K&dateb=&owner=include&count=40&output=atom"),
    ("PR Newswire Fin",  "press",      "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss"),
    ("BusinessWire Fin", "press",      "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFhRWQ=="),
    ("Reuters Biz",      "markets",    "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best"),
    ("BizToc",           "aggregator", "https://biztoc.com/feed"),
]

# ── Ticker universe ──────────────────────────────────────────────────────────
def load_tickers() -> set[str]:
    if not TICKERS_FILE.exists():
        print(f"WARN: {TICKERS_FILE.name} not found, ticker tagging disabled", flush=True)
        return set()
    tickers = set()
    for line in TICKERS_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip().upper()
        if s and not s.startswith("#"):
            tickers.add(s)
    return tickers


TICKERS = load_tickers()

# Common English words that look like tickers — exclude to cut false positives
TICKER_BLOCKLIST = {
    "A","I","AT","BE","BY","DO","FOR","GO","IF","IN","IS","IT","OF","ON","OR",
    "SO","TO","UP","US","WE","ALL","AND","ANY","ARE","BUT","CAN","DAY","FED",
    "GET","GMT","GOP","GOV","HAS","HOW","ITS","NEW","NOT","NOW","OUT","SAY",
    "SEC","SET","THE","TOP","WAR","WHO","WHY","YOU","CEO","CFO","COO","COP",
    "EPA","EST","ETC","FBI","GDP","IPO","IRS","NEWS","OPEC","UK","EU","UN",
    "AI","ML","UI","UX","VP","API","CDC","CIA","FDA","FAA","CSV","ETF","ETFS",
    "PDF","PR","SDK","HQ","NYC","LA","SF","DC","Q1","Q2","Q3","Q4","M&A",
    "JR","SR","INC","LLC","CO","CORP","LTD","NV","SA","AG","PLC","USA","USD",
    "GMT","BST","EDT","EST","CET","WTI","BPS","YOY","QOQ","MOM","WOW","WSJ",
    "FT","CNBC","BBG","RT","UAW","DOJ","DOE","DHS","DOL","DOT","HUD","TSA",
}


_TICKER_PATTERNS = [
    re.compile(r"\$([A-Z]{1,5})\b"),                     # $TSLA cashtag
    re.compile(r"\(([A-Z]{1,5})\)"),                     # (TSLA)
    re.compile(r"\b(NYSE|NASDAQ|NYSEARCA):\s*([A-Z]{1,5})"),  # NYSE: TSLA
]
_BARE_TICKER = re.compile(r"\b([A-Z]{2,5})\b")


def extract_tickers(text: str) -> list[str]:
    """Return up to 4 tickers found in the text, ranked by signal strength."""
    if not text:
        return []
    found: dict[str, int] = {}  # ticker -> confidence score

    # High-confidence: cashtag / parens / exchange prefix
    for pat in _TICKER_PATTERNS:
        for m in pat.findall(text):
            # findall returns tuple when there are groups
            t = m[-1] if isinstance(m, tuple) else m
            t = t.upper()
            if t in TICKERS and t not in TICKER_BLOCKLIST:
                found[t] = found.get(t, 0) + 3

    # Lower confidence: bare 2-5 letter uppercase token that's in the universe
    for m in _BARE_TICKER.findall(text):
        t = m.upper()
        if t in TICKERS and t not in TICKER_BLOCKLIST:
            found[t] = found.get(t, 0) + 1

    if not found:
        return []
    ranked = sorted(found.items(), key=lambda kv: -kv[1])
    return [t for t, _ in ranked[:4]]


# ── Feed parsing ─────────────────────────────────────────────────────────────
def _hash_url(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def _parse_published(entry) -> str | None:
    """Return ISO timestamp string in UTC, or None."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return None


def _clean_title(title: str) -> str:
    title = re.sub(r"<[^>]+>", "", title or "").strip()
    title = re.sub(r"\s+", " ", title)
    return title


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def fetch_feed(name: str, category: str, url: str) -> list[dict]:
    """Fetch and parse a single feed, returning normalized headline dicts."""
    items: list[dict] = []
    try:
        # feedparser respects HTTP_PROXY and supports custom UA via agent=
        parsed = feedparser.parse(url, agent=USER_AGENT)
    except Exception as e:
        print(f"  {name}: fetch failed — {e}", flush=True)
        return items

    if parsed.bozo and not parsed.entries:
        print(f"  {name}: parse error, no entries", flush=True)
        return items

    for entry in parsed.entries[:30]:
        title = _clean_title(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue
        summary = _clean_title(entry.get("summary", ""))[:500]
        published = _parse_published(entry) or datetime.now(timezone.utc).isoformat()
        text_for_tickers = f"{title} {summary}"
        tickers = extract_tickers(text_for_tickers)
        items.append({
            "id":        _hash_url(link),
            "title":     title,
            "link":      link,
            "summary":   summary if summary != title else "",
            "source":    name,
            "category":  category,
            "domain":    _domain(link),
            "published": published,
            "tickers":   tickers,
        })
    print(f"  {name}: {len(items)} headlines", flush=True)
    return items


# ── Merge with existing log ──────────────────────────────────────────────────
def load_existing() -> list[dict]:
    if not HEADLINES_PATH.exists():
        return []
    try:
        with open(HEADLINES_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return doc.get("headlines", [])
    except Exception as e:
        print(f"WARN: could not load existing headlines.json — {e}", flush=True)
        return []


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"Loaded {len(TICKERS)} ticker symbols for tagging", flush=True)
    print(f"Fetching {len(FEEDS)} feeds...", flush=True)

    fresh: list[dict] = []
    for name, category, url in FEEDS:
        fresh.extend(fetch_feed(name, category, url))
        # Be polite — small delay between requests
        time.sleep(0.3)

    existing = load_existing()
    seen_ids = {h["id"] for h in existing}
    new_items = [h for h in fresh if h["id"] not in seen_ids]
    print(f"\nFetched {len(fresh)} total ({len(new_items)} new since last run)", flush=True)

    # Merge, dedupe by id (newest wins), sort by published desc, cap at MAX_KEEP
    by_id: dict[str, dict] = {h["id"]: h for h in existing}
    for h in fresh:
        by_id[h["id"]] = h     # overwrite if duplicate
    combined = sorted(by_id.values(), key=lambda h: h.get("published", ""), reverse=True)
    combined = combined[:MAX_KEEP]

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "feed_count": len(FEEDS),
        "ticker_universe_size": len(TICKERS),
        "headlines": combined,
    }

    HEADLINES_PATH.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(combined)} headlines to {HEADLINES_PATH.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
