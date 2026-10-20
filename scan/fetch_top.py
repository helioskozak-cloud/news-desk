"""
fetch_top.py — what the day's top stories are, and which companies they are
about (owner, 2026-10-20: "you should kind of be able to get a feel for what
the top stories are from other top stories feeds and how many mentions a
ticker gets in a broad index based article").

Two readings, written to docs/data/top_stories.json:

  stories   Google News' Business front page, in its own order: each story
            with the number of outlets Google groups under it (up to five are
            listed), the companies its headlines name, and a link to the first
            outlet in the group that does not need a login (paywall.py).
  mentions  per ticker, over the last 24 hours:
              top     top stories whose headlines name it
              wraps   times it is named inside the body of the day's
                      market-wide articles (index moves, "stocks rise
                      as...", live updates) from our own free feeds
              wrap_n  how many of those articles name it
              score   top * 3 + wrap_n * 2 + min(wraps, 10)   (a ranking aid,
                      not a measurement)

Article bodies are fetched once and cached in the output (by URL), at most
MAX_FETCH new ones a run. A page that will not load counts as no mentions,
and the count of articles read is reported beside the numbers.
Runs at most every MIN_AGE_MIN minutes; --force runs regardless.
"""
import html as htmllib
import json
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paywall import is_walled
import fetch_news as fn

ROOT = Path(__file__).parent.parent
OUT = ROOT / "docs" / "data" / "top_stories.json"
HEADLINES = ROOT / "docs" / "data" / "headlines.json"
TOP_URL = "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}
# These answer an article request from a script with 403 (checked 2026-10-20);
# their stories are counted from the headline and summary instead.
NO_BODY = ("nasdaq.com", "investing.com")
MIN_AGE_MIN = 15
MAX_STORIES = 40
MAX_FETCH = 20          # new article bodies per run
WINDOW_H = 24
# Outlets that need a login, by the name Google prints beside the headline.
WALLED_NAMES = {"WSJ", "The Wall Street Journal", "Bloomberg", "Bloomberg.com", "Financial Times",
                "The New York Times", "The Washington Post", "Barron's", "MarketWatch", "Reuters",
                "Business Insider", "Fortune", "Forbes", "The Information", "The Economist",
                "Seeking Alpha", "Investor's Business Daily", "Morningstar", "Los Angeles Times",
                "The Atlantic", "Nikkei Asia", "The Globe and Mail", "The Telegraph", "The Times"}
# A market-wide article: about the market, not one company (same idea as
# regime-desk's Market news toggle).
MARKET = re.compile(r"\b(stocks|wall street|s&p|dow|nasdaq|russell|indexes|indices|futures|treasur(?:y|ies)|"
                    r"yields?|the fed|federal reserve|inflation|markets?|sell-?off|rally|live updates)\b", re.I)
# A foreign exchange's own close (Investing.com's "<Country> stocks lower at
# close of trade") is not a US market article (owner, 2026-10-20).
FOREIGN_CLOSE = re.compile(r"\b(at close of trade|ftse|dax|cac 40|ibex|omx|obx|wig ?\d\d|bist|ta-35|nifty|sensex|"
                           r"kospi|taiex|hang seng|asx|tsx|bovespa|athens general|all shares)\b", re.I)
NOT_MARKET = re.compile(r"\b(stock|shares) (rating|price target|climbing|falling|surges?|jumps?|plunges?|sinks?)\b|"
                        r"\bmarket debut\b|^why is .+ stock\b", re.I)


def fresh_enough() -> bool:
    try:
        doc = json.loads(OUT.read_text(encoding="utf-8"))
        t = datetime.fromisoformat(doc["generated"])
        return datetime.now(timezone.utc) - t < timedelta(minutes=MIN_AGE_MIN)
    except Exception:
        return False


def get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def split_title(t: str) -> tuple[str, str]:
    """'Headline - Publisher' -> (headline, publisher)."""
    t = htmllib.unescape(t or "").strip()
    m = re.match(r"^(.*)\s+-\s+([^-]{2,60})$", t)
    return (m.group(1).strip(), m.group(2).strip()) if m else (t, "")


