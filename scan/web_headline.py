"""
web_headline.py — a headline from the open web for a mover our feeds missed
(owner, 2026-10-14: "where there is no headline in our feeds can we find one
somewhere on the internet").

Source: Google News search RSS (free, no key), last two days, "<name>" stock.
A result from a site that needs a login (paywall.py) is skipped, and one
counts only if its title names the company or the ticker, so a
search that wanders off to another firm is no headline rather than a wrong
one. Nothing found is reported as nothing found.
"""
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from paywall import is_walled

SEARCH = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126 Safari/537.36"}
MAX_AGE_H = 36       # the same window regime-desk uses for a feed headline
PAUSE_S = 0.6        # between searches; one refresh asks about ~40 names at most
# Corporate suffixes are not the name: "Apple Inc." must match "Apple".
SUFFIX = re.compile(r"[,.]?\s+(?:Inc|Corp|Corporation|Co|Company|Ltd|Limited|plc|PLC|Holdings?|Group|"
                    r"Technologies|Therapeutics|Pharmaceuticals|Bancorp|Financial|N\.?V|S\.?A|AG|SE|LP|L\.?P)\.?$")


# A suffix stays when cutting it would leave a word that names half the market
# ("First Financial" must not match every headline that says "first").
COMMON = {"first", "general", "united", "american", "national", "global", "international",
          "new", "south", "north", "east", "west", "western", "eastern", "central", "pacific", "atlantic"}


def core_name(name: str) -> str:
    n = (name or "").strip()
    while True:
        m = SUFFIX.search(n)
        if not m or m.start() == 0:
            return n
        cut = n[:m.start()].strip()
        if " " not in cut and cut.lower() in COMMON:
            return n
        n = cut


def names_it(title: str, ticker: str, name: str) -> bool:
    """The title is about this company: it carries the ticker as a word, or the
    company's core name (first two words of it at most, case-insensitive)."""
    # A ticker counts in a cashtag, in brackets, after an exchange, or as a
    # bare word of three letters or more ("ON", "A", "IT" are English).
    t = re.escape(ticker)
    if re.search(rf"\${t}|\(\s*(?:[A-Z]+:\s*)?{t}\s*\)|(?:NYSE|NASDAQ|Nasdaq|AMEX)\s*:\s*{t}", title):
        return True
    if len(ticker) >= 3 and re.search(rf"(?<![A-Za-z]){t}(?![A-Za-z])", title):
        return True
    core = " ".join(core_name(name).split()[:2])
    return len(core) >= 3 and core.lower() in title.lower()


def parse(xml: bytes, ticker: str, name: str, now: datetime) -> dict | None:
    """The newest item in a Google News RSS document that names the company."""
    best = None
    for it in ET.fromstring(xml).iter("item"):
        title = (it.findtext("title") or "").strip()
        src = it.find("source")
        source = (src.text if src is not None and src.text else "").strip()
        # Google links through its own redirect; the publisher is the source's url.
        if src is not None and is_walled(src.get("url", "")):
            continue
        if source and title.endswith(" - " + source):      # Google appends " - Source"
            title = title[: -len(source) - 3].strip()
        try:
            when = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if now - when > timedelta(hours=MAX_AGE_H) or not names_it(title, ticker, name):
            continue
        link = (it.findtext("link") or "").strip()
        if not link.startswith("https://"):
            continue
        if best is None or when > best["_when"]:
            best = {"title": title, "link": link, "source": source or "Google News",
                    "published": when.astimezone(timezone.utc).isoformat(), "_when": when}
    if best:
        best.pop("_when")
    return best


def find(ticker: str, name: str, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    who = core_name(name) or ticker
    q = urllib.parse.quote(f'"{who}" stock when:2d')
    try:
        req = urllib.request.Request(SEARCH.format(q=q), headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            return parse(r.read(), ticker, name, now)
    except Exception as e:                       # a failed search is no headline, never a crash
        print(f"web_headline: {ticker}: {e}", flush=True)
        return None
    finally:
        time.sleep(PAUSE_S)
