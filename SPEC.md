# JobSearch App — Functional Specification
> Local only — not tracked by git. Last updated: 2026-06-12.
> Gates implementation of all P1–P5 items. No feature should be coded before its section here is finalised.

---

## 1. Overview

JobSearch is a Dockerised job search and tracking application that replaces the manual `/recruiter` CLI skill. It automates discovery, verification, scoring, and status tracking for engineering management job opportunities, persisting all state to a lightweight database.

**Key principles:**
- Never surface a role that is expired — verify before showing
- Never save a duplicate — deduplicate by company + role title + URL
- Auto-save only when fit ≥ `fit_autosave_threshold` (default 70%); prompt the user otherwise
- All data lives in the DB; no flat files in production

---

## 2. Docker topology

| Service | Image / runtime | Role |
|---|---|---|
| `app` | Python 3.12 (FastAPI) | Main application: search pipeline, scoring, tracker API, UI |
| `db` | SQLite file mounted as a volume | Persistent storage; single-file, zero-config |

**Volumes:**
- `./data/jobsearch.db` → `/app/data/jobsearch.db` (SQLite file)
- `./resume/` → `/app/resume/` (uploaded resume files, read-only at runtime) — **V2 only**; not mounted in V1

**Ports:**
- `app`: `8080:8080` (web UI + REST API)

**No external network dependencies at runtime** — all crawling is outbound HTTP from the `app` container.

---

## 3. Data models

### 3.1 UserProfile

Pre-populated at startup from the account owner's hardcoded resume data (V1). One active profile at a time (id = 1). Manual edits to `skills`, `domains`, and `seniority` are preserved across any future re-seed.

| Field | Type | Notes |
|---|---|---|
| `id` | integer PK | always 1 (single profile) |
| `full_name` | text | |
| `email` | text | |
| `skills` | JSON array of strings | e.g. `["Go", "Kubernetes", "AWS"]` |
| `experience_years` | integer | total EM / leadership years |
| `seniority` | text | `senior`, `staff`, `director`, `vp` |
| `domains` | JSON array of strings | e.g. `["fintech", "payments", "platform"]` |
| `resume_path` | text | V2 — path to source `.docx`/`.pdf` inside container; unused in V1 |
| `updated_at` | datetime | |

### 3.2 SearchEngine

Registry of job boards and aggregators the search pipeline queries.

| Field | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `name` | text | e.g. `"Remotive"` |
| `search_url_template` | text | URL with `{query}` and `{filters}` placeholders |
| `fetch_strategy` | text | `rss`, `html`, `api`, `sitemap` |
| `quirks` | JSON object | per-platform crawl *behavior* rules (login walls, stale signals, redirect patterns) — see §3.9 |
| `search_params` | text/JSON | per-engine auxiliary config/secrets, editable from the Engines page (PRIORITIES.md item 55). Distinct from `quirks`: this holds engine-specific *data* rather than crawl-behavior rules. For the LinkedIn engine this field holds the `li_at` session cookie (see §3.9); other engines leave it blank. **Never** sourced from process env — auxiliary config like session cookies is per-user, and the app is multi-user (item 50). |
| `active` | boolean | soft-disable without deleting |
| `last_crawled_at` | datetime | |

Pre-populated entries: Remotive, Ashby, Himalayas, Workable, Lever, Greenhouse, golangprojects, builtin, LinkedIn (item 56).

### 3.3 CompanyCareerPage

Career pages discovered via search engine results — stored as direct crawl sources.

| Field | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `company` | text | normalised company name |
| `careers_url` | text | direct ATS or careers page URL |
| `ats_type` | text | `ashby`, `workable`, `lever`, `greenhouse`, `custom`, `unknown` |
| `last_verified_at` | datetime | |
| `active` | boolean | |

### 3.4 JobListing

A discovered role, before or after tracker entry.

| Field | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `company` | text | |
| `role_title` | text | |
| `apply_url` | text | canonical ATS URL |
| `source` | text | which `SearchEngine.name` found it |
| `remote_type` | text | `remote`, `hybrid`, `onsite`, `unknown` |
| `geo_restriction` | text | `worldwide`, `emea`, `latam`, `usa`, `brazil`, `unknown` |
| `relocation_offered` | boolean | |
| `visa_sponsorship` | boolean | |
| `quality_flags` | JSON array | e.g. `["internal-only", "maternity-cover", "remote-mismatch"]` |
| `fit_score` | integer | 0–100; null until scored |
| `verified_active` | boolean | null = unverified; true = active; false = expired |
| `verified_at` | datetime | |
| `discovered_at` | datetime | |

### 3.5 TrackerRecord

The user-facing job tracker. One record per company+role the user has decided to track.

