# Tally Automation

AI-assisted processing of purchase invoices and bank statements into
TallyPrime vouchers, with mandatory human review and approval.

**Status: Phase 2 (document upload and processing foundation).** Documents can be
uploaded, validated, de-duplicated (exact file) and queued. Text extraction, OCR, AI
extraction and Tally communication do not exist yet, so queued documents stay queued.
Nothing in this codebase can write to TallyPrime.

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

Observed environment (re-inspected 2026-11-05): Computer A is `SUDHANSU`
(Tailscale `100.111.30.101`, tailnet account `shivamacc1593@`; Windows 11 Pro,
i5-12500, 32 GB RAM, no usable GPU, so OCR runs on CPU). The tailnet has no other
devices yet, and TallyPrime is not installed on Computer A. An earlier inspection on
2026-11-04 recorded a different machine (`desktop-6fjvg7s`, `100.95.249.91`); that
entry is superseded.

## Layout

```
src/tally_automation/
  __main__.py        `python -m tally_automation` → server.run()
  server.py          bind-address policy + Uvicorn launcher
  config.py          settings from environment / .env
  logging_config.py  JSON logs with redaction of sensitive fields
  security.py        password hashing (stdlib scrypt)
  api/               app factory, dependencies, middleware, JSON routes
  documents/
    validation.py    extension, magic-byte and structural (PyMuPDF / Pillow) checks
    storage.py       content-addressed, write-once storage of originals
    service.py       ingestion: validate → hash → duplicate check → store → queue
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

Modules for text extraction, OCR, AI extraction, validation, duplicates, Tally and
authentication are added in their own phases.

### Frontend rules

- Vanilla ES modules, no framework, no build step, no CDN.
- Pages are thin shells. Data comes from `/api/v1` and is inserted with
  `textContent` only. `innerHTML`, `eval`, inline scripts, inline styles and
  `localStorage`/`sessionStorage` are banned. `tests/test_frontend.py` enforces this.
- Business logic and validation live on the backend. The frontend only displays data.

## Document intake (Phase 2)

Pipeline steps 1–4 run synchronously during upload (`POST /api/v1/documents`, one file per request):

1. **Name and type.** The filename is reduced to a display-only base name. The extension
   must be allowed for the document type (invoices: `.pdf .jpg .jpeg .png`; bank statements: `.pdf`).
2. **Size.** `Content-Length` is checked before the body is read, and the size limit is enforced
   again while streaming. Chunked uploads without a length are refused (411).
3. **SHA-256** is computed while streaming to a private temp file.
4. **Content.** Magic bytes must match the extension. PDFs must open in PyMuPDF, must not be
   encrypted, and must have between 1 and `MAX_PDF_PAGES` pages. Images must decode fully and stay
   under `MAX_IMAGE_MEGAPIXELS` (decompression-bomb guard).
5. **Exact duplicate (Level 1).** If the SHA-256 is already stored, the upload is recorded as a
   `duplicate` document that points at the original, with the evidence, and is **not** queued.
   Otherwise the file is stored and a `queued` processing job is created.

Rejected uploads are not stored. Responses carry a stable `code` and a readable `detail`.

**Storage.** Originals live in `data/uploads/originals/<aa>/<sha256>.<ext>`. Names come only from the
hash (no path traversal), files are hard-linked into place (never overwritten) and made read-only.

**Integrity.** The database enforces a unique SHA-256 per stored file, at most one non-duplicate
document per file (partial unique index), "duplicate ⇔ has an original", and "failed job ⇔ has an
error message". Concurrent identical uploads therefore yield exactly one original.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/documents/upload-limits` | Allowed extensions and size limits (used by the Upload page) |
| `POST /api/v1/documents` | Upload one file (`multipart/form-data`: `file`, `document_type`) |
| `GET /api/v1/documents?status=&document_type=&limit=&offset=` | List, newest first, with the latest job's stage statuses |
| `GET /api/v1/documents/{id}` | One document |
| `GET /api/v1/documents/stats` | Dashboard counts (`pending_approvals` is `null` until Phase 8) |

**Cross-site protection.** POST/PUT/PATCH/DELETE requests are rejected (403) when the browser's
`Origin` or `Sec-Fetch-Site` header shows another site. Per-session CSRF tokens arrive with login in Phase 9.

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
| No processing worker yet | Accepted documents stay `queued` | The worker arrives in Phase 3 with its first real stage (text extraction) |
| No login yet | Anyone who can reach the app can upload | Keep `BIND_HOSTS=127.0.0.1` until Phase 9 |
| The tailnet contains only Computer A | Computer B and the Tally host cannot connect yet | Join both devices to Computer A's tailnet (account `shivamacc1593@`) |
| SQLite stores datetimes without an offset | Naive values would reach the browser and be misread as local time | `UtcDateTime` column type stores UTC, always returns UTC-aware values and refuses naive input |

## TallyPrime connectivity prerequisites (Phase 6)

1. Computer A, Computer B and the Tally host are on the same tailnet. Tailscale
   ACLs, if used, allow A → Tally host:9000 and B → A:`APP_PORT`.
2. TallyPrime 6.2: F1 Help → Settings → Connectivity → Client/Server configuration.
   Set TallyPrime to act as *Server* (or *Both*) and note the port (default 9000).
3. Windows Firewall on the Tally host allows that port **only** from `100.64.0.0/10`.
4. A company is loaded in TallyPrime for read-only testing.

## Phases

1. Foundation ✅ · 2. Upload & processing foundation ✅ · 3. PDF text & OCR ·
4. AI extraction · 5. Duplicates & accounting validation · 6. Tally read-only ·
7. Voucher generation · 8. Approval & controlled import · 9. Dashboard,
security & networking · 10. End-to-end testing & deployment
