"""Duplicate detection against the applications tracker.

The tracker has no role field, so a posting match (same job id on the same
board) is definite and a company match is only a hint. These tests use a
fixed job list; nothing here touches the network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from applied import _job_key, _norm_company, check_duplicate

JOBS = [
    {"company": "Initech", "date": "2026-08-19", "status": "Applied",
     "link": "https://initech.wd5.myworkdayjobs.com/en-US/site/job/R2023633"},
    {"company": "Globex", "date": "2026-08-11", "status": "Phone Screen",
     "link": "https://boards.greenhouse.io/globex/jobs/6143552004"},
    {"company": "Airbnb", "date": "2026-07-02", "status": "Rejected",
     "link": ""},
]


def test_same_posting_with_tracking_params_still_matches():
    found = check_duplicate(
        "", "https://boards.greenhouse.io/globex/jobs/6143552004?gh_src=x",
        jobs=JOBS)
    assert found["posting"]["company"] == "Globex"


def test_same_posting_on_the_other_greenhouse_host_matches():
    found = check_duplicate(
        "", "https://job-boards.greenhouse.io/globex/jobs/6143552004",
        jobs=JOBS)
    assert found["posting"]["company"] == "Globex"


def test_trailing_slash_and_scheme_are_ignored():
    found = check_duplicate(
        "", "http://boards.greenhouse.io/globex/jobs/6143552004/", jobs=JOBS)
    assert found["posting"] is not None


def test_a_different_role_at_the_same_company_is_not_a_posting_match():
    found = check_duplicate(
        "Globex", "https://boards.greenhouse.io/globex/jobs/9999999999",
        jobs=JOBS)
    assert found["posting"] is None
    assert [j["company"] for j in found["company"]] == ["Globex"]


def test_company_match_survives_suffixes_and_case():
    found = check_duplicate("INITECH, Inc.", "", jobs=JOBS)
    assert [j["company"] for j in found["company"]] == ["Initech"]


def test_a_genuinely_new_job_matches_nothing():
    found = check_duplicate("Some New Co", "https://example.com/jobs/1", jobs=JOBS)
    assert found["posting"] is None
    assert found["company"] == []


def test_rows_without_a_link_never_match_a_posting():
    found = check_duplicate("Airbnb", "", jobs=JOBS)
    assert found["posting"] is None
    assert [j["company"] for j in found["company"]] == ["Airbnb"]


def test_empty_url_does_not_collide_with_empty_links():
    assert _job_key("https://boards.greenhouse.io/embed/job_app?for=x&token=7811967") == \
        _job_key("https://job-boards.greenhouse.io/x/jobs/7811967")
    assert _job_key("https://boards.greenhouse.io/embed/job_app?for=a&token=1111111") != \
        _job_key("https://boards.greenhouse.io/embed/job_app?for=b&token=2222222")
    assert _job_key("https://jobs.ashbyhq.com/o/43ba8b53-4643-4544-bb21-f13efe99e0a0?jr_id=1") == \
        _job_key("https://www.homebound.com/careers?ashby_jid=43ba8b53-4643-4544-bb21-f13efe99e0a0&jr_id=6a6")
    assert _job_key("https://www.okta.com/company/careers/rd/staff-product-designer-auth0-8078764/") == \
        _job_key("https://www.okta.com/company/careers/opportunity/8078764?gh_jid=8078764")
    assert _job_key("") == ""
    assert _job_key("   ") == ""


def test_company_normalisation():
    assert _norm_company("The Globex Co.") == "globex"
    assert _norm_company("GLOBEX, LLC") == "globex"
