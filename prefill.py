"""Paste a posting link, get company, role, salary and location.

For the Add application row in the tracker. Most boards (Greenhouse, Lever, Ashby, LinkedIn public pages) embed a
schema.org JobPosting JSON-LD block; meta tags and a pay-range regex are
the fallbacks.
"""

import json
import re

import requests

from tracker import normalize_salary as _normalize_salary

def _jsonld_job_postings(html):
    postings = []
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I,
    ):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            for node in [item] + (item.get("@graph") or []):
                if not isinstance(node, dict):
                    continue
                node_type = node.get("@type")
                types = node_type if isinstance(node_type, list) else [node_type]
                if "JobPosting" in types:
                    postings.append(node)
    return postings


def _salary_from_jsonld(posting):
    base = posting.get("baseSalary")
    if not isinstance(base, dict):
        return ""
    value = base.get("value")
    if isinstance(value, dict):
        unit = (value.get("unitText") or "").upper()
        lo, hi = value.get("minValue"), value.get("maxValue")
        single = value.get("value")
        nums = [n for n in (lo, hi) if isinstance(n, (int, float))]
        if not nums and isinstance(single, (int, float)):
            nums = [single]
        # Annual figures only; hourly/monthly rates would render as nonsense
        if nums and (unit in ("", "YEAR") and min(nums) >= 10000):
            return _normalize_salary(" - ".join(f"${n:,.0f}" for n in nums))
    return ""


def _salary_from_text(html):
    # CA/NY pay-transparency ranges in the body, e.g. "$150,000 - $185,000"
    m = re.search(
        r"\$\s*(\d{2,3}),?(\d{3})(?:\.\d+)?\s*(?:-|–|\u2014|to)\s*\$?\s*(\d{2,3}),?(\d{3})",
        html,
    )
    if m:
        return _normalize_salary(f"${m.group(1)}{m.group(2)} - ${m.group(3)}{m.group(4)}")
    m = re.search(r"\$\s*(\d{2,3})\s*[kK]\s*(?:-|–|\u2014|to)\s*\$?\s*(\d{2,3})\s*[kK]", html)
    if m:
        return _normalize_salary(f"${m.group(1)}K - ${m.group(2)}K")
    return ""


def _company_from_url(url):
    # Board URLs carry the company slug: boards.greenhouse.io/<co>/...,
    # jobs.lever.co/<co>/..., jobs.ashbyhq.com/<co>/...
    m = re.search(
        r"(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io|jobs\.lever\.co|jobs\.ashbyhq\.com)/([^/?#]+)",
        url, re.I,
    )
    if m and m.group(1).lower() not in ("embed", "jobs"):
        return m.group(1).replace("-", " ").title()
    return ""


def _meta_content(html, prop):
    m = re.search(
        r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.I,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\']',
        html, re.I,
    )
    return m.group(1).strip() if m else ""


def _extract_job_info(html, url):
    info = {"company": "", "salary": "", "location": "", "title": ""}

    for posting in _jsonld_job_postings(html):
        org = posting.get("hiringOrganization")
        if isinstance(org, dict) and org.get("name"):
            info["company"] = str(org["name"]).strip()
        elif isinstance(org, str):
            info["company"] = org.strip()
        if posting.get("title"):
            info["title"] = str(posting["title"]).strip()
        if not info["salary"]:
            info["salary"] = _salary_from_jsonld(posting)
        loc_type = posting.get("jobLocationType") or ""
        if "TELECOMMUTE" in str(loc_type).upper():
            info["location"] = "Remote"
        elif posting.get("jobLocation"):
            info["location"] = "On-site"
        if info["company"]:
            break

    lowered = html.lower()
    if info["location"] != "Remote" and re.search(
        r"hybrid\s+(?:work|role|schedule|model|environment|position)|\(hybrid\)", lowered
    ):
        info["location"] = "Hybrid"

    if not info["company"]:
        info["company"] = _company_from_url(url)
    if not info["company"]:
        # Greenhouse: <title>Job Application for <role> at <company></title>
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        title_tag = m.group(1).strip() if m else ""
        m = re.search(r"\bat\s+([^|<\-]+?)\s*$", title_tag)
        if m:
            info["company"] = m.group(1).strip()
    if not info["company"]:
        site = _meta_content(html, "og:site_name")
        if site and site.lower() not in ("linkedin", "greenhouse", "lever", "indeed", "glassdoor"):
            info["company"] = site

    if not info["salary"]:
        info["salary"] = _salary_from_text(html)

    return info


def _prefill_reason(resp, info):
    """Why a prefill came back empty, or "" when it did not.

    Every failure used to look the same from the page's side: empty fields and
    no explanation, which is exactly what a paste that never registered looks
    like. Naming the reason is the difference between "nothing happened" and
    "that posting wants a login".
    """
    if resp is None:
        return "unreachable"
    status = getattr(resp, "status_code", 0)
    if status in (401, 403, 429):
        # Wants a login, or does not serve robots. Either way, not readable.
        return "blocked"
    if status in (404, 410):
        return "not_found"
    if status >= 400:
        return "unreachable"
    if any(info.get(field) for field in ("company", "salary", "location")):
        return ""
    # Read the page fine, found nothing in it worth filling in.
    return "nothing_found"




UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def prefill(url):
    """{company, title, salary, location, reason}. reason is "" when
    something was found, else unreachable, blocked, not_found or
    nothing_found, so the page can say why the fields stayed empty."""
    empty = {"company": "", "salary": "", "location": "", "title": ""}
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    except Exception:
        return {**empty, "reason": "unreachable"}
    info = _extract_job_info(resp.text[:2_000_000], url) if resp.status_code < 400 else dict(empty)
    return {**info, "reason": _prefill_reason(resp, info)}