def parse_top(xml: str) -> list[dict]:
    out = []
    for i, it in enumerate(xml.split("<item>")[1:MAX_STORIES + 1]):
        title_m = re.search(r"<title>(.*?)</title>", it, re.S)
        link_m = re.search(r"<link>(.*?)</link>", it, re.S)
        pub_m = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        desc_m = re.search(r"<description>(.*?)</description>", it, re.S)
        if not title_m:
            continue
        title, pub = split_title(title_m.group(1))
        group = []      # (headline, outlet, link)
        if desc_m:
            d = htmllib.unescape(desc_m.group(1))
            for a, txt, outlet in re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>(?:&nbsp;|\s)*<font[^>]*>(.*?)</font>', d, re.S):
                group.append((htmllib.unescape(re.sub(r"<[^>]+>", "", txt)).strip(), outlet.strip(), a))
        if not group:
            group = [(title, pub, link_m.group(1).strip() if link_m else "")]
        free = next((g for g in group if g[1] not in WALLED_NAMES), None)
        text = " ".join(g[0] for g in group)
        tks = fn.extract_tickers(text)
        published = None
        if pub_m:
            try:
                from email.utils import parsedate_to_datetime
                published = parsedate_to_datetime(pub_m.group(1).strip()).astimezone(timezone.utc).isoformat()
            except Exception:
                published = None
        out.append({
            "rank": i + 1,
            "title": title,
            "publisher": pub,
            "outlets": len(group),
            "names": [g[1] for g in group],
            "link": (free or group[0])[2],
            "link_outlet": (free or group[0])[1],
            "walled": free is None,
            "published": published,
            "tickers": tks,
        })
    return out


def _json_body(page: str) -> str:
    """CNBC (and pages built like it) ship the article as JSON, not <p> tags:
    "body":{"content":[{"tagName":"p","children":["text", ...]}...]}. The
    text is every plain string sitting in a children list."""
    i = page.find('"body":{"content"')
    if i < 0:
        return ""
    chunk = page[i:i + 300_000]
    out = []
    for m in re.finditer(r'(?<=[\[,])"((?:[^"\\]|\\.){2,})"(?=[,\]])', chunk):
        try:
            x = json.loads('"' + m.group(1) + '"')
        except ValueError:
            continue
        # A sentence, or a single word that is a child of an element (a
        # linked company name: {"tagName":"a",...,"children":["Apple"]}).
        if " " in x or chunk[max(0, m.start() - 12):m.start()] == '"children":[':
            out.append(x)
    return " ".join(out)


def article_text(page: str) -> str:
    body = _json_body(page)
    page = re.sub(r"(?is)<(script|style|noscript|header|footer|nav|aside)[^>]*>.*?</\1>", " ", page)
    paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", page)
    text = " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", p)) for p in paras)
    if len(body.split()) > len(text.split()):
        text = body
    return re.sub(r"\s+", " ", text)


def count_mentions(text: str) -> Counter:
    """Every time a company in the universe is named in text: by name or alias
    (case-sensitive, possessive allowed) or by a $TICKER / (NYSE: X) form.
    Bare all-caps tokens are not counted here: in a long article they are
    mostly acronyms."""
    c: Counter = Counter()
    for pat in fn._TICKER_PATTERNS:
        for m in pat.findall(text):
            t = (m[-1] if isinstance(m, tuple) else m).upper()
            if t in fn.TICKERS and t not in fn.TICKER_BLOCKLIST:
                c[t] += 1
    seen_spans = []
    for name, ticker in fn.NAME_MAP:            # longest first
        if ticker in fn.TICKER_BLOCKLIST:
            continue
        for m in re.finditer(r"\b" + re.escape(name) + r"(?:'s|’s|')?\b", text):
            a, b = m.span()
            if any(a < y and b > x for x, y in seen_spans):
                continue                         # inside a longer name already counted
            seen_spans.append((a, b))
            c[ticker] += 1
    return c