| Field | Type | Notes |
|---|---|---|
| `id` | integer PK | sequential, user-visible |
| `listing_id` | FK → JobListing | nullable (manual entries have no listing) |
| `company` | text | denormalised for display |
| `role_title` | text | denormalised for display |
| `apply_url` | text | |
| `status` | text | see §4 state machine |
| `fit_pct` | integer | 0–100 |
| `date_shown` | date | when first surfaced to user |
| `date_applied` | date | nullable |
| `notes` | text | free-form user notes |
| `created_at` | datetime | |
| `updated_at` | datetime | |

### 3.6 SearchSession

One record per search run, for audit and V2 session output persistence.

| Field | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `started_at` | datetime | |
| `finished_at` | datetime | |
| `query_params` | JSON object | role type, geo, filters used |
| `listings_found` | integer | total before dedup/verify |
| `listings_saved` | integer | added to tracker |
| `listings_skipped` | integer | expired, duplicate, or below threshold |
| `skip_reasons` | JSON object | counts by reason |

### 3.7 AppConfig

Global user-modifiable settings. One row, always `id = 1`.

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | integer PK | 1 | singleton |
| `fit_autosave_threshold` | integer | 70 | 0–100; roles scoring ≥ this are saved automatically |
| `updated_at` | datetime | | |

Exposed as:
- `GET /api/config` — read current settings
- `PATCH /api/config` — update one or more fields; validates `fit_autosave_threshold` is 0–100

### 3.8 ScoringRubric

User-modifiable scoring weights. One row per dimension, persisted in DB. The pipeline reads this table at the start of each search run.

| Field | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `dimension` | text | `domain_match`, `tech_stack`, `seniority`, `remote_geo`, `relocation_visa_bonus` |
| `weight` | integer | contribution to score; see constraint below |
| `is_bonus` | boolean | if true, weight is additive beyond the 100-point base (not included in the sum-to-100 check) |

**Constraint:** `SUM(weight) WHERE is_bonus = false` must equal exactly 100 before any save is accepted. Bonus dimensions (e.g. `relocation_visa_bonus`) are excluded from this check and may push the final score above 100. The API rejects saves that violate this with HTTP 422 and a message listing the current sum.

Default rows:

| Dimension | Weight | is_bonus |
|---|---|---|
| `domain_match` | 35 | false |
| `tech_stack` | 30 | false |
| `seniority` | 25 | false |
| `remote_geo` | 10 | false |
| `relocation_visa_bonus` | 10 | true |

CRUD API:
- `GET /api/scoring-rubric` — list all dimensions with current weights
- `PUT /api/scoring-rubric` — replace all non-bonus weights in one transaction; validates sum = 100 before committing
- `PATCH /api/scoring-rubric/{dimension}` — update a single dimension's weight; validates that the full rubric sum remains 100 after the change (excluding bonuses)

### 3.9 PlatformQuirk (V2 — part of SearchEngine.quirks for now) (V2 — part of SearchEngine.quirks for now)

In V1, platform quirks are stored as a `quirks` JSON column in `SearchEngine`. In V2 (item 22) this graduates to a first-class table. Current known quirks are encoded at ingestion time:

| Platform | Quirk |
|---|---|
| LinkedIn | **No longer a permanent skip** (PRIORITIES.md item 56). Listing search and detail/verification both go through LinkedIn's guest API (`jobs-guest/jobs/api/seeMoreJobPostings/search` and `jobs-guest/jobs/api/jobPosting/<id>`), which requires a per-user `li_at` session cookie stored in this engine's `search_params` field (§3.2), not a global env var. **To obtain/refresh the cookie:** log into linkedin.com in a browser, open DevTools → Application (Chrome) / Storage (Firefox) → Cookies → `https://www.linkedin.com`, copy the value of the `li_at` cookie, and paste it into the LinkedIn engine's search-parameters field on the Engines page. LinkedIn sessions typically last ~1 year but can be invalidated early by logging out or changing your password; refresh the cookie when LinkedIn fetches start failing or returning login-wall responses. |
| Himalayas | ~50% of listings are stale; always verify via ATS URL |
| Lever | Returns 403 on direct job URLs from some IPs; fall back to company page |
| Workable | Listings redirect through `apply.workable.com`; follow redirect to get ATS URL |
| Greenhouse | Boards at `boards.greenhouse.io/{company}` are crawlable without auth |
| Ashby | Boards at `jobs.ashbyhq.com/{company}` are crawlable; status field in JSON |

---

## 4. Status state machine

```
         ┌──────────────────────────────────┐
         │           shown                  │
         └──────────────────────────────────┘
                      │
         ┌────────────┼───────────┬──────────────┐
         ▼            ▼           ▼              ▼
      applied       skipped    expired        (manual
         │         (terminal) (terminal)      expired)
         │
    ┌────┼──────────────────┬──────────────┐
    ▼    ▼                  ▼              ▼
interviewing            rejected        expired
    │                  (terminal)      (terminal)
┌───┴────┐
▼        ▼
offer  rejected
(term.)(terminal)
```

