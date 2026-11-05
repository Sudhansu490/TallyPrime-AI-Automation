# Tally Automation

AI-assisted processing of purchase invoices and bank statements into
TallyPrime vouchers, with mandatory human review and approval.

**Status: Phase 4 (AI extraction and review).** Documents are uploaded, validated,
de-duplicated (exact file), and processed by a background worker: embedded PDF text,
OCR only where needed, then structured extraction by an LLM (Groq). Every field keeps
its evidence, confidence and status, and reviewers correct values on the Review page
(audited). Accounting validation (Phase 5) and Tally communication do not exist yet.
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
  processing/
    pdf_text.py      embedded PDF text (PyMuPDF) and the per-page "needs OCR" decision
    ocr.py           page rendering / image loading, PaddleOCR engine (CPU), line ordering
    normalize.py     layout-only text normalisation (never changes digits or symbols)
    pipeline.py      text stage: embedded text → OCR where needed → normalise
    layout.py        layout-preserving text: words placed by x position (keeps table columns)
    worker.py        background thread: claim job → text → AI extraction → persist; re-queue
  extraction/
    schemas.py       field catalogues → strict LLM JSON schema and Pydantic response models
    prompts.py       system/user prompts (PROMPT_VERSION is stored with every run)
    llm.py           provider adapter (Groq): strict JSON output, retries, rate-limit waits
    parsing.py       deterministic parsing: amounts, dates, GSTIN (checksum), IFSC, state codes
    service.py       batching, validation, evidence checks, conflict detection
    records.py       persistence, key columns, audited reviewer edits
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

Modules for accounting validation, duplicates, Tally and authentication are added in
their own phases.

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

## Text extraction and OCR (Phase 3)

Pipeline steps 5–7 run in a background worker thread inside the app process, one document
at a time (OCR is CPU-bound, and accuracy matters more than speed). Progress is stored per stage
on the processing job, and the Processing page polls it.

1. **Embedded text (PDF).** PyMuPDF reads each page's text layer in reading order.
2. **Per-page OCR decision.** A page is OCR'd only if its text layer has fewer than
   `OCR_MIN_TEXT_CHARS` letters/digits, or if it is mostly image (≥ 50 %) with under 200
   letters/digits (a scan with a typed header). Text PDFs never touch OCR.
3. **OCR.** Pages are rendered at `OCR_RENDER_DPI` (default 300, lowered automatically above
   `MAX_IMAGE_MEGAPIXELS`); JPEG/PNG are loaded upright (EXIF orientation). PaddleOCR 2.9 runs on
   CPU with angle classification and its detector at `OCR_DET_MAX_SIDE` px (its 960 px default loses
   small invoice text). The model loads once, on first use (~20 s), then takes ~5 s per page here.
   If OCR finds no more text than an existing thin text layer, the text layer is kept.
4. **Layout and normalisation.** Words (PDF) and OCR boxes are placed at a column proportional
   to their x position, so table columns stay aligned under their headers (see Phase 4: without
   this, a deposit and a withdrawal look identical in plain text). Normalisation then fixes only
   Unicode form (NFKC), whitespace characters, line endings and invisible characters; runs of
   spaces are kept. Digits, ₹, %, punctuation and case are never changed, and nothing is "corrected".

Per page, the database keeps the raw embedded text, the raw OCR text, the normalised text
that later stages will read, which source was chosen and why, and every OCR line with its
confidence and bounding box (the source evidence for Phase 4). OCR lines below
`OCR_LOW_CONFIDENCE` (0.85) are flagged and highlighted in the UI.

| Situation | Text extraction | OCR | Document |
|---|---|---|---|
| PDF with a text layer | succeeded | skipped | text_extracted |
| Scanned PDF, or some pages without text | succeeded | succeeded | text_extracted |
| JPEG/PNG | skipped | succeeded | text_extracted |
| No readable text on any page | succeeded / skipped | failed | failed |
| OCR engine error, missing or unreadable file | the stage that failed, with the reason | | failed |
| App stopped mid-job | the running stage is failed: "Interrupted…" | | failed |

Failed documents can be re-queued (`POST /api/v1/documents/{id}/reprocess`, or **Reprocess** on the
Processing page, after a confirmation). Each attempt is a new job; earlier attempts and their errors
are kept. Interrupted jobs are never restarted automatically, so a document that crashes the process
cannot loop.

