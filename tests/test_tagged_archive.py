"""tagged.json keeps ticker-tagged stories for 72 h, past headlines.json's 600 cap
(owner Q109 = c, 2026-10-26: finvisible's Morning tab lost yesterday afternoon)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scan"))
import fetch_news as F  # noqa: E402

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def _h(i, hours_ago, tickers, **kw):
    return {"id": str(i), "title": f"t{i}", "link": f"https://x/{i}", "source": "S",
            "summary": "long text", "published": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "tickers": tickers, **kw}


def test_keeps_tagged_stories_the_cap_dropped_and_ages_them_out():
    previous = [_h(1, 60, ["AAPL"]), _h(2, 80, ["MSFT"])]          # 2 is past 72 h
    stories = [_h(3, 1, ["NVDA"]), _h(4, 2, []), _h(1, 60, ["AAPL", "TSM"])]
    out = F.tagged_archive(previous, stories, NOW)
    assert [h["id"] for h in out] == ["3", "1"]                    # untagged and too old out
    assert out[1]["tickers"] == ["AAPL", "TSM"]                     # this run's tags win
    assert set(out[0]) == set(F.TAGGED_FIELDS)                      # summary dropped
