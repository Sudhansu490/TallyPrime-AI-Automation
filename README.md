# Tally Automation

AI-assisted processing of purchase invoices and bank statements into
TallyPrime vouchers, with mandatory human review and approval.

**Status: Phase 1 (foundation).** No document processing, AI extraction or
Tally communication exists yet. Nothing in this codebase can write to TallyPrime.

## Principles

- Accounting accuracy over speed. Missing values stay `null`; nothing is invented.
- Extracted, calculated and user-edited values are kept distinct.
- AI output is schema-validated and never decides ledgers or posts vouchers.
- Voucher *generation* and voucher *import* are separate operations. Import
  requires an approval record **and** `TALLY_WRITE_ENABLED=true`.

## Network topology

```
Computer B (browser) ──Tailscale──▶ Computer A: one FastAPI process
                                      ├─ /            HTML pages (Jinja2 shells)
                                      ├─ /static/...  CSS + JS (local, no CDN)
                                      └─ /api/v1/...  JSON API
                                                        │
                                                        └──Tailscale──▶ TallyPrime host :9000
```

- One process serves pages, static files and the API from the same origin,
  so there is no CORS configuration at all.
- The server binds explicit sockets: `127.0.0.1` and, when enabled, Computer A's
  Tailscale IP. `0.0.0.0`, LAN and public addresses are refused at startup.
- Only the backend talks to TallyPrime. The browser never does.
- No port is ever opened to the public internet.

Observed environment (Phase 1 inspection, 2026-11-04): Computer A is
`desktop-6fjvg7s` (Tailscale `100.95.249.91`). The tailnet has no other devices yet,
and TallyPrime is not installed on Computer A.

## Layout

```
src/tally_automation/
  __main__.py        `python -m tally_automation` → server.run()
  server.py          bind-address policy + Uvicorn launcher
  config.py          settings from environment / .env
  logging_config.py  JSON logs with redaction of sensitive fields
  security.py        password hashing (stdlib scrypt)
  api/               app factory, dependencies, middleware, JSON routes
  frontend/
    routes.py        HTML page routes and navigation
    templates/       Jinja2 layout and page shells
    static/css/      shared stylesheet
    static/js/       api.js (API client), ui.js (DOM/format helpers), pages/*.js
  db/                SQLAlchemy base, session/engine, models
migrations/          Alembic environment and revisions
tests/               pytest suite (synthetic data only)
data/                runtime data: database, uploads, logs (gitignored)
```

Modules for documents, OCR, extraction, validation, duplicates, Tally and
authentication are added in their own phases.

### Frontend rules

- Vanilla ES modules, no framework, no build step, no CDN.
- Pages are thin shells. Data comes from `/api/v1` and is inserted with
  `textContent` only. `innerHTML`, `eval`, inline scripts, inline styles and
  `localStorage`/`sessionStorage` are banned. `tests/test_frontend.py` enforces this.
- Business logic and validation live on the backend. The frontend only displays data.

## Setup (Windows 11, Python 3.12.10)

The project uses the existing `tally_env` virtual environment.

```powershell
.\tally_env\Scripts\Activate.ps1
pip install -e ".[dev]" -c constraints.txt
copy .env.example .env      # then edit .env
alembic upgrade head
```

`constraints.txt` is required; see Known issues.

## Running

Production-style (explicit loopback/Tailscale sockets, from `BIND_HOSTS`):

```powershell
python -m tally_automation
```

Development with auto-reload (loopback only):

```powershell
uvicorn tally_automation.api.main:build_app --factory --host 127.0.0.1 --port 8000 --reload
```

Open http://127.0.0.1:8000/ for the dashboard.
Health check: `GET /api/v1/health`.
Interactive API docs are at `/docs` outside production (`APP_ENV=production` disables them).

### Access from Computer B

Do this only once login exists (Phase 9), or on a tailnet you fully control.

1. Join Computer B to the same tailnet.
2. In `.env`, set `BIND_HOSTS=127.0.0.1,<output of tailscale ip -4>`. Optionally
   set `EXTRA_ALLOWED_HOSTS=<Computer A MagicDNS name>`.
3. Allow inbound TCP `APP_PORT` on Computer A in Windows Firewall, from
   `100.64.0.0/10` only. Do not accept Windows' "allow on all networks" prompt.
4. Browse to `http://<Computer A Tailscale IP>:8000/` from Computer B.

Requests with any other `Host` header are rejected with HTTP 400 (DNS-rebinding protection).

## Tests

```powershell
pytest
```

Tests use a temporary SQLite database per test, ignore `.env`, never start a
real server, and never need TallyPrime. Test data is synthetic and labelled as such.

## Database migrations

```powershell
alembic revision --autogenerate -m "describe change"   # review the generated file
alembic upgrade head
```

`tests/test_database.py::test_models_match_migrations` fails if a model changes
without a matching migration.

## Configuration

See `.env.example`. Secrets (`SECRET_KEY`, `LLM_API_KEY`) are read as
`SecretStr` and never appear in reprs or logs. Never commit `.env`.

## Architectural decisions

| Decision | Reason |
|---|---|
| Single FastAPI process for pages, static files and API | Same origin: no CORS, a simple CSP, one thing to deploy |
| Custom launcher binding one socket per address | Uvicorn `--host` takes one address, and `0.0.0.0` would expose Wi-Fi/LAN |
| Jinja2 renders layout only; JS fetches data | Keeps one data path (the JSON API) that the backend validates |
| stdlib scrypt for passwords | Memory-hard and OWASP-accepted, with no extra dependency |
| SQLite in WAL mode, foreign keys on, Alembic batch mode | Concurrent reads during processing; real constraints; SQLite-safe migrations |
| Swagger `/docs` gets a looser CSP (CDN + inline) | Development only, that path only; disabled in production |

## Known issues

| Issue | Effect | Resolution |
|---|---|---|
| `paddlepaddle 2.6.2` needs `protobuf<=3.20.2` | A newer protobuf breaks Paddle import | `constraints.txt` pins 3.20.2 |
| `opencv-python`, `opencv-contrib-python` and `opencv-python-headless` are all installed | They overwrite the same `cv2` module | Reduce to one in Phase 3 |
| Streamlit 1.66 and pyarrow 26 are still installed in `tally_env` (from the earlier UI) | Unused; pyarrow 26 needs NumPy ≥2 | No longer a project dependency. Optional: `pip uninstall streamlit pyarrow` |
| The tailnet contains only Computer A | Computer B and the Tally host cannot connect yet | Join both devices to tailnet `shivam-proteco-1997.github` |
| SQLite `DateTime(timezone=True)` | SQLite stores no offset | Values are written in UTC; treat them as UTC |

## TallyPrime connectivity prerequisites (Phase 6)

1. Computer A, Computer B and the Tally host are on the same tailnet. Tailscale
   ACLs, if used, allow A → Tally host:9000 and B → A:`APP_PORT`.
2. TallyPrime 6.2: F1 Help → Settings → Connectivity → Client/Server configuration.
   Set TallyPrime to act as *Server* (or *Both*) and note the port (default 9000).
3. Windows Firewall on the Tally host allows that port **only** from `100.64.0.0/10`.
4. A company is loaded in TallyPrime for read-only testing.

## Phases

1. Foundation ✅ · 2. Upload & processing foundation · 3. PDF text & OCR ·
4. AI extraction · 5. Duplicates & accounting validation · 6. Tally read-only ·
7. Voucher generation · 8. Approval & controlled import · 9. Dashboard,
security & networking · 10. End-to-end testing & deployment
