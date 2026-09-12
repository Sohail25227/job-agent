"""Check which sources actually answer from this network.

Corporate networks block some job sites outright, and free APIs change or retire
endpoints without notice. Run this before trusting a config: it hits every
source once and reports what came back.

    python scripts/probe_sources.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobagent.config import load_config  # noqa: E402
from jobagent.http import HttpClient  # noqa: E402
from jobagent.sources import ALL_SOURCES  # noqa: E402
from jobagent.tls import enable_system_trust  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# Probes hit one endpoint per source with a shape that needs no configuration.
PROBES: dict[str, tuple[str, str]] = {
    "greenhouse": ("GET", "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"),
    "lever": ("GET", "https://api.lever.co/v0/postings/matchgroup?mode=json"),
    "ashby": ("GET", "https://api.ashbyhq.com/posting-api/job-board/ramp"),
    "smartrecruiters": ("GET", "https://api.smartrecruiters.com/v1/companies/Visa/postings?limit=1"),
    "workable": ("GET", "https://apply.workable.com/api/v1/widget/accounts/hotjar?details=true"),
    # Recruitee has no public demo tenant; it only answers for a real company
    # slug, so there is nothing generic to probe.
    "remotive": ("GET", "https://remotive.com/api/remote-jobs?limit=1"),
    "remoteok": ("GET", "https://remoteok.com/api"),
    "arbeitnow": ("GET", "https://www.arbeitnow.com/api/job-board-api?page=1"),
    "himalayas": ("GET", "https://himalayas.app/jobs/api?limit=5&offset=0"),
    "jobicy": ("GET", "https://jobicy.com/api/v2/remote-jobs?count=5&industry=engineering"),
    "weworkremotely": ("GET", "https://weworkremotely.com/categories/remote-programming-jobs.rss"),
    "linkedin": (
        "GET",
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        "?keywords=java&location=India&start=0",
    ),
    "naukri": ("GET", "https://www.naukri.com/jobapi/v3/search?noOfResults=5&keyword=java"),
    "workday": (
        "POST",
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs",
    ),
}


def describe(payload) -> str:
    if isinstance(payload, dict):
        for key in ("jobs", "results", "data", "offers", "content", "jobPostings"):
            if isinstance(payload.get(key), list):
                return f"{len(payload[key])} items under {key!r}"
        return f"dict with keys {sorted(payload)[:6]}"
    if isinstance(payload, list):
        return f"list of {len(payload)}"
    text = str(payload)
    if "base-card" in text:
        return f"{text.count('base-card')} job cards in HTML"
    if "<item>" in text:
        return f"{text.count('<item>')} RSS items"
    return f"{type(payload).__name__}, {len(text)} chars"


def main() -> int:
    enable_system_trust()
    config = load_config()
    client = HttpClient(config)
    known = {cls.name for cls in ALL_SOURCES}
    failures = 0

    for name in sorted(known):
        probe = PROBES.get(name)
        if not probe:
            print(f"  ?  {name:<16} no probe defined")
            continue
        method, url = probe
        try:
            if method == "POST":
                payload = client.post_json(
                    url,
                    json={"appliedFacets": {}, "limit": 5, "offset": 0, "searchText": "software"},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
            elif url.endswith(".rss") or "linkedin.com" in url:
                # LinkedIn's guest endpoint returns an HTML fragment, not JSON.
                payload = client.get_text(url)
            else:
                payload = client.get_json(url)
            print(f"  OK {name:<16} {describe(payload)}")
        except Exception as exc:
            failures += 1
            print(f"  XX {name:<16} {type(exc).__name__}: {str(exc)[:110]}")

    keyed = {
        "adzuna": config.sources.adzuna,
        "jooble": config.sources.jooble,
    }
    for name, settings in keyed.items():
        state = "credentials present" if settings.credentials_present else "NO API KEY in .env"
        print(f"  -- {name:<16} {state}")

    client.close()
    print(f"\n{len(PROBES) - failures}/{len(PROBES)} reachable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
