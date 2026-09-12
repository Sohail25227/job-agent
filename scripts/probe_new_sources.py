"""Verify candidate job APIs before wiring them in as sources.

Two things are being checked, and the second is the one that matters:

1. Does the endpoint answer at all, with jobs in it?
2. Does it *honour the location filter*? An API that silently ignores an
   unrecognised city looks like a working source and quietly contributes
   thousands of postings from the wrong continent. The test is to ask for a
   city that cannot exist and compare the answer to a real one.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

NONSENSE_CITY = "Nowhereville"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8", "replace")
    # The Muse embeds raw control characters inside job HTML, which strict JSON
    # rejects. Every other board here is clean, so tolerate it rather than
    # losing the source.
    return json.loads(raw, strict=False)


def muse(location: str | None) -> tuple[int, list[str]]:
    url = "https://www.themuse.com/api/public/jobs?category=Software%20Engineering&page=1"
    if location:
        url += "&location=" + urllib.parse.quote(location)
    data = get(url)
    sample = []
    for job in (data.get("results") or [])[:3]:
        sample += [loc["name"] for loc in (job.get("locations") or [])]
    return int(data.get("total") or 0), sample


def jobdata(location: str | None, title: str | None = None) -> tuple[int, list[str]]:
    url = "https://jobdataapi.com/api/jobs/?country_code=IN&max_age=30"
    if location:
        url += "&location=" + urllib.parse.quote(location)
    if title:
        url += "&title=" + urllib.parse.quote(title)
    data = get(url)
    sample = [job.get("location") or "" for job in (data.get("results") or [])[:3]]
    return int(data.get("count") or 0), sample


def report(name: str, probe, cities: list[str]) -> None:
    print(f"\n=== {name}")
    baseline, _ = probe(NONSENSE_CITY)
    print(f"  unrecognised city returns {baseline} -> ", end="")
    print("filter is enforced" if baseline == 0 else "FILTER IGNORED, counts below must be read with care")

    for city in cities:
        try:
            total, sample = probe(city)
        except Exception as exc:
            print(f"  {city:22s} FAILED: {exc}")
            continue
        verdict = "ignored" if total == baseline and baseline else "ok"
        shown = ", ".join(dict.fromkeys(sample))[:70]
        print(f"  {city:22s} {total:6d}  [{verdict}]  {shown}")


if __name__ == "__main__":
    muse_cities = [
        "Gurgaon, India", "Gurugram, India", "New Delhi, India", "Delhi, India",
        "Noida, India", "Bangalore, India", "Bengaluru, India",
        "Hyderabad, India", "Pune, India", "Mumbai, India", "Chennai, India",
    ]
    jobdata_cities = [
        "Gurgaon", "Gurugram", "Delhi", "New Delhi", "Noida",
        "Bangalore", "Bengaluru", "Hyderabad", "Pune", "Mumbai", "Chennai",
    ]

    report("themuse", lambda c: muse(c), muse_cities)
    report("jobdataapi", lambda c: jobdata(c), jobdata_cities)

    print("\n=== jobdataapi, Gurgaon + keyword")
    for keyword in ["backend", "java", "python", "software engineer", "developer", ""]:
        try:
            total, _ = jobdata("Gurgaon", keyword or None)
        except Exception as exc:
            print(f"  {keyword or '(none)':20s} FAILED: {exc}")
            continue
        print(f"  {keyword or '(none)':20s} {total:6d}")

    sys.exit(0)
