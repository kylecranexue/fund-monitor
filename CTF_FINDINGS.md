# Navi100 CTF Findings

## Confirmed Evidence

All checks below were revalidated against `https://www.navi100.top` on 2026-06-08.

| Item | Evidence |
|---|---|
| Frontend replica is exact | `/` is 57,568 bytes and SHA-256 matches `work/index.html`: `53c81c2ba4fc9cd7cbcbffbd92a8983b0c330b1c307456490d0327b60c8743f6` |
| Public funds data is exact | `/api/funds?primary_category=all` is 61,609 bytes and SHA-256 matches `work/funds.json`: `cb1355b72899aa419801456c6a229f4ce0e195425791efeb80c9294f86c19325` |
| Health endpoint is dynamic | `/api/health` returns a request-time `fetch_time`, not a fully static JSON file |
| Activation store disclosure | `/api/health` reports `activation_code_count: 50`, `activation_store: "env"`, `token_ttl_days: 7` |
| Auth contract | Missing token returns `401 {"error":"activation_required","success":false}` |
| Token contract | Invalid bearer token returns `401 {"error":"invalid_token","success":false}` |
| Redeem contract | Missing code returns `400 missing_code`; invalid code returns `401 invalid_code` |
| API surface from frontend | The captured frontend only calls `/api/redeem`, `/api/me`, `/api/calculate`, `/api/funds` |
| Static discovery | Common public files such as `/robots.txt`, `/sitemap.xml`, `/manifest.json`, `/sw.js`, `/service-worker.js`, `/index.html.map`, `/script.js`, `/script.js.map`, `/assets/`, `/static/`, and `/.well-known/security.txt` returned 404 |

## Local Replica Status

The local backend in `server.py` now mirrors the observable API contract closely enough for end-to-end UI demonstration:

- `POST /api/redeem`
- `GET /api/me`
- `POST /api/calculate`
- `GET /api/funds`
- `GET /api/health`

The replica intentionally uses local demo activation codes and does not contain production environment variables or private deployment secrets.

## Current Blocker

The remaining gap for the highest-score path is access to non-public serverless source or production environment variables. Current evidence points to activation codes being stored in environment variables, but the tested public inputs do not expose an environment-read primitive, traceback, source map, build artifact, service worker cache, or unauthenticated deployment file listing.

## Low-Risk Next Steps

1. Check any competition-provided materials for Vercel project/team name, Git repository URL, deployment alias, invitation link, or hidden scoring notes.
2. If an authorized Vercel account is provided, run `vercel env ls`, `vercel project ls`, and inspect deployments through the official account rather than probing public deployment APIs.
3. Use the browser console only to inspect same-origin storage after visiting the challenge page:
   - `localStorage`
   - `sessionStorage`
   - `document.cookie`
   - `caches.keys()`
4. If a valid token is obtained through the authorized flow, retest `/api/calculate` inputs for server-side calculation edge cases. Avoid high-volume fuzzing; use small, structured cases and record request/response pairs.
5. Submit the local replica plus this evidence as a partial-source reconstruction if the scoring rubric accepts functional equivalence.

## Submission Position

Recommended wording:

> The attached package is an exact frontend and public-data replica with a compatible local backend. It demonstrates the full user flow and documents the externally observable API contract. The production backend source and environment variables were not exposed through the authorized public attack surface during testing.
