"""
paywall.py — sites whose articles need a login or a subscription (owner,
2026-10-14: "some of these headline sources require a login"). A headline we
cannot open is not a headline: no feed from these sites, no web-search result
from them, and a stored story linking to one is purged.

Listed by host suffix. A free section of a walled site is an explicit
exception (markets.businessinsider.com is free; businessinsider.com is not).
"""
from urllib.parse import urlparse

WALLED = {
    "wsj.com", "barrons.com", "marketwatch.com",          # Dow Jones: subscription
    "bloomberg.com", "ft.com", "economist.com",
    "nytimes.com", "washingtonpost.com", "latimes.com",
    "seekingalpha.com",                                    # login wall after the first screen
    "investors.com",                                       # IBD
    "reuters.com",                                         # registration wall
    "businessinsider.com", "fortune.com", "forbes.com",    # metered, then sign-in
    "theinformation.com", "theglobeandmail.com", "theatlantic.com",
    "asia.nikkei.com", "nikkei.com", "telegraph.co.uk", "thetimes.co.uk",
    "morningstar.com",                                     # premium analysis behind sign-in
}
FREE_EXCEPTIONS = {"markets.businessinsider.com"}


def host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_walled(url: str) -> bool:
    h = host(url)
    if not h or h in FREE_EXCEPTIONS:
        return False
    if "cnbc.com" in h and "/pro/" in url:                 # CNBC Pro is subscription-only
        return True
    return any(h == d or h.endswith("." + d) for d in WALLED)