**Terminal states:** `skipped`, `rejected`, `expired`.

`expired` can be reached in two ways:
- **Automatic** — set by the pipeline when `verified_active = false`; or by tracker hygiene automation (V2) after 7 days in `shown` with no action
- **Manual** — user explicitly marks a role as expired from any non-terminal state (e.g. they discovered the role was filled before applying)

**Rules:**
- Forward transitions only — no going back (e.g. `applied` cannot revert to `shown`)
- `skipped` and `rejected` are terminal; re-discovery of the same role is suppressed
- `expired` roles are preserved in the DB for reopen detection — if the same URL reappears active, surface it as a new `shown` entry with a note "Previously expired"
- The tracker's `PATCH /api/tracker/{id}/status` endpoint accepts `expired` as a valid target from any non-terminal state

---

## 5. Component boundaries

```
┌──────────────────────────────────────────────────────┐
│                     app container                    │
│                                                      │
│  ┌─────────────┐    ┌──────────────────────────┐    │
│  │  Ingestion  │    │      Search Pipeline     │    │
│  │  (resume)   │    │  ┌──────────────────┐    │    │
│  └──────┬──────┘    │  │ Engine Directory │    │    │
│         │           │  └────────┬─────────┘    │    │
│         ▼           │           │              │    │
│  ┌─────────────┐    │  ┌────────▼─────────┐    │    │
│  │ UserProfile │    │  │  Crawler/Fetcher │    │    │
│  │    (DB)     │    │  └────────┬─────────┘    │    │
│  └──────┬──────┘    │           │              │    │
│         │           │  ┌────────▼─────────┐    │    │
│         │           │  │   Verification   │    │    │
│         │           │  └────────┬─────────┘    │    │
│         │           │           │              │    │
│         │           │  ┌────────▼─────────┐    │    │
│         └───────────┼─►│  Fit Scorer      │    │    │
│                     │  └────────┬─────────┘    │    │
│                     │           │              │    │
│                     │  ┌────────▼─────────┐    │    │
│                     │  │  Dedup Filter    │    │    │
│                     └──┴────────┬─────────┘    │    │
│                                 │              │    │
│                    ┌────────────▼───────────┐  │    │
│                    │    Tracker (DB + API)   │  │    │
│                    └────────────┬───────────┘  │    │
│                                 │              │    │
│                    ┌────────────▼───────────┐  │    │
│                    │       Web UI           │  │    │
│                    └────────────────────────┘  │    │
└──────────────────────────────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │   SQLite (DB)   │
                    └─────────────────┘
```

### 5.1 Ingestion

**V1 — startup seed (item 2):**
- `UserProfile` is seeded from the account owner's hardcoded resume data on first startup (if the table is empty)
- Seed covers: `full_name`, `email`, `skills`, `experience_years`, `seniority`, `domains`
- No file parsing; no upload endpoint in V1
- If the profile row already exists (id = 1), the seed is skipped — manual edits are never overwritten

**V2 — file-based ingestion (item 23):**
- Accepts `.docx` or `.pdf` resume file via UI upload or CLI argument
- Parses into `UserProfile` fields using python-docx / pdfminer
- Merge strategy: system fields (name, email, raw skills) are overwritten; manually edited fields (`skills`, `domains`, `seniority`) are merged (union, not replace)
- Exposed as: `POST /api/profile/ingest`

### 5.2 Search Pipeline

Orchestrates a full search run. Steps in order:

1. **Query construction** — build queries per engine from user-specified role type, geo, and keyword filters
2. **Crawl** — fetch listings from each active `SearchEngine` using its `fetch_strategy`; respect `quirks`
3. **Company page extraction** — if a result links to a company careers page not yet in `CompanyCareerPage`, persist it
4. **Verification** — for each listing, fetch the ATS URL directly; mark `verified_active`; skip expired
5. **Quality flags** — apply listing quality checks (internal-only, maternity cover, remote mismatch)
6. **Geo/remote filter** — apply remote_type and geo_restriction filters
7. **Dedup** — skip any listing matching an existing `TrackerRecord` with terminal-or-active status
8. **Fit scoring** — score each surviving listing against `UserProfile`
9. **Save decision** — auto-save if `fit_score ≥ fit_autosave_threshold` (see §3 AppConfig); prompt user for scores below the threshold
10. **Session record** — write a `SearchSession` record on completion

Exposed as: `POST /api/search/run` (async, streams progress events via SSE)

### 5.3 Fit Scorer