**OCR is not trusted for amounts.** On a small synthetic test image, PaddleOCR read `1,180.00` as
`1.180.00` with 0.99 confidence; the same text on a full-page 300 DPI scan was read correctly. High
confidence therefore does not prove a figure. OCR text is stored as recognised, and amounts must be
checked arithmetically (line totals, taxes, grand total) in Phases 4–5 before anything is accepted.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/documents/{id}/text` | Pages of the latest successful attempt: text, source, raw texts, OCR lines and confidence |
| `POST /api/v1/documents/{id}/reprocess` | Queue a new attempt for a failed document (409 otherwise) |

**Cross-site protection.** POST/PUT/PATCH/DELETE requests are rejected (403) when the browser's
`Origin` or `Sec-Fetch-Site` header shows another site. Per-session CSRF tokens arrive with login in Phase 9.

## AI extraction (Phase 4)

Pipeline steps 8–12. After the page text is saved, the worker sends it to the configured LLM
and stores structured results for human review. **With `LLM_PROVIDER` blank, nothing leaves this
computer** and documents stop at *Text extracted*; once configured, **Run AI extraction** on the
Processing page re-runs them. When enabled, the text of every processed document (including
account numbers on statements) is sent to the provider.

**Provider and model.** Groq, `openai/gpt-oss-120b` by default: the largest model on the account
and one of the two Groq models with strict (constrained) JSON-schema output. Temperature 0,
`LLM_REASONING_EFFORT=medium`.

**What the LLM does, and what it does not.** It only *transcribes*: every value comes back as the
string printed on the document, with a verbatim evidence quote, its page and a confidence. It
never converts, calculates, maps ledgers or posts anything. The prompt tells it to use null for
anything not printed, and that the document text is data, not instructions.

**Safeguards, applied in code to every reply:**

1. The request uses a strict JSON schema, and the reply is validated again with Pydantic
   (`extra="forbid"`, strict types, confidence 0–1). Invalid JSON or a schema violation (ours or
   Groq's own `json_validate_failed`) gets one retry, then the document fails. Unvalidated output
   is never stored as data.
2. Output cut off at the token limit fails the document instead of silently dropping rows.
3. Values are parsed deterministically (`parsing.py`): Indian and Western digit grouping,
   ₹/Rs./INR, Dr/Cr and parentheses on balances, day-first dates (reported as an assumption),
   GSTIN format and check character (mod-36, verified against GSTN's published example), state
   codes, IFSC, HSN/SAC. Anything ambiguous (`1.180.00`, `31/02/2026`) stays unparsed and invalid.
4. **Anti-fabrication:** each value must occur in the document text (compared on letters and
   digits only), or the field is marked *invalid* with "may have been invented". The evidence
   quote must occur in the document and contain the value, or the field is flagged.
5. Flags: model confidence below `LLM_LOW_CONFIDENCE`, evidence on an OCR line below the OCR
   threshold, different values on different page batches (`conflict`, all alternatives kept),
   and missing required fields (invoice number, date, supplier, grand total; statement account).
6. The model also reports whether the text is the expected document type; a mismatch is shown
   as a document issue.

Long documents are sent `LLM_PAGES_PER_CALL` pages at a time; header fields are merged (first
value wins, disagreements flagged) and rows concatenated in page order.

**Storage.** `extraction_runs` (provider, model, prompt version, tokens, the validated raw
replies), `invoices` / `invoice_line_items`, `bank_statements` / `bank_transactions`. Each entity
keeps per-field JSON (`raw`, parsed `value`, `type`, `status`, `method` = extracted / calculated /
edited, `flags`, `messages`, `evidence`, `page`, `confidence`, `alternatives`) plus typed, indexed
key columns (GSTIN, invoice number/date, grand total; account number, period; transaction date,
debit, credit, balance, reference) for queries and Phase 5 duplicate detection. Money is stored
as exact decimal text, never as floats.

**Review and edits.** The Review page shows each field with its value, status tags
(Edited, Calculated, Low confidence, Check, Invalid, Required, Conflict), evidence quote, page and
confidence, and every line item / transaction as an editable grid. An edit is re-parsed and
re-checked by the backend with the same rules, keeps the first extracted value and evidence under
`original`, updates the key columns, and appends a row to `field_edits` (old, new, reason, time,
device address; the user id is filled once login exists in Phase 9). Edits are never deleted.

**Masking.** Account numbers are masked (`XXXXXXXX9012`) in every extraction and edit-history
response, including inside other fields' evidence quotes and transaction row text. The full
number stays in the database for matching. The Processing page's *View text* shows the document
text itself, unmasked, because it is the source document.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/documents/{id}/extraction` | Latest successful extraction: run info, fields, rows, summary |
| `PATCH /api/v1/extractions/{entity}/{id}/fields/{field}` | Edit one value (`{"value", "reason"}`), re-checked and audited |
| `GET /api/v1/extractions/{entity}/{id}/edits` | Edit history of one invoice / line item / statement / transaction |

