"""
fetch_news.py — pulls headlines from a curated set of free RSS feeds,
dedupes, classifies (upgrade/earnings/M&A/etc), tags with tickers, and
writes docs/data/headlines.json.

Idempotent: re-running merges with the existing log so nothing is lost
between cron runs. Capped at MAX_KEEP most-recent stories.
"""
import json
import re
import sys
import time
import hashlib
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import socket
import urllib.request
import feedparser

# Cap per-feed network IO at 15 seconds — some feeds hang otherwise
socket.setdefaulttimeout(15)
warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent.parent
DATA = ROOT / "docs" / "data"
TICKERS_FILE = Path(__file__).parent / "universe_tickers.csv"
NAMES_FILE   = Path(__file__).parent / "universe_names.csv"
ALIASES_FILE = Path(__file__).parent / "universe_aliases.csv"
HEADLINES_PATH = DATA / "headlines.json"

MAX_KEEP = 600
USER_AGENT = "news-desk/0.2 (https://github.com/helioskozak-cloud/news-desk)"

# ── Feed list ────────────────────────────────────────────────────────────────
# (display_name, category, url). Every feed here was checked live on
# 2026-10-13 for a 200 AND a story dated that day; see RETIRED below for the
# ones that failed.
FEEDS = [
    # General market news
    ("MarketWatch",       "markets",   "http://feeds.marketwatch.com/marketwatch/topstories/"),
    ("CNBC Top",          "markets",   "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("CNBC Earnings",     "earnings",  "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135"),
    ("CNBC Economy",      "macro",     "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("CNBC Investing",    "markets",   "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839263"),
    ("NASDAQ Stocks",     "stocks",    "https://www.nasdaq.com/feed/rssoutbound?category=Stocks"),
    ("NASDAQ Markets",    "markets",   "https://www.nasdaq.com/feed/rssoutbound?category=Markets"),
    ("Investing.com",     "markets",   "https://www.investing.com/rss/news.rss"),
    ("Investing Stock",   "stocks",    "https://www.investing.com/rss/news_25.rss"),
    ("Investing Forex",   "fx",        "https://www.investing.com/rss/news_1.rss"),
    ("Seeking Alpha",     "stocks",    "https://seekingalpha.com/feed.xml"),
    ("Seeking Alpha M&A", "ma",        "https://seekingalpha.com/market_currents.xml"),
    ("Markets Insider",   "stocks",    "https://markets.businessinsider.com/rss/news"),
    ("NYT Business",      "macro",     "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
    ("NYT Economy",       "macro",     "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml"),
    ("FT Markets",        "markets",   "https://www.ft.com/markets?format=rss"),
    ("FT Companies",      "stocks",    "https://www.ft.com/companies?format=rss"),
    ("Federal Reserve",   "macro",     "https://www.federalreserve.gov/feeds/press_all.xml"),
    # Press release wires. Only stories that name a ticker are kept (PRESS_NEEDS_TICKER).
    ("PR Newswire Fin",   "press",     "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss"),
    ("PR Newswire All",   "press",     "https://www.prnewswire.com/rss/news-releases-list.rss"),
    ("BusinessWire",      "press",     "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFtRWA=="),
    # Ticker-tagged stock news (every headline structurally tagged with a symbol)
    ("Stock Titan",       "stocks",    "https://www.stocktitan.net/rss"),
    ("Benzinga",          "stocks",    "https://www.benzinga.com/feed"),
]
# SEC filings come from regime-desk's EDGAR scan (fetch_filings below), not
# EDGAR's own RSS, which answers 403 to anything without a registered identity.
FILINGS_URL = "https://raw.githubusercontent.com/helioskozak-cloud/regime-desk/main/data/filings.json"

# NASDAQ answers a bot user agent with a hang, and the Fed's feed fails
# through feedparser's own fetch; both come through as a browser request.
BROWSER_UA_FEEDS = {"NASDAQ Stocks", "NASDAQ Markets", "Federal Reserve"}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# A story older than this is not news; a feed whose newest story is older
# than STALE_FEED_DAYS is reported in the run log as stale.
MAX_AGE_DAYS = 7
STALE_FEED_DAYS = 3
PRESS_NEEDS_TICKER = True
SLOW_FEEDS = {"Federal Reserve"}     # publishes a few times a week, not daily

# Source names that have ever been retired — used to purge historical
# entries from the running headlines.json on the next merge so the UI
# doesn't keep showing them after a feed is dropped.
DROPPED_SOURCES = {
    "BizToc",  # affiliate-link spam aggregator, retired 2026-06-17
    # Retired 2026-10-13 after a live check of every feed:
    "Yahoo Finance", "BI Finance", "Benzinga News", "Benzinga Markets",   # 404
    "GlobeNewswire", "SEC 8-K Filings", "SEC 13D/G",                      # 403
    "WSJ Markets", "WSJ Business", "WSJ World",      # newest story Jan 2025
    "MarketWatch RT",                                # newest story Jul 2025
}

# ── Ticker universe ──────────────────────────────────────────────────────────
def load_tickers() -> set[str]:
    if not TICKERS_FILE.exists():
        return set()
    return {
        s.strip().upper()
        for s in TICKERS_FILE.read_text(encoding="utf-8").splitlines()
        if s.strip() and not s.strip().startswith("#")
    }


def load_name_map() -> list[tuple[str, str]]:
    """Return list of (company_name, ticker), longest-first. Pulls from BOTH
    universe_names.csv (canonical names) and universe_aliases.csv (brand /
    product / subsidiary aliases). De-duped by lowercase name."""
    raw: list[tuple[str, str]] = []
    for path in (NAMES_FILE, ALIASES_FILE):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split(",", 1)
            if len(parts) != 2:
                continue
            ticker = parts[0].strip().upper()
            name = parts[1].strip()
            if not name or not ticker or len(name) < 3:
                continue
            raw.append((name, ticker))

    # A fund answers only to its fund name. "JPMorgan" was JIRE's short name,
    # so every JPMorgan story was tagged with a JPMorgan ETF (audit 2026-10-13:
    # 16 funds held a bare issuer brand — iShares, Vanguard, Schwab, ...).
    funds = {t for n, t in raw if _FUND_WORDS.search(n)}
    pairs: list[tuple[str, str]] = []
    seen_keys = set()
    for name, ticker in raw:
        if ticker in funds and not _FUND_WORDS.search(name):
            continue
        if name in NAME_BLOCKLIST or (name, ticker) in WRONG_NAMES:
            continue
        key = name.lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        pairs.append((name, ticker))

    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


_FUND_WORDS = re.compile(r"\b(?:ETF|ETN|Fund|Trust|Index|Shares|Portfolio)\b", re.I)

# One-word aliases that are ordinary words. Headlines are Title Case, so a
# case-sensitive match does not protect them: "News" tagged NWSA on 124 of
# 600 stories in the 2026-10-13 audit. These companies still tag by ticker,
# by their longer names ("News Corp", "Service Corporation") and by aliases.
NAME_BLOCKLIST = {
    "News", "Service", "Strategy", "Restaurant", "West", "Packaging", "Fortune",
    "Nasdaq", "Applied", "Arch", "Array", "Ball", "Beam", "Block", "Chart",
    "Choice", "Citizens", "Comfort", "Compass", "Constellation", "Crane", "Crown",
    "Dave", "Eagle", "Edison", "Elastic", "Figure", "Flex", "Franklin", "Grab",
    "Herc", "Jazz", "Lineage", "Mac", "Mercury", "Noble", "Nova", "Optimus",
    "Pixel", "Pool", "Popular", "Post", "Primo", "Principal", "Quest", "Range",
    "Reliance", "Rhythm", "Ross", "Square", "Stride", "Threads", "Tyler", "Vera",
    "Viking", "Voyager", "Windows", "Zoom", "Mueller", "Carpenter",
    "Snap", "Toast", "Arm", "Affirm", "Bullish", "Celsius", "Corvus", "Matador",
    "Kirby", "Lear", "Dover", "Waters", "Apogee", "Perpetua", "Terns",
    "Karman", "Seaboard", "Lumen",
}
# Aliases pointing at the wrong company.
WRONG_NAMES = {("Quest", "META"), ("RTX", "NVDA"), ("Block", "SQ")}


TICKERS = load_tickers()
NAME_MAP = load_name_map()

# Common English words / acronyms that look like tickers — exclude
TICKER_BLOCKLIST = {
    "A","I","AT","BE","BY","DO","FOR","GO","IF","IN","IS","IT","OF","ON","OR",
    "SO","TO","UP","US","WE","AM","PM","AN","NO","AS","HE","ME","MY","OK",
    "ALL","AND","ANY","ARE","BUT","CAN","DAY","FED","GET","GMT","GOP","GOV",
    "HAS","HOW","ITS","NEW","NOT","NOW","OUT","SAY","SEC","SET","THE","TOP",
    "WAR","WHO","WHY","YOU","CEO","CFO","COO","COP","EPA","EST","ETC","FBI",
    "GDP","IPO","IRS","NEWS","OPEC","UK","EU","UN","AI","ML","UI","UX","VP",
    "API","CDC","CIA","FDA","FAA","CSV","ETF","ETFS","PDF","PR","SDK","HQ",
    "NYC","LA","SF","DC","Q1","Q2","Q3","Q4","M&A","JR","SR","INC","LLC",
    "CO","CORP","LTD","NV","SA","AG","PLC","USA","USD","BST","EDT","CET",
    "WTI","BPS","YOY","QOQ","MOM","WOW","WSJ","FT","CNBC","BBG","RT","UAW",
    "DOJ","DOE","DHS","DOL","DOT","HUD","TSA","ESG","SPAC","TLDR","FYI",
    "RNG","ION","KEY","TRUE","FALSE","JUST","NOW","NEXT","ONE","TWO","ROW",
    "OWN","OFF","BIG","TOP","WIN","HIT","WAY","BAD","GOOD","FREE","SEE",
    "MEN","RUN","BEAT","WANT","HOME","WORK","CALL","PUT","BUY","SELL","HOLD",
    "RATE","JOB","CUT","WIN","ALSO","LATE","REAL","HUGE","SAME","BLUE","RED",
    "MOST","FUND","LOAN","BANK","TREE","ROAD","GAS","OIL","GOLD",
}

_TICKER_PATTERNS = [
    re.compile(r"\$([A-Z]{1,5})\b"),                        # $TSLA
    re.compile(r"\(([A-Z]{1,5})\)"),                        # (TSLA)
    re.compile(r"\(([A-Z]{1,5}):[A-Z]{1,4}\)"),             # (TSLA:US), (LABS:CA) — Seeking Alpha
    re.compile(r"\b(?:NYSE|NASDAQ|NYSEARCA|AMEX):\s*([A-Z]{1,5})"),  # NYSE: TSLA
    re.compile(r"\|\s*([A-Z]{1,5})\s+Stock\s+News\s*$"),    # ... | TSLA Stock News (Stock Titan)
]
# A bare capitalised token is the weakest evidence. Not when it is glued to
# & (PG&E, S&P) or to a one-letter suffix (CAR-T; "Schneider-PTC" still
# tags PTC), and not when the headline shouts in capitals
# ("TEQUILA DON JULIO INTRODUCES ..." tagged DIA).
_BARE_TICKER = re.compile(r"(?<!&)\b([A-Z]{2,5})\b(?!&|-[A-Z]\b)")
# Bare tokens that, in news copy, are nearly always something else: brokers
# (RBC Capital, TD Cowen), drug classes (ADC), states (MA), Dia de Muertos ...
BARE_BLOCKLIST = {"RBC", "TD", "EL", "CAR", "ADC", "MA", "DIA", "LH", "MTN",
                  "SHOP", "ACWI", "WPC", "AEM"}


_CAPS_RUN = re.compile(r"\b[A-Z]{3,}(?:[\s,]+[A-Z]{3,}){2,}\b")


def _shouting(text: str) -> bool:
    """Three or more capitalised words in a row, at least two of them not
    tickers. "META, VTRS, MRVL, COKE" is a ticker list, not shouting."""
    for run in _CAPS_RUN.findall(text):
        words = re.findall(r"[A-Z]{3,}", run)
        if sum(w not in TICKERS for w in words) >= 2:
            return True
    return False


def extract_tickers(text: str) -> list[str]:
    """Return up to 4 tickers found in text, ranked by confidence."""
    if not text:
        return []
    found: dict[str, int] = {}

    for pat in _TICKER_PATTERNS:
        for m in pat.findall(text):
            t = (m[-1] if isinstance(m, tuple) else m).upper()
            if t in TICKERS and t not in TICKER_BLOCKLIST:
                found[t] = found.get(t, 0) + 5

    if not _shouting(text):
        for m in _BARE_TICKER.findall(text):
            t = m.upper()
            if t in TICKERS and t not in TICKER_BLOCKLIST and t not in BARE_BLOCKLIST:
                found[t] = found.get(t, 0) + 1

    # Company-name + alias matches (case-sensitive — proper-noun company
    # names start with a capital in news copy; "Apple" matches but "apple"
    # the fruit doesn't). Allow optional possessive 's / ' suffix.
    for name, ticker in NAME_MAP:
        if ticker in TICKER_BLOCKLIST:
            continue
        try:
            # \b name (?:'s|’s|')? \b — possessive optional
            pattern = r"\b" + re.escape(name) + r"(?:'s|’s|')?\b"
            if re.search(pattern, text):
                # Boost name matches over bare-token matches
                found[ticker] = found.get(ticker, 0) + 4
        except re.error:
            continue

    if not found:
        return []
    ranked = sorted(found.items(), key=lambda kv: -kv[1])
    return [t for t, _ in ranked[:4]]


# ── Headline classifier ─────────────────────────────────────────────────────
# Map a headline to an "action type" — used by the In Play view for color/icon
_TYPE_PATTERNS = [
    # Analyst actions only: "raises $10M" and "boosts output" are not upgrades.
    ("upgrade",   re.compile(r"\b(?:upgrades?|upgraded|raises? (?:price target|PT|to (?:buy|outperform|overweight))|PT raised|positive ratings?)\b", re.I)),
    ("downgrade", re.compile(r"\b(?:downgrades?|downgraded|cuts? (?:price target|PT|to (?:sell|underperform|underweight|neutral|hold))|lowers? (?:price target|PT)|PT cut|negative ratings?)\b", re.I)),
    ("analyst",   re.compile(r"\b(?:initiates? coverage|reiterates?|maintains?|price target|PT raised|PT cut)\b", re.I)),
    # Bare "earnings" counts now: "Bank earnings live updates ... deal backlog"
    # used to fall through to M&A on "deal".
    ("earnings",  re.compile(r"\b(?:Q[1-4]|earnings|EPS|reported|results|quarterly|guidance|beats? estimates|misses estimates)\b", re.I)),
    ("ma",        re.compile(r"\b(?:acquires?|acquired|acquisition|merger|merge|to buy|buyout|takeover|deal to (?:buy|acquire)|combine)\b", re.I)),
    ("fda",       re.compile(r"\b(?:FDA|approval|approved|rejected|trial results|phase \d|EUA|biologic)\b", re.I)),
    ("legal",     re.compile(r"\b(?:lawsuit|sued?|settlement|fine|probe|investigation|charges?|fraud)\b", re.I)),
    ("guidance",  re.compile(r"\b(?:raises? guidance|cuts? guidance|outlook|forecast|sees? Q|prelim)\b", re.I)),
    ("dividend",  re.compile(r"\b(?:dividend|buyback|repurchase|stock split)\b", re.I)),
    ("offering",  re.compile(r"\b(?:offering|prices? offering|secondary|debt offering|notes due|IPO)\b", re.I)),
    ("8k",        re.compile(r"\b(?:8-K|13D|13G|S-1|10-Q|10-K|prospectus)\b", re.I)),
    ("macro",     re.compile(r"\b(?:Fed|inflation|CPI|PCE|jobs report|GDP|unemployment|nonfarm|FOMC|rates?|tariff)\b", re.I)),
]


def classify(title: str, category: str) -> str:
    """Return short action type, falls back to category."""
    for typ, pat in _TYPE_PATTERNS:
        if pat.search(title):
            return typ
    return category or "news"


# ── Sentiment classifier (conservative — only color when confident) ─────────
_POS_RE = re.compile(
    r"\b(beats?|beat|surge[ds]?|soar[ds]?|jump[s]?|jumped|rall(?:y|ies|ied)|"
    r"climb[s]?|climbed|gain[s]?|gained|rise[s]?|rose|hike[ds]?|raise[ds]?|"
    r"upgrade[ds]?|outperform[s]?|tops?|topped|exceeds?|exceeded|strong|"
    r"record\s+high|all[-\s]time\s+high|profit[s]?|profitable|expand[s]?|"
    r"approves?|approved|wins?|won|secures?|secured|launches?|launched)\b",
    re.I,
)
_NEG_RE = re.compile(
    r"\b(misses?|missed|plunge[ds]?|slump[s]?|slumped|slide[ds]?|slid|"
    r"falls?|fell|drop[s]?|dropped|tumble[ds]?|crash(?:es|ed)?|sink[s]?|sank|"
    r"downgrade[ds]?|cut[s]?|slashe[ds]?|reduce[ds]?|lower[ed]?|"
    r"lawsuit|sued?|charged?|fined?|fraud|scandal|probe|investigation|"
    r"recall[s]?|halt[s]?|halted|suspend[s]?|suspended|"
    r"bankrupt(?:cy)?|insolven(?:t|cy)|layoffs?|fired|resign[s]?|resigned|"
    r"warn(?:s|ed|ing)?|weak|disappoint(?:s|ing|ed)?|"
    r"shrink[s]?|shrank|loss(?:es)?|missed\s+estimates?|guides?\s+down)\b",
    re.I,
)


def sentiment(title: str, type_: str) -> str:
    """positive / negative / neutral. Falls through to the action-type
    semantics when the verb-based check is ambiguous."""
    t = title or ""
    has_pos = bool(_POS_RE.search(t))
    has_neg = bool(_NEG_RE.search(t))
    if has_pos and not has_neg:
        return "positive"
    if has_neg and not has_pos:
        return "negative"
    # Type-based fallback
    if type_ in ("upgrade", "dividend"):
        return "positive"
    if type_ in ("downgrade", "legal"):
        return "negative"
    return "neutral"


# ── Feed parsing ─────────────────────────────────────────────────────────────
def _hash_url(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def _parse_published(entry) -> str | None:
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


# Bump when the tagger or classifier changes: stored stories carry the
# version they were tagged under and are re-tagged once on the next run.
TAG_VERSION = 2


def tag(h: dict) -> dict:
    """(Re)derive tickers, type and sentiment from the story's own text."""
    h["tickers"] = extract_tickers(f"{h['title']} {h.get('summary', '')}")
    h["type"] = classify(h["title"], h.get("category", ""))
    h["sentiment"] = sentiment(h["title"], h["type"])
    h["tag_v"] = TAG_VERSION
    return h


def fetch_feed(name: str, category: str, url: str) -> list[dict]:
    items: list[dict] = []
    try:
        if name in BROWSER_UA_FEEDS:
            body = urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})).read()
            parsed = feedparser.parse(body)
        else:
            parsed = feedparser.parse(url, agent=USER_AGENT)
    except Exception as e:
        print(f"  {name}: fetch failed — {e}", flush=True)
        return items

    if parsed.bozo and not parsed.entries:
        print(f"  {name}: parse error, no entries", flush=True)
        return items

    now = datetime.now(timezone.utc)
    oldest_ok = (now - timedelta(days=MAX_AGE_DAYS)).isoformat()
    newest, old, untagged = "", 0, 0
    for entry in parsed.entries[:40]:
        title = _clean_title(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue
        summary = _clean_title(entry.get("summary", ""))[:500]
        published = _parse_published(entry) or now.isoformat()
        newest = max(newest, published)
        if published < oldest_ok:
            old += 1
            continue
        h = tag({
            "id":        _hash_url(link),
            "title":     title,
            "link":      link,
            "summary":   summary if summary != title else "",
            "source":    name,
            "category":  category,
            "domain":    _domain(link),
            "published": published,
        })
        if PRESS_NEEDS_TICKER and category == "press" and not h["tickers"]:
            untagged += 1
            continue
        items.append(h)
    note = "".join([f", {old} older than {MAX_AGE_DAYS}d skipped" if old else "",
                    f", {untagged} untagged press releases skipped" if untagged else ""])
    print(f"  {name}: {len(items)} headlines{note}", flush=True)
    if newest and name not in SLOW_FEEDS and newest < (now - timedelta(days=STALE_FEED_DAYS)).isoformat():
        print(f"  WARN {name}: STALE — newest story {newest[:10]}", flush=True)
    return items


MAX_FILING_RANK = 2
_ITEM_TEXT = re.compile(r"\b\d\.\d\d\s+")


def fetch_filings() -> list[dict]:
    """Material SEC filings from regime-desk's EDGAR scan, as headlines.

    regime-desk ranks each filing 0 (read first: non-reliance, bankruptcy,
    late filing) to 6 (routine). Ranks 0-2 come through: non-reliance,
    bankruptcy, delisting, auditor change, impairment, change of control,
    activist stakes (about 27 a week in October 2026; ranks 0-3 were 145, a
    quarter of the feed). Officer changes and offerings stay on regime-desk's
    Filings tab. The ticker is EDGAR's own, not inferred from text."""
    try:
        req = urllib.request.Request(FILINGS_URL, headers={"User-Agent": USER_AGENT})
        doc = json.loads(urllib.request.urlopen(req).read())
    except Exception as e:
        print(f"  SEC EDGAR: fetch failed — {e}", flush=True)
        return []
    now = datetime.now(timezone.utc)
    items = []
    for f in doc.get("filings", []):
        if f.get("rank", 6) > MAX_FILING_RANK:
            continue
        tk = (f.get("ticker") or "").upper()
        if not tk or not f.get("url"):
            continue
        what = _ITEM_TEXT.sub("", f.get("what") or f.get("form", "")).replace("NON-RELIANCE",
                                                                             "non-reliance on past financials")
        form = f.get("form", "")
        title = f"{f.get('name') or tk} ({tk}) files {form}: {what}" if f.get("kind") == "8-K" \
            else f"{f.get('name') or tk} ({tk}): {what}"
        # EDGAR gives the filing DATE only. Stamp it 4 PM ET, or now if that
        # is still ahead, so a filing never sorts above the news of its own day.
        stamp = min(datetime.fromisoformat(f["date"]).replace(hour=20, tzinfo=timezone.utc), now)
        items.append({
            "id":        _hash_url(f["url"]),
            "title":     title,
            "link":      f["url"],
            "summary":   "",
            "source":    "SEC EDGAR",
            "category":  "filings",
            "type":      "8k",
            "sentiment": "negative" if f.get("rank", 6) <= 1 else "neutral",
            "domain":    "sec.gov",
            "published": stamp.isoformat(),
            "tickers":   [tk],
            "rank":      f.get("rank"),
            "tag_v":     TAG_VERSION,
        })
    print(f"  SEC EDGAR: {len(items)} material filings (of {len(doc.get('filings', []))})", flush=True)
    return items


def load_existing() -> list[dict]:
    if not HEADLINES_PATH.exists():
        return []
    try:
        with open(HEADLINES_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return doc.get("headlines", [])
    except Exception as e:
        print(f"WARN: existing headlines load failed — {e}", flush=True)
        return []


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"Loaded {len(TICKERS)} ticker symbols, {len(NAME_MAP)} name patterns", flush=True)
    print(f"Fetching {len(FEEDS)} feeds...", flush=True)

    fresh: list[dict] = []
    for name, category, url in FEEDS:
        fresh.extend(fetch_feed(name, category, url))
        time.sleep(0.25)
    fresh.extend(fetch_filings())

    existing = load_existing()
    # Purge any entries from sources we've retired — keeps the UI clean
    # when a feed is dropped instead of letting old items hang around for
    # MAX_KEEP rotations.
    before = len(existing)
    existing = [h for h in existing if h.get("source") not in DROPPED_SOURCES]
    purged = before - len(existing)
    if purged:
        print(f"Purged {purged} entries from retired sources: "
              f"{', '.join(sorted(DROPPED_SOURCES))}", flush=True)

    seen_ids = {h["id"] for h in existing}
    new_items = [h for h in fresh if h["id"] not in seen_ids]
    print(f"\nFetched {len(fresh)} total ({len(new_items)} new since last run)", flush=True)

    # Re-tag stories stored under an older tagger, once; then apply today's
    # press-release rule to them too.
    retagged = 0
    for h in existing:
        if h.get("tag_v") != TAG_VERSION and h.get("source") != "SEC EDGAR":
            tag(h)
            retagged += 1
    if retagged:
        print(f"Re-tagged {retagged} stored stories under tagger v{TAG_VERSION}", flush=True)
    if PRESS_NEEDS_TICKER:
        existing = [h for h in existing if h.get("category") != "press" or h.get("tickers")]

    by_id: dict[str, dict] = {h["id"]: h for h in existing}
    for h in fresh:
        by_id[h["id"]] = h
    combined = sorted(by_id.values(), key=lambda h: h.get("published", ""), reverse=True)
    combined = combined[:MAX_KEEP]

    tagged_pct = (sum(1 for h in combined if h["tickers"]) / max(1, len(combined))) * 100
    print(f"\nFinal: {len(combined)} headlines, {tagged_pct:.0f}% ticker-tagged", flush=True)

    # Build a ticker -> canonical name map for tickers actually present in
    # the feed (used by the UI tooltip). Pick the first name pair that matches.
    tickers_in_feed = {tk for h in combined for tk in h.get("tickers", [])}
    name_lookup = {}
    for name, ticker in NAME_MAP:
        if ticker in tickers_in_feed and ticker not in name_lookup:
            name_lookup[ticker] = name
    print(f"Built ticker_names map for {len(name_lookup)} symbols", flush=True)

    payload = {
        "generated":    datetime.now(timezone.utc).isoformat(),
        "feed_count":   len(FEEDS),
        "ticker_universe_size": len(TICKERS),
        "headlines":    combined,
        "ticker_names": name_lookup,
    }
    HEADLINES_PATH.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {HEADLINES_PATH.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
