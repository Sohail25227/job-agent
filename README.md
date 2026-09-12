# job-agent

Anyone signs up, uploads a resume, types a role and a few cities, and presses
search. It goes out to every job board that publishes a usable API **at that
moment**, in parallel, and comes back in ten to fifteen seconds with one ranked
list and the employer's own apply link on every row.

**It never applies for you.** No form filling, no auto-submit, no logging into a
job board as you. That line exists because the failure mode on the other side of
it is a bad application sent under your name to a company you wanted.

Your resume is read once to work out what you can do. It is not stored as a
file, not sent to an employer, and never rewritten.

---

## What actually happens when you press search

```
                    ┌─ live, while you wait (~12s) ────────────────┐
press search  ──►   │ LinkedIn · Adzuna · Jooble · Workday ·       │  ──►  ranked
                    │ The Muse · Remotive · RemoteOK · Jobicy ·    │       list
                    │ Himalayas · Arbeitnow · WeWorkRemotely       │
                    └──────────────────────────────────────────────┘
                    ┌─ background, twice a day ────────────────────┐
                    │ the same eleven, plus Greenhouse · Lever ·   │
                    │ Ashby · SmartRecruiters · JobDataAPI, plus   │
                    │ the full description text                    │
                    └──────────────────────────────────────────────┘
```

Two paths, because one shape cannot serve both:

| | Live path | Background crawl |
|---|---|---|
| Runs | when you press search | on a schedule, nobody waiting |
| Sources | the eleven a keyword can steer | all sixteen — adds the slug-addressed boards and JobDataAPI |
| Descriptions | skipped — this is the whole trick | fetched and filled in |
| HTTP timeout | 8s, one attempt | 30s, three attempts |
| Takes | 10-15 seconds | as long as it needs |

Fetching the full text of every card is ten to twenty times the requests. Doing
it on the live path was what made a search take eighteen minutes; skipping it
brought the same search down to about twelve. The descriptions still arrive,
just from the crawl instead, which is also what fills in the boards no keyword
can reach.

The short live timeout matters as much as it looks trivial. With the crawl's 30s
timeout, one stalled source held the whole search at 31s while the other nine
finished inside 8s — and then returned nothing anyway. Waiting 8s and moving on
costs only that source, which the crawl picks up later regardless.

Openings land in **one shared cache**, so a search by one user warms it for
everyone. Scores are *not* cached: they are computed per request from the
reader's own resume, because the same posting is a different opportunity for two
different people. A Java role that scores 74 for a backend engineer scores 40
for a frontend one, off the same row.

---

## Deploy it for free

Three services, no card, nothing to pay:

| Piece | Where | Why not the obvious choice |
|---|---|---|
| Web app | Render free web service | — |
| Database | Neon free Postgres | Render's own free Postgres is deleted after 30 days |
| Scheduled crawl | GitHub Actions cron | Render's free tier has no background worker and sleeps when idle |

### 1. Database

