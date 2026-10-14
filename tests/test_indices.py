"""The market strip (2026-10-14, Q65 = a): change is against the close BEFORE
the latest price's session, the 10-year moves in basis points, and an
instrument Yahoo did not return is left out."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scan"))
from fetch_indices import build  # noqa: E402


def frames():
    days = pd.to_datetime(["2026-10-12", "2026-10-13", "2026-10-14"])
    daily = pd.DataFrame({"^GSPC": [7800.0, 7805.0, 7713.0], "^TNX": [5.24, 5.22, 5.19]}, index=days)
    ts = pd.date_range("2026-10-14 13:30", periods=78, freq="5min", tz="UTC")
    intra = pd.DataFrame({"^GSPC": [7805.0] * 77 + [7727.0], "^TNX": [5.22] * 77 + [5.195]}, index=ts)
    return daily, intra


def test_change_is_against_the_prior_session_close():
    rows = {r["key"]: r for r in build(*frames())}
    spx = rows["^GSPC"]
    assert spx["prev"] == 7805.0 and spx["last"] == 7727.0
    assert spx["change_pct"] == round((7727 / 7805 - 1) * 100, 2)


def test_the_ten_year_moves_in_basis_points():
    tnx = {r["key"]: r for r in build(*frames())}["^TNX"]
    assert tnx["change_bp"] == -2.5 and "change_pct" not in tnx


def test_the_path_is_thinned_and_ends_on_the_last_price():
    spx = {r["key"]: r for r in build(*frames())}["^GSPC"]
    assert len(spx["path"]) <= 81 and spx["path"][-1][1] == 7727.0


def test_a_missing_instrument_is_left_out():
    keys = [r["key"] for r in build(*frames())]
    assert "^DJI" not in keys and keys == ["^GSPC", "^TNX"]
