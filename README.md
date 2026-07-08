# JobSearch

A self-hosted job search and tracking app. It automates discovery, verification,
fit scoring, and status tracking for job opportunities across multiple ATS
platforms and job boards, replacing manual spreadsheet/note tracking.

**Version:** see [`VERSION`](./VERSION)

## What it does

- **Crawls** search engines and per-company career pages (Ashby, Lever, Workable,
  Greenhouse, LinkedIn's guest API, RSS feeds, and generic HTML boards) for
  relevant listings.
- **Verifies** each listing is still live before ever surfacing it — expired
  postings are never shown.
- **Deduplicates** by company + role title + URL, so re-runs don't create
  duplicate tracker entries.
- **Scores** each listing's fit against your profile (domain match, tech stack,
  seniority, remote/geo, relocation/visa) and auto-saves anything above a
  configurable threshold; everything else is skipped with a reason.
- **Tracks** status end-to-end (shown → applied → interviewing → offer/rejected),
  with notes, quality flags, and a per-run audit trail (`SearchSession`).

## Stack

FastAPI + SQLAlchemy + Jinja2 (server-rendered UI, no separate frontend build) +
SQLite, packaged as a single Docker image.

## Multi-user

Accounts are admin-created only, from a profile YAML — there's no public
sign-up form. New users get a "set your password on first login" flow. All
tracker/listing/profile/config data is scoped per-user; the search-engine and
company-career-page registries are shared/global.

**Bootstrapping the first admin (empty database):** `/admin/users/new` is
normally gated behind an existing admin login, but there's a bootstrap
exception — while the `users` table is completely empty (a fresh install,
before any account exists), that page is reachable with no login at all.
Uploading a valid profile YAML there creates the very first account and
auto-promotes it to admin. From that point on, `/admin/users/new` requires a
logged-in admin like any other admin route.

**Profile YAML format**, uploaded at `/admin/users/new`:

```yaml
name: "Jane Doe"
email: "jane@example.com"
skills:
  - "Golang"
  - "AWS"
  - "Kubernetes"
domain_expertise:
  - "fintech"
  - "payments"
experience_years: 12
seniority: "senior"
# resume_file: "jane-resume.pdf"   # optional, informational only — not parsed
```

`name`, `email`, `skills`, `domain_expertise`, `experience_years`, and
`seniority` are all required; the account is rejected with a 422 listing
whatever's missing. `resume_file` is optional and purely informational (no
file is actually uploaded or parsed - left for V2).

## Running it

```bash
# Generate a real session-signing key first — required, the app refuses to
# start without one:
openssl rand -hex 32

docker compose -f docker-compose.example.yml up -d
```

`docker-compose.example.yml` pulls the published image from Docker Hub by
default. To build from source instead, edit that file: comment out `image:`
and uncomment `build: .`, then run with `--build` appended.

Required environment variables (see `docker-compose.example.yml`):

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SESSION_SECRET_KEY` | **yes** | — | Signs the session cookie. App fails to start without it. |
| `DATABASE_URL` | no | `sqlite:////app/data/jobsearch.db` | |
| `SESSION_COOKIE_HTTPS_ONLY` | no | `false` | Set `true` only if served exclusively over HTTPS. |
| `ENABLE_API_DOCS` | no | `false` | Enables `/docs`/`/redoc`/`/openapi.json`. Leave off in production. |

On first run, visit `/admin/users/new` to create your first (admin) account —
see "Bootstrapping the first admin" above.

## Development

```bash
pip install -r requirements.txt
pytest tests/
```

Tests are self-contained per file (own in-memory SQLite DB + `dependency_overrides`,
no shared fixtures) — see `tests/`.