Create a project at [neon.com](https://neon.com) and copy the connection string.
It looks like `postgresql://user:pass@ep-xxx.aws.neon.tech/neondb?sslmode=require`.
Any managed Postgres works — Supabase, Aiven, your own — this is just the one
whose free tier does not expire.

### 2. Web app

Push this repository to GitHub, then in Render: **New → Blueprint**, pick the
repo. `render.yaml` describes the service, so the only thing to fill in is
`DATABASE_URL`. Optionally add `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` and
`JOOBLE_API_KEY` (both free, about a minute each) for wider India coverage.

`SECRET_KEY` is generated for you and must then be left alone — changing it logs
every user out.

### 3. Scheduled crawl

In the GitHub repo, add the same values under **Settings → Secrets and variables
→ Actions**: `DATABASE_URL`, and the Adzuna/Jooble keys if you have them. The
workflow in `.github/workflows/crawl.yml` then runs twice a day, and can be
triggered by hand from the Actions tab.

### What the free tier will actually do to you

Worth knowing before you send anyone the link, rather than after:

- **First visit after 15 idle minutes takes 30-60 seconds.** Render stops free
  instances when nobody is using them. A search itself is fast; it is the wake-up
  that is slow. Letting it sleep is the right default — see the warning below
  before reaching for an uptime pinger to hide it.
- **LinkedIn may refuse a datacenter IP.** It is the single richest source of
  India roles and it is enabled here, but the guest endpoint is far more likely
  to rate-limit a cloud address than your laptop. If `linkedin` starts returning
  nothing after deploying, that is why — the other nine sources carry on. Its
  use is also against LinkedIn's terms of service; see below.
- **Adzuna's free key is 250 calls/day for the whole app, not per user.** That is
  what `DAILY_SEARCH_LIMIT` (default 25 searches per account per day) exists to
  protect. With more users than that, get a higher limit from Adzuna or have
  each user bring their own key.
- **512 MB and a tenth of a CPU.** Fine for this, since the work is all waiting
  on other people's servers, but do not expect to run two workers.

### If you already have something else on Render, read this one

The 750 free instance-hours are granted **per workspace, not per service**, and
they are shared by every free web service you own. Run out and Render suspends
*all* of them until the first of the next month — including whatever you had
running before this.

A month is about 730 hours, so a single service kept awake around the clock
consumes essentially the entire allowance on its own. That is what makes the
uptime-pinger trick above dangerous here: pinging `/health` every 10 minutes
keeps this app from ever sleeping, and the app you already had deployed goes
down with it when the hours run out.

So with a second service in the workspace:

| | Hours used | Verdict |
|---|---|---|
| Both allowed to sleep | only while actually serving | fine, this is the default |
| One pinged awake 24/7 | ~730 of 750 | the other is living on 20 hours |
| Both pinged awake | ~1460 of 750 | both suspended partway through the month |

Sleeping is cheap because hours accrue only while a service is running. Waking
costs a minimum of 15 minutes of allowance per burst of use, since that is the
idle timeout, so even a few dozen searches a day lands nowhere near the cap.

Check where you actually stand on the
[billing page](https://dashboard.render.com/billing#included-usage) — it shows
hours consumed so far this month. Two other allowances are workspace-wide too:
500 build minutes a month, which a `pip install -r requirements.txt` build eats
a few of each deploy, and your outbound bandwidth.

---

## Run it locally

```bash
git clone <this repo> && cd job-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m jobagent.cli init
.venv/bin/python -m jobagent.cli serve      # http://127.0.0.1:8000
```

No `DATABASE_URL` means SQLite at `data/jobs.db`, which is the right choice on a
laptop. Then in the browser: create an account, upload your resume, type a role,
pick your cities, press search.

### Cost

Zero. Public job APIs need no keys, and an LLM is optional — it is used for two
things only, structuring your uploaded resume and suggesting related job titles.
Without one both fall back to built-in logic and everything still works.

1. **Ollama** — local, offline, free: `brew install ollama && ollama pull qwen2.5:7b-instruct`
2. **Gemini** or **Groq** free tier — one env var
3. **Nothing** — heuristic resume parsing and a curated title table

---

## CLI

Day-to-day use is the dashboard; searching belongs to a logged-in account, so it
has no CLI equivalent.

```bash
.venv/bin/python -m jobagent.cli doctor        # is everything wired up
.venv/bin/python -m jobagent.cli crawl         # warm the cache (what cron calls)
.venv/bin/python -m jobagent.cli backfill -n 100   # fill in missing descriptions
.venv/bin/python -m jobagent.cli stats         # cache size by source
.venv/bin/python -m jobagent.cli users         # who has an account
.venv/bin/python -m jobagent.cli sources       # every source, on or off
.venv/bin/python -m jobagent.cli schedule      # local cron, foreground
```

### Checks

```bash
.venv/bin/python scripts/check_multiuser.py       # isolation, per-user scoring, HTTP flow
.venv/bin/python scripts/check_postgres_ready.py  # schema + queries against the PG dialect
.venv/bin/python scripts/time_live_search.py      # how long a real search takes
.venv/bin/python scripts/probe_sources.py         # which sources are reachable
.venv/bin/python scripts/probe_new_sources.py     # does a candidate API honour the city filter
```

`check_postgres_ready.py` compiles the schema and every read-path query for
Postgres without needing a server. It does not replace running against a real
one, which happens first on deploy.

### Upgrading an older single-user install

```bash
.venv/bin/python scripts/migrate_to_multiuser.py
```

Keeps the cached openings — they cost hours of polite crawling — and drops the
columns that only made sense for one person (score, status, application state),
which now live per user or are computed on read.

---

## Adding more companies and Workday tenants

Board slugs change and companies migrate between platforms, so verify instead of
guessing:

```bash
curl -s "https://boards-api.greenhouse.io/v1/boards/<slug>/jobs" | head -c 200
curl -s "https://api.lever.co/v0/postings/<slug>?mode=json" | head -c 200
curl -s "https://api.smartrecruiters.com/v1/companies/<slug>/postings?limit=1" | head -c 200
curl -s "https://apply.workable.com/api/v1/widget/accounts/<slug>?details=true" | head -c 200
```

A slug can return `200 OK` with nothing in it, which is the failure worth
watching for — it looks like a working source that quietly contributes zero.
SmartRecruiters slugs are also case-sensitive and often not the company name:
`Bosch` returns nothing, `BoschGroup` returns several thousand. Check the count
in the response, not the status code.

For Workday, open the careers page —
`https://TENANT.wdN.myworkdayjobs.com/en-US/SITE` — and copy `tenant`, `host`
and `site` into `sources.workday.tenants` in `config/config.yaml`. A 422 means
the site name is wrong; 401 means that tenant needs a login.

---

## What is and is not automated

| | |
|---|---|
| Automated | Finding openings across every source, ranking them against your resume, filtering out what is off-target, tracking what you applied to. |
| **Not** automated | Filling any form. Submitting any application. Rewriting your resume. Logging into any job board. Contacting any recruiter. |

### On LinkedIn and Naukri

Automated access is against both platforms' terms of service. These adapters are
read-only, never log in, never submit, and use conservative paging — they read
the same public search results a browser would. `linkedin` ships enabled because
it is by far the richest source of India roles, but running a *public* service
off it is a bigger exposure than personal use, and you are the one accepting
that. Set `sources.linkedin.enabled: false` in `config/config.yaml` to turn it
off; the other thirteen sources are licensed APIs or the employer's own ATS.

---

## Layout

```
config/           config.yaml, profile.yaml
jobagent/
  sources/        ats.py, workday.py, aggregators.py, feeds.py, discovery.py
  matching/       skills.py (taxonomy + resume → weights), filters.py, scorer.py, roles.py
  resume/         ingest.py (upload → structured, in memory), llm.py
  web/            app.py, auth.py, tasks.py, templates/
  models.py       shared jobs cache, users, searches, per-user decisions
  fetch.py        live path and scheduled crawl
  results.py      read-time ranking for one user
  search.py       saved search → per-source queries
.github/workflows/crawl.yml
render.yaml
```

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| First page load takes a minute | Free instance was asleep; this is normal and costs nothing. Keeping it warm with a pinger fixes it but spends the whole workspace hour allowance — see the warning above if you host anything else on Render. |
| `linkedin` returns nothing after deploying | Cloud IPs get rate-limited far more than home ones. The other sources still work; disable it if it stays dead. |
| Everyone got logged out after a deploy | `SECRET_KEY` changed or was never set. Set it as a real environment variable. |
| A source returns nothing | Run `scripts/probe_sources.py`. 404 usually means a dead board slug; a timeout usually means the network blocks that site. |
| Search finds nothing | Lower the minimum score, widen the cities, or tick "include filtered out" on the results page to see what was rejected and why. |
| Search says it hit the time limit | Normal under load — whatever arrived is shown, the rest lands on the next crawl. |
| `CERTIFICATE_VERIFY_FAILED` | Corporate network re-signs HTTPS. Handled by `truststore` (`http.use_system_certs: true`); point `http.ca_bundle` at a PEM if you need more. |
| Resume parsed badly | Re-upload a resume that names the skills explicitly; the parser copies, it does not infer. A scanned PDF has no text layer and cannot be read at all. |
| Out of live searches | `DAILY_SEARCH_LIMIT` per account per day, protecting a shared API quota. Raise it if your keys allow. |
