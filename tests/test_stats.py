"""Stats page numbers (stats.py)."""
from datetime import date

import stats


def row(status, day, **extra):
    r = {"status": status, "date": day, "role": "Senior Product Designer", "job_id": None,
         "rejected_at": None, "rejected_on": None, "last_contact": None, "source": None}
    r.update(extra)
    return r


TODAY = date(2026, 10, 23)


def test_rates_need_enough_rows_and_always_carry_counts():
    rows = [row("Applied", "2026-10-20"), row("Rejected", "2026-10-01", rejected_on="2026-10-05")]
    d = stats.compute(rows, TODAY)
    assert d["rejected"] == {"n": 1, "of": 2, "pct": None}
    rows += [row("No response", "2026-09-01")] * 4
    d = stats.compute(rows, TODAY)
    assert d["rejected"]["pct"] == 17 and d["silent"]["n"] == 4


def test_funnel_counts_the_furthest_stage_including_rejections():
    rows = [row("Applied", "2026-10-20"), row("Phone Screen", "2026-10-10"),
            row("Rejected", "2026-10-01", rejected_at="Interviewing"), row("Saved", None)]
    f = {x["stage"]: x["n"] for x in stats.compute(rows, TODAY)["funnel"]}
    assert f == {"Saved": 4, "Applied": 3, "Phone Screen": 2, "Interviewing": 1, "Onsite": 0, "Offer": 0}


def test_no_trend_from_a_handful_and_no_median_from_too_few():
    rows = [row("Rejected", "2026-10-1%d" % i, rejected_on="2026-10-2%d" % i) for i in range(3)]
    d = stats.compute(rows, TODAY)
    assert not d["trend"]["enough"]
    assert d["to_rejection"] == {"n": 3, "days": None}


def test_velocity_is_by_week_with_this_week_last():
    rows = [row("Applied", "2026-10-20"), row("Applied", "2026-10-21"), row("Applied", "2026-10-13")]
    w = stats.compute(rows, TODAY)["weeks"]
    assert len(w) == 12 and w[-1] == {"start": "2026-10-19", "n": 2, "current": True} and w[-2]["n"] == 1
