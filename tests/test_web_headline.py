"""A headline from the open web for a mover our feeds missed (2026-10-14):
only a story that names the company counts, the newest wins, nothing older
than the window, and a mover our feeds already cover is not searched."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scan"))
import fetch_movers  # noqa: E402
from web_headline import core_name, names_it, parse  # noqa: E402

NOW = datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc)


def rss(*items):
    body = "".join(
        f"<item><title>{t} - {s}</title><link>https://news.google.com/rss/articles/{i}</link>"
        f"<pubDate>{(NOW - timedelta(hours=h)).strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>"
        f"<source url='{u}'>{s}</source></item>"
        for i, (t, s, h, *rest) in enumerate(items) for u in [rest[0] if rest else 'https://x.com'])
    return f"<rss><channel>{body}</channel></rss>".encode()


def test_core_name_drops_corporate_suffixes_but_not_into_a_common_word():
    assert core_name("Halozyme Therapeutics") == "Halozyme"
    assert core_name("Bitdeer Technologies Group") == "Bitdeer"
    assert core_name("Apple Inc.") == "Apple"
    assert core_name("First Financial") == "First Financial"


def test_a_title_must_name_the_company():
    assert names_it("Why State Street (STT) Stock Is Down Today", "STT", "State Street")
    assert names_it("Liquidia and United Therapeutics settle", "LQDA", "Liquidia")
    assert not names_it("Markets slide as yields climb", "LQDA", "Liquidia")
    # Two-letter tickers that are English words count only in brackets or a cashtag.
    assert not names_it("Stocks turn ON a dime", "ON", "ON Semiconductor")
    assert names_it("Chipmaker (ON) jumps", "ON", "ON Semiconductor")


def test_the_newest_relevant_story_wins_and_old_or_unrelated_ones_do_not():
    doc = rss(("Liquidia shares soar on settlement", "IBD", 5),
              ("Liquidia stock jumps again", "Reuters", 1),
              ("Fed holds rates", "CNBC", 0.5),
              ("Liquidia last year", "Old", 60))
    hit = parse(doc, "LQDA", "Liquidia", NOW)
    assert hit["title"] == "Liquidia stock jumps again" and hit["source"] == "Reuters"
    assert hit["link"].startswith("https://")
    assert parse(rss(("Fed holds rates", "CNBC", 1)), "LQDA", "Liquidia", NOW) is None


def test_only_movers_our_feeds_missed_are_searched_and_misses_say_null(tmp_path, monkeypatch):
    feed = tmp_path / "headlines.json"
    feed.write_text('{"headlines":[{"published":"2026-10-14T18:00:00+00:00","tickers":["CRWV"]}]}',
                    encoding="utf-8")
    monkeypatch.setattr(fetch_movers, "HEADLINES", feed)
    asked = []

    def find(t, name, now):
        asked.append(t)
        return {"title": f"{name} moves", "link": "https://e", "source": "S",
                "published": now.isoformat()} if t == "LQDA" else None

    payload = {"as_of": "2026-10-14",
               "gainers": [{"ticker": "LQDA", "name": "Liquidia"}, {"ticker": "ZZZ", "name": "Zed"}],
               "losers": [{"ticker": "CRWV", "name": "CoreWeave"}],
               "movers": [{"ticker": "LQDA", "name": "Liquidia"}]}
    fetch_movers.add_web_headlines(payload, {}, NOW, find=find)
    assert asked == ["LQDA", "ZZZ"]                         # CRWV is in our feeds; LQDA asked once
    assert payload["gainers"][0]["web"]["title"] == "Liquidia moves"
    assert payload["movers"][0]["web"] == payload["gainers"][0]["web"]
    assert payload["gainers"][1]["web"] is None
    assert "web" not in payload["losers"][0]

    # Next refresh, same session: the find is reused, the miss is searched again.
    asked.clear()
    fresh = {"as_of": "2026-10-14", "gainers": [{"ticker": "LQDA", "name": "Liquidia"},
                                                {"ticker": "ZZZ", "name": "Zed"}],
             "losers": [], "movers": []}
    fetch_movers.add_web_headlines(fresh, payload, NOW + timedelta(minutes=30), find=find)
    assert asked == ["ZZZ"] and fresh["gainers"][0]["web"]["title"] == "Liquidia moves"


# ── companies only in the gainers and losers (2026-10-14) ───────────────────

def test_funds_drop_out_by_security_type_even_with_no_name():
    asked = []

    def lookup(t):
        asked.append(t)
        return {"FNGD": "ETF", "VXX": "ETF", "HALO": "EQUITY", "LQDA": "EQUITY"}.get(t)

    types = {}
    pool = [("FNGD", {}), ("LQDA", {}), ("VXX", {}), ("HALO", {})]
    got = [t for t, _ in fetch_movers.top_companies(pool, {"LQDA": "Liquidia"}, types, lookup)]
    assert got == ["LQDA", "HALO"]
    assert types == {"FNGD": "ETF", "VXX": "ETF", "HALO": "EQUITY", "LQDA": "EQUITY"}
    asked.clear()
    fetch_movers.top_companies(pool, {}, types, lookup)
    assert asked == []                                       # cached: Yahoo asked once per ticker


def test_when_yahoo_cannot_say_the_name_decides():
    none = lambda t: None
    assert fetch_movers.is_company("ACME", "Acme Corp", {}, none)
    assert not fetch_movers.is_company("XYZU", "Direxion Daily Bull 3X Shares", {}, none)
    assert not fetch_movers.is_company("NONAME", "", {}, none)


# ── no headlines we cannot open (2026-10-14) ──────────────────────────────────

def test_a_walled_publisher_is_skipped_for_the_next_free_one():
    doc = rss(("Liquidia soars, subscribers read why", "Investor's Business Daily", 1, "https://www.investors.com"),
              ("Liquidia stock jumps on settlement", "Nasdaq", 3, "https://www.nasdaq.com"))
    assert parse(doc, "LQDA", "Liquidia", NOW)["source"] == "Nasdaq"
    only_walled = rss(("Liquidia rallies", "Barron's", 1, "https://www.barrons.com"))
    assert parse(only_walled, "LQDA", "Liquidia", NOW) is None


def test_the_paywall_list():
    from paywall import is_walled
    for u in ("https://www.wsj.com/a", "https://seekingalpha.com/news/1", "https://www.ft.com/content/x",
              "https://www.marketwatch.com/story/x", "https://www.cnbc.com/2026/10/14/pro/x.html"):
        assert is_walled(u), u
    for u in ("https://www.cnbc.com/2026/10/14/x.html", "https://markets.businessinsider.com/news/x",
              "https://www.nasdaq.com/articles/x", "https://news.google.com/rss/articles/x", ""):
        assert not is_walled(u), u
