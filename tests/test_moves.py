"""Move size: the last daily move in units of the stock's own 20-day
standard deviation, measured on the 20 returns before it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scan"))
from fetch_stocks import move_size  # noqa: E402


def closes(rets, start=100.0):
    out = [start]
    for r in rets:
        out.append(out[-1] * (1 + r))
    return out


def test_a_quiet_stock_moving_three_percent_is_a_big_day():
    calm = [0.01, -0.01] * 10                 # sd ~1.03%
    m = move_size(closes(calm + [0.03]))
    assert 2.7 < m["sigma"] < 3.0 and abs(m["vol_20d"] - 1.03) < 0.02


def test_the_move_does_not_dilute_its_own_yardstick():
    calm = [0.01, -0.01] * 10
    assert move_size(closes(calm + [0.10]))["vol_20d"] == move_size(closes(calm + [0.0]))["vol_20d"]


def test_a_down_move_is_negative_and_too_little_history_is_no_reading():
    assert move_size(closes([0.01, -0.01] * 10 + [-0.04]))["sigma"] < 0
    assert move_size(closes([0.01] * 5)) == {}


def test_top_movers_leave_out_funds_and_leveraged_products():
    from fetch_movers import FUND
    for name in ["Direxion Daily S&P Biotech Bear 3X Shares", "Defiance Daily Target 2X Long ORCL ETF",
                 "ProShares Ultra Bloomberg Natural Gas", "iShares Core MSCI EAFE"]:
        assert FUND.search(name), name
    for name in ["PBF Energy", "Venture Global", "Cipher Mining", "GitLab", "Applied Materials"]:
        assert not FUND.search(name), name
