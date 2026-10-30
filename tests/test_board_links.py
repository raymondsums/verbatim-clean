"""A posting link names the board it came from, so applying can watch it."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: E402,F401  keep tests off the real vault and .env

from jobs import boards, watch  # noqa: E402


def test_links_name_their_board():
    cases = {
        "https://job-boards.greenhouse.io/doordashusa/jobs/7012": ("greenhouse", "doordashusa"),
        "https://jobs.ashbyhq.com/Vandelay/1f2e": ("ashby", "Vandelay"),
        "https://jobs.lever.co/xsolla/abc-123": ("lever", "xsolla"),
        "https://apply.workable.com/serko-ltd/j/6BEBC6A0BB/": ("workable", "serko-ltd"),
        "https://tyrell.wd5.myworkdayjobs.com/en-US/zillow_group_external/job/Designer_P1":
            ("workday", "tyrell|wd5|zillow_group_external"),
        "https://turo.wd12.myworkdayjobs.com/Turo_careers/job/SF/Designer_R-1":
            ("workday", "turo|wd12|Turo_careers"),
        "https://jobs.smartrecruiters.com/Wayneco/6000000001514798-account-executive":
            ("smartrecruiters", "Wayneco"),
        "https://ats.rippling.com/adapt/jobs/a24f6f2e-aef0?jobSite=LinkedIn": ("rippling", "adapt"),
        "https://jobs.gem.com/spotnana/am9icG9zdDpj2FO6g?utm_source=x": ("gem", "spotnana"),
    }
    for url, want in cases.items():
        assert boards.board_from_url(url) == want, url
    assert boards.board_from_url("https://www.linkedin.com/jobs/view/1") is None
    # An embed link with its query string cut off names no board.
    assert boards.board_from_url("https://job-boards.greenhouse.io/embed/job_app") is None
    assert boards.board_from_url(
        "https://boards.greenhouse.io/embed/job_app?for=seatgeek&token=1") == ("greenhouse", "seatgeek")


def test_workday_posted_dates():
    assert watch._workday_posted("Posted Today")
    assert watch._workday_posted("Posted 30+ Days Ago")
    assert watch._workday_posted("") == ""


def test_new_systems_read_as_postings(monkeypatch):
    feeds = {
        "smartrecruiters.com": {"totalFound": 1, "content": [{
            "id": "77", "name": "Product Designer", "releasedDate": "2026-10-20T10:00:00.000Z",
            "location": {"fullLocation": "San Francisco, CA, United States", "remote": False}}]},
        "rippling.com": [{"uuid": "u1", "name": "Product Designer",
                          "url": "https://ats.rippling.com/adapt/jobs/u1",
                          "workLocation": {"label": "San Francisco, CA"}}],
        "gem.com": [{"id": 5, "title": "Product Designer", "absolute_url": "https://jobs.gem.com/x/5",
                     "location": {"name": "Remote, US"}, "location_type": "remote",
                     "first_published_at": "2026-10-01T00:00:00.000Z", "content_plain": "Design."}],
    }
    monkeypatch.setattr(watch, "_get", lambda url: next(v for k, v in feeds.items() if k in url))
    sr = watch.fetch_smartrecruiters("Wayneco")[0]
    assert (sr["pid"], sr["posted"], sr["url"]) == (
        "77", "2026-10-20T10:00:00", "https://jobs.smartrecruiters.com/Wayneco/77")
    rp = watch.fetch_rippling("adapt")[0]
    assert (rp["pid"], rp["location"], rp["text"]) == ("u1", "San Francisco, CA", None)
    gm = watch.fetch_gem("x")[0]
    assert (gm["pid"], gm["remote"], gm["text"]) == ("5", True, "Design.")