**Observed with real Groq on synthetic documents (2026-11-05):**

| Document | Result |
|---|---|
| GST invoice, text PDF | All 21 printed fields and both line items correct; ~7k tokens, ~6 s |
| Same invoice, scanned | Correct except invoice number: OCR read "SOS" as "s0s" with high confidence. It is in the OCR text, so no check can catch it; the reviewer must. |
| Bank statement, 2 pages | First run put a ₹25,000 deposit in *debit*: plain text had lost the columns. Fixed by layout-preserving text; re-run correct. Phase 5's running-balance check is the second safety net. |
| Invoice schema | gpt-oss once answered `{"items": [...]}` instead of an array, so Groq's strict mode is not a guarantee. Describing the array fixed it; Pydantic validation plus one retry stays. |

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
The worker is disabled in tests and driven directly with a fake OCR engine; one test
(marker `ocr`, ~25 s) runs the real PaddleOCR. Skip it with `pytest -m "not ocr"`.

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
| In-process worker thread, job state in SQLite | No broker or second service; the browser polls; restart-safe because state is in the database |
| OCR only pages whose text layer is insufficient | Embedded text is exact and instant; OCR is slow and can misread figures |
| Keep raw and normalised text, and every OCR line with confidence and box | Later stages can cite evidence; normalisation stays auditable |
| Migrations disable SQLite foreign keys during batch rebuilds, then run `PRAGMA foreign_key_check` | Rebuilding a referenced table otherwise fails on a non-empty database |
| Static files sent with `Cache-Control: no-cache` | URLs are not versioned; revalidation (304) prevents stale JavaScript after upgrades |
| The LLM transcribes strings; code parses them | Number and date interpretation is deterministic, testable and never hidden in a model |
| Every value must be found in the document text | The cheapest strong defence against invented values |
| Field metadata as JSON plus typed key columns | Evidence/confidence/status per field without ~60 extra columns; typed columns where queries and constraints need them |
| Groq through its own SDK, behind a one-method interface | Strict JSON-schema support; other providers can be added as adapters |
| Provider error text never includes the generated output | Groq echoes the reply (document content) in schema errors; it must not reach logs or the database |

## Known issues

| Issue | Effect | Resolution |
|---|---|---|
| `paddlepaddle 2.6.2` needs `protobuf<=3.20.2` | A newer protobuf breaks Paddle import | `constraints.txt` pins 3.20.2 |
| `opencv-python`, `opencv-contrib-python` and `opencv-python-headless` are all installed | They share one `cv2` module (currently 4.11.0, working) | Kept deliberately: PaddleOCR 2.9.1 requires the first two and albumentations requires the headless one, so removing any breaks `pip check` and a reinstall brings it back. `test_real_paddleocr_reads_synthetic_text` proves the combination works. Revisit with PaddleOCR 3.x |
| Streamlit 1.66 and pyarrow 26 are still installed in `tally_env` (from the earlier UI) | Unused; pyarrow 26 needs NumPy ≥2 | No longer a project dependency. Optional: `pip uninstall streamlit pyarrow` |
| Groq account limit: 8,000 tokens/minute for gpt-oss-120b | One document uses ~4–7k tokens, so about one document per minute; extra documents wait | The client waits for Groq's `retry-after` (up to 90 s, `LLM_MAX_RETRIES` times). For volume, a paid Groq tier |
| OCR can misread letters as similar digits (`SOS` → `s0s`) with high confidence | Not detectable automatically when the misread text is all there is | Reviewer compares with the document; Phase 5 checks totals and GST arithmetic |
| Accounting checks (totals, GST, running balance) do not exist yet | A transcription can be internally consistent with the page but arithmetically wrong | Phase 5 |
| PaddleOCR 2.9 import takes ~20 s | The first OCR'd document after startup waits for the model | Loaded once per process, lazily in the worker thread, so app startup is not delayed |
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

1. Foundation ✅ · 2. Upload & processing foundation ✅ · 3. PDF text & OCR ✅ ·
4. AI extraction ✅ · 5. Duplicates & accounting validation · 6. Tally read-only ·
7. Voucher generation · 8. Approval & controlled import · 9. Dashboard,
security & networking · 10. End-to-end testing & deployment
