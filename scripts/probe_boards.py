"""Check which ATS board slugs actually exist, and how many backend roles they list.

Board slugs change and companies migrate between ATS platforms, so guessing is
pointless. Run this against a candidate list and paste the winners into
config.yaml.

    python scripts/probe_boards.py                    # probe the built-in candidates
    python scripts/probe_boards.py --company razorpay --company zomato
    python scripts/probe_boards.py --india-only
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobagent.tls import enable_system_trust  # noqa: E402

enable_system_trust()

BACKEND = re.compile(
    r"backend|back-end|java|spring|microservice|platform engineer|api engineer"
    r"|software engineer|software development engineer|sde\b|distributed",
    re.I,
)
INDIA = re.compile(r"india|bengaluru|bangalore|hyderabad|pune|gurgaon|gurugram|noida|delhi|chennai|mumbai", re.I)

CANDIDATES = [
    # Indian product companies and unicorns
    "razorpay", "zomato", "swiggy", "meesho", "phonepe", "cred", "groww", "navi",
    "zepto", "dream11", "urbancompany", "lenskart", "cars24", "spinny", "slice",
    "juspay", "chargebee", "freshworks", "postman", "browserstack", "hasura",
    "harness", "whatfix", "mindtickle", "clevertap", "moengage", "darwinbox",
    "innovaccer", "leadsquared", "capillary", "sprinklr", "zeta", "epifi",
    "jupiter", "setu", "m2p", "perfios", "unacademy", "upgrad", "physicswallah",
    "fi-money", "khatabook", "yellowai", "haptik", "gupshup", "exotel",
    # Global companies with large India engineering teams
    "stripe", "databricks", "atlassian", "uber", "rippling", "notion", "figma",
    "canva", "mongodb", "confluent", "cloudflare", "datadog", "twilio", "gitlab",
    "dropbox", "coinbase", "doordash", "hashicorp", "samsara", "plaid", "airbnb",
    "anthropic", "openai", "linear", "vanta", "clickhouse", "deel", "ramp",
    "netflix", "mistral", "flexport", "benchling", "wise", "revolut", "grammarly",
    "miro", "gojek", "grab", "sea", "shopify",
]

PROBES = {
    "greenhouse": (
        "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        lambda data: data.get("jobs", []),
        lambda job: (job.get("title") or "", (job.get("location") or {}).get("name") or ""),
    ),
    "lever": (
        "https://api.lever.co/v0/postings/{slug}?mode=json",
        lambda data: data if isinstance(data, list) else [],
        lambda job: (job.get("text") or "", (job.get("categories") or {}).get("location") or ""),
    ),
    "ashby": (
        "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        lambda data: data.get("jobs", []) if isinstance(data, dict) else [],
        lambda job: (job.get("title") or "", job.get("location") or ""),
    ),
}


def probe(slug: str, ats: str, india_only: bool) -> dict | None:
    url_template, extract, fields = PROBES[ats]
    try:
        response = httpx.get(
            url_template.format(slug=slug),
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "job-agent board probe (personal job search)"},
        )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        jobs = extract(response.json())
    except json.JSONDecodeError:
        return None
    if not jobs:
        return None

    backend = 0
    india = 0
    samples: list[str] = []
    for job in jobs:
        title, location = fields(job)
        if BACKEND.search(title):
            if india_only and not INDIA.search(location):
                continue
            backend += 1
            if INDIA.search(location):
                india += 1
            if len(samples) < 3:
                samples.append(f"{title.strip()[:52]} ({location.strip()[:28]})")
    if not backend:
        return None
    return {
        "slug": slug, "ats": ats, "total": len(jobs),
        "backend": backend, "india": india, "samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", "-c", action="append", default=[])
    parser.add_argument("--india-only", action="store_true",
                        help="Only count backend roles located in India.")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    slugs = args.company or CANDIDATES
    tasks = [(slug, ats) for slug in slugs for ats in PROBES]
    results: list[dict] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(probe, slug, ats, args.india_only): (slug, ats) for slug, ats in tasks}
        for future in concurrent.futures.as_completed(futures):
            found = future.result()
            if found:
                results.append(found)

    results.sort(key=lambda r: (-r["india"], -r["backend"]))
    by_ats: dict[str, list[str]] = {}
    print(f"\n{'ATS':<16}{'BOARD':<20}{'BACKEND':>8}{'IN INDIA':>10}   SAMPLE")
    print("-" * 110)
    for row in results:
        by_ats.setdefault(row["ats"], []).append(row["slug"])
        print(f"{row['ats']:<16}{row['slug']:<20}{row['backend']:>8}{row['india']:>10}   "
              f"{row['samples'][0] if row['samples'] else ''}")

    print("\nPaste into config/config.yaml:\n")
    for ats, found in sorted(by_ats.items()):
        print(f"  {ats}:")
        print("    enabled: true")
        print("    boards:" if ats != "smartrecruiters" else "    companies:")
        for slug in sorted(found):
            print(f"      - {slug}")
    print(f"\n{len(results)} live board(s) out of {len(tasks)} probes.")


if __name__ == "__main__":
    main()