Scores a `JobListing` against `UserProfile`. Base score is 0–100; bonus dimensions may push it above 100.

Weights are read from the `ScoringRubric` table (§3.8) at the start of each search run — never hardcoded. Default rubric:

| Dimension | Default weight | is_bonus | Signal |
|---|---|---|---|
| Domain match | 35 | false | overlap between `UserProfile.domains` and role description keywords |
| Tech stack | 30 | false | overlap between `UserProfile.skills` and required/preferred skills in listing |
| Seniority alignment | 25 | false | `UserProfile.seniority` vs. role title/level signals |
| Remote/geo fit | 10 | false | matches user's preferred remote type and geo |
| Relocation/visa bonus | 10 | true | +10 if `relocation_offered` or `visa_sponsorship` when user is outside role's primary geo |

**Constraint:** non-bonus weights must always sum to 100 — enforced at save time by the CRUD API (§3.8). Bonus dimensions are additive and may cause the final score to exceed 100.

Exposed as:
- `POST /api/score` (internal; called by pipeline)
- `GET /api/scoring-rubric`, `PUT /api/scoring-rubric`, `PATCH /api/scoring-rubric/{dimension}` (user-facing CRUD — see §3.8)

### 5.4 Tracker

CRUD API over `TrackerRecord`. Enforces the state machine (§4) — rejects invalid transitions with HTTP 422.

| Endpoint | Method | Description |
|---|---|---|
| `/api/tracker` | GET | list all records, filterable by status |
| `/api/tracker/{id}` | GET | single record |
| `/api/tracker/{id}/status` | PATCH | advance status; validates transition |
| `/api/tracker/{id}/notes` | PATCH | update notes |
| `/api/tracker` | POST | manual entry (no listing_id required) |

### 5.5 Web UI

Minimum viable interface at P4 (item 15). Single-page app or server-rendered HTML.

**Views:**
- **Dashboard** — table of `TrackerRecord` grouped by status; sortable by fit_pct, date_shown
- **Search** — trigger a new search run with filter controls; live progress feed
- **Profile** — view/edit `UserProfile`; resume upload
- **Engines** — view/add/remove `SearchEngine` entries; toggle active

---

## 6. Search filters and parameters

All filters are optional and additive. Defaults:

| Filter | Default |
|---|---|
| Remote type | `remote` or `hybrid` only |
| Geo | `worldwide` (no restriction) |
| Role type | `engineering manager`, `director of engineering`, `head of engineering` |
| Seniority | derived from `UserProfile.seniority` |

User-configurable per search run:
- `geo`: `latam`, `emea`, `north_america`, `worldwide`
- `remote_type`: `remote`, `hybrid`, `onsite`, `any`
- `relocation_required`: boolean — include roles that require relocation only if `relocation_offered = true`
- `visa_required`: boolean — include roles that require work authorisation only if `visa_sponsorship = true`
- `keywords`: free-text list appended to queries (e.g. `golang`, `payments`, `fintech`)
- `salary_min`: integer (USD/year) — filter listings where salary data is available

---

## 7. Verification protocol

Before any listing is surfaced, it must be verified active. Priority order:

1. Direct ATS URL fetch (Ashby → Workable → Lever → Greenhouse → company page)
2. If ATS URL returns 200 with title present → `verified_active = true`
3. If 404 / 410 / title absent → `verified_active = false` (expired); mark and skip
4. If fetch errors or timeouts → `verified_active = null`; surface with "unverified" flag

Apply `SearchEngine.quirks` before fetching (e.g. follow Workable redirects). LinkedIn URLs are verified via the guest-API detail call (§3.9, item 56) rather than skipped, using the `li_at` cookie from the engine's `search_params` field.

---

## 8. Duplicate suppression rules

Before adding any `JobListing` to the tracker, run two independent checks — either match is sufficient to trigger dedup:

**Check A — by URL (exact match):**
1. Query `TrackerRecord` by `apply_url` — exact string match
2. If match found with any non-expired status → apply the same rules as Check B below
3. URL match takes priority: a company may post the same role title on different dates with different URLs; those are distinct listings and must not be suppressed by title alone

**Check B — by company + title (fuzzy match):**
1. Query `TrackerRecord` by `(company, role_title)` — case-insensitive, after stripping common suffixes (e.g. " — EMEA", " (Remote)", date tokens)
2. Only trigger if the URL did not already match (avoid double-counting)

**Decision table (applies to whichever check matched):**

| Matched status | Action |
|---|---|
| `applied`, `interviewing`, `offer`, `skipped`, `rejected` | Skip silently; log reason to `SearchSession.skip_reasons` |
| `shown` | Skip — already in front of the user |
| `expired` | Surface as new `shown` with note "Previously expired — re-appeared active" |

**No match in either check** → proceed to save.
