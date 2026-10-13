"""Ticker tagging and story types, pinned to real headlines from the
2026-10-13 audit (before: 289 of 384 tags came from a company-name match,
124 of them "News" -> NWSA)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scan"))
import fetch_news as F  # noqa: E402

tags = F.extract_tickers


@pytest.mark.parametrize("text,wrong", [
    ("AmeraMex International Secures $1.4 Million in New Orders | AMMX Stock News", "NWSA"),
    ("Bank earnings live updates: JPMorgan and Goldman execs discuss AI", "JIRE"),
    ("Schwab clients add to equities in September", "SCHF"),
    ("Sage Potash Confirms Multiple Water Bearing Zones - Advancing Development Strategy", "MSTR"),
    ("Chemours downgraded by RBC on weak chemical demand", "RBC"),
    ("TD Cowen reiterates Buy on Axon stock", "TD"),
    ("Itron and PG&E Advance Strategic Collaboration on Wildfire Mitigation", "PG"),
    ("Anixa's Ovarian Cancer CAR-T Therapy Inventor to Deliver Keynote", "CAR"),
    ("TEQUILA DON JULIO INTRODUCES EL PUENTE ETERNO IN CELEBRATION OF DIA DE MUERTOS", "DIA"),
    ("Hilltop Residential Ranks No. 8 on the 2026 Fortune Best Workplaces List", "FBIN"),
], ids=["news", "fund-brand", "fund-brand-schwab", "strategy", "broker-rbc", "broker-td",
        "glued-amp", "glued-suffix", "shouting", "fortune"])
def test_false_tags_are_gone(text, wrong):
    assert wrong not in tags(text)


@pytest.mark.parametrize("text,right", [
    ("Bank earnings live updates: JPMorgan and Goldman Sachs execs discuss AI", "JPM"),
    ("SA analyst upgrades/downgrades: META, VTRS, MRVL, COKE", "VTRS"),
    ("Why did investors react so negatively to Schneider-PTC deal?", "PTC"),
    ("U.S. builds AI infrastructure while Europe leads in adoption, Apollo says", "APO"),
    ("Micron, Seagate, Western Digital sink as chip sell-off deepens", "MU"),
    ("Tesla (TSLA) recalls 10,000 vehicles", "TSLA"),
    ("BP stock hits 52-week high at 48.29 USD", "BP"),
], ids=["jpm", "ticker-list", "hyphen-pair", "apollo", "name", "explicit", "bp"])
def test_real_tags_survive(text, right):
    assert right in tags(text)


@pytest.mark.parametrize("title,typ", [
    ("Bank earnings live updates: JPMorgan and Goldman execs discuss AI, deal backlog", "earnings"),
    ("Johnson & Johnson beats estimates, raises 2026 guidance", "earnings"),
    ("A. O. Smith Corporation raises dividend by 5.6% to $0.38", "dividend"),
    ("Chemours downgraded by RBC on weak chemical demand", "downgrade"),
    ("Jefferies upgrades Nike to Buy", "upgrade"),
    ("Merck to buy Verona Pharma for $10 billion", "ma"),
], ids=["bank-earnings", "beat-and-raise", "dividend", "downgrade", "upgrade", "ma"])
def test_story_types(title, typ):
    assert F.classify(title, "stocks") == typ


def test_raising_money_is_not_an_upgrade():
    assert F.classify("Nutriband raises $1.75M from warrant exercises", "press") != "upgrade"


def test_a_fund_keeps_its_fund_name():
    names = {n: t for n, t in F.NAME_MAP}
    assert names.get("JPMorgan") != "JIRE"
    assert all(F._FUND_WORDS.search(n) for n, t in F.NAME_MAP if t == "JIRE")