def market_articles(now: datetime) -> list[dict]:
    try:
        doc = json.loads(HEADLINES.read_text(encoding="utf-8"))
    except Exception:
        return []
    cut = (now - timedelta(hours=WINDOW_H)).isoformat()
    out = []
    for h in doc.get("headlines", []):
        link, title = h.get("link", ""), h.get("title", "")
        if (h.get("published") or "") < cut or not link.startswith("http") or is_walled(link):
            continue
        if "news.google.com" in link or h.get("source") in ("Stock Titan", "SEC EDGAR") or h.get("category") == "press":
            continue
        if h.get("type") in ("earnings", "dividend", "offering", "fda", "legal", "analyst", "ma", "guidance"):
            continue
        if FOREIGN_CLOSE.search(title):
            continue
        if len(h.get("tickers") or []) >= 2 or (MARKET.search(title) and not NOT_MARKET.search(title)):
            out.append(h)
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--force" not in argv and fresh_enough():
        print(f"fetch_top: top_stories.json is under {MIN_AGE_MIN} min old — skipped", flush=True)
        return 0
    now = datetime.now(timezone.utc)
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    cache = {a["link"]: a for a in prev.get("articles", []) if a.get("link")}

    try:
        stories = parse_top(get(TOP_URL))
    except Exception as e:
        print(f"fetch_top: Google News top stories failed — {e}; keeping the last list", flush=True)
        stories = prev.get("stories", [])
    # The last 24 hours only (owner, 2026-10-20: "top stories must also be from
    # the last 24 hours"). Google's front page can carry an older story, and
    # the kept list above ages while Google is down. An undated story is out:
    # its age cannot be shown to be inside the window.
    cut24 = (now - timedelta(hours=WINDOW_H)).isoformat()
    stories = [s for s in stories if (s.get("published") or "") >= cut24]

    arts = market_articles(now)
    fetched = failed = 0
    articles = []
    for h in arts:
        link = h["link"]
        a = cache.get(link)
        if a is None and any(d in link for d in NO_BODY):
            text = f"{h.get('title', '')} {h.get('summary', '')}"
            a = {"link": link, "title": h.get("title", ""), "source": h.get("source", ""),
                 "published": h.get("published"), "words": 0, "summary_only": True,
                 "mentions": dict(count_mentions(text))}
        if a is None and fetched < MAX_FETCH:
            fetched += 1
            try:
                text = article_text(get(link))
                a = {"link": link, "title": h.get("title", ""), "source": h.get("source", ""),
                     "published": h.get("published"), "words": len(text.split()),
                     "mentions": dict(count_mentions(text)) if len(text.split()) >= 80 else {}}
            except Exception:
                failed += 1     # counted from what the feed gave us instead
                text = f"{h.get('title', '')} {h.get('summary', '')}"
                a = {"link": link, "title": h.get("title", ""), "source": h.get("source", ""),
                     "published": h.get("published"), "words": 0, "summary_only": True,
                     "mentions": dict(count_mentions(text))}
            time.sleep(0.4)
        if a is not None:
            articles.append(a)

    top = Counter(t for s in stories for t in s.get("tickers", []))
    wraps: Counter = Counter()
    wrap_n: Counter = Counter()
    for a in articles:
        for t, n in a.get("mentions", {}).items():
            wraps[t] += n
            wrap_n[t] += 1
    mentions = {}
    for t in set(top) | set(wraps):
        score = top[t] * 3 + wrap_n[t] * 2 + min(wraps[t], 10)
        mentions[t] = {"top": top[t], "wraps": wraps[t], "wrap_n": wrap_n[t], "score": score}
    payload = {
        "generated": now.isoformat(),
        "source": "Google News Business front page",
        "stories": stories,
        "articles_read": sum(1 for a in articles if a.get("words")),
        "articles_summary_only": sum(1 for a in articles if a.get("summary_only")),
        "articles_seen": len(articles),
        "mentions": dict(sorted(mentions.items(), key=lambda kv: -kv[1]["score"])),
        "articles": articles,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"fetch_top: {len(stories)} top stories, {len(articles)} market articles "
          f"({payload['articles_read']} read, {fetched} fetched this run, {failed} failed), "
          f"{len(mentions)} tickers mentioned", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
