# Tally Automation

AI-assisted processing of purchase invoices and bank statements into
TallyPrime vouchers, with mandatory human review and approval.

**Status: all 10 phases complete (Phase 10: end-to-end tests, performance, deployment).**
Documents are uploaded, validated,
de-duplicated (exact file), and processed by a background worker: embedded PDF text, OCR
only where needed, structured extraction by an LLM (Groq), then accounting validation and
business-level duplicate detection. Reviewers correct values on the Review page and decide
possible duplicates on the Duplicates page (both audited). The backend reads companies,
groups, ledgers and voucher types from TallyPrime, and builds and checks TallyPrime voucher
XML with ledgers a reviewer chooses. Approved vouchers are imported into TallyPrime one at a
time from Computer A, only with `TALLY_WRITE_ENABLED=true`, never twice, and verified by
reading them back. Every page and API needs a login; roles (viewer, reviewer, approver,
admin) decide who may change data, approve and import.

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

Observed environment (re-inspected 2026-11-06): Computer A is `SUDHANSU`
(Tailscale `100.111.30.101`, tailnet account `shivamacc1593@`; Windows 11 Pro,
i5-12500, 32 GB RAM, no usable GPU, so OCR runs on CPU). TallyPrime is not installed on
Computer A. The **TallyPrime host is `desktop-a0fljal`** (Tailscale `100.124.182.62`,
confirmed by the owner on 2026-11-06): TallyPrime 6.2 serves XML on port 9000 and its
license server answers on 9999. Computer B has not joined the tailnet yet. An earlier inspection on
2026-11-04 recorded a different machine (`desktop-6fjvg7s`, `100.95.249.91`); that
entry is superseded.

## Layout

```
src/tally_automation/
  __main__.py        `python -m tally_automation` → cli.main()
  cli.py             server (default), `users …`, `migrate`, `backup`, `check`
  operations.py      schema status/migration, backups, pre-flight check
  server.py          bind-address policy + Uvicorn launcher
  config.py          settings from environment / .env
  logging_config.py  JSON logs with redaction of sensitive fields
  security.py        password hashing (stdlib scrypt), session token helpers
  auth/
    service.py       accounts, login, lockout, server-side sessions, password changes
    middleware.py    client allow-list, login gate, CSRF check
    deps.py          current user and role requirements for routes
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
    worker.py        background thread: claim job → text → AI extraction → validation; re-queue
  extraction/
    schemas.py       field catalogues → strict LLM JSON schema and Pydantic response models
    prompts.py       system/user prompts (PROMPT_VERSION is stored with every run)
    llm.py           provider adapter (Groq): strict JSON output, retries, rate-limit waits
    parsing.py       deterministic parsing: amounts, dates, GSTIN (checksum), IFSC, state codes
    service.py       batching, validation, evidence checks, conflict detection
    records.py       persistence, key columns, audited reviewer edits
    masking.py       account-number masking for every API response
  validation/
    accounting.py    extraction and accounting checks (pure functions, never change values)
    duplicates.py    Level 2 (invoice) and Level 3 (bank transaction) matching rules
    service.py       run checks, store results, refresh matches, eligibility, reviewer decisions
  tally/
    xmlio.py         Export request templates, read-only guard, tolerant response parsing
    client.py        HTTP client: host rules, timeouts, size limit, companies and masters
    masters.py       parsing of companies, groups, ledgers, voucher types; primary groups
    service.py       connection check, snapshot retrieval, interrupted-run recovery
    importing.py     voucher import guard, marker lookup request, import answer parsing
  vouchers/
    drafts.py        voucher entries from reviewed values and chosen ledgers (no derived amounts)
    xml.py           TallyPrime import XML rendering and independent structural verification
    compat.py        TallyPrime compatibility checks against the retrieved masters
    service.py       suggestions, generation, superseding, mapping memory, staleness
    approval.py      approve / reject / withdraw, tied to the XML's SHA-256
    importer.py      controlled import: checks, duplicate lookup, send, read-back, recovery
  network.py         Tailscale address ranges (bind addresses and TallyPrime host)
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
| `GET /api/v1/documents/stats` | Dashboard counts (`pending_approvals`: ready vouchers awaiting a decision) |

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

**Fallback model.** Each Groq model has its own daily quota (gpt-oss-120b: 200,000 tokens/day on
the free tier, about 20-25 invoices). When the main model reports a *daily* limit, requests
switch at once to `LLM_FALLBACK_MODEL` (`openai/gpt-oss-20b`) and stay there until Groq says the
main model is available again; per-minute limits still just wait. A document read by the
fallback is labelled with that model and gets a validation warning to check every value. If the
fallback is exhausted too, processing fails at once with a clear message (no pointless waiting).

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
debit, credit, balance, reference) for queries and duplicate detection (Phase 5). Money is stored
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

## Validation and duplicates (Phase 5)

Pipeline step 11. Right after AI extraction, and again after every reviewer edit, the worker
runs the `validation` package over the stored values. **It never changes a value**: each rule
reports what it calculated, what was printed and the difference, and a person decides. Calculated
values appear only in the check results, tagged *Calculated*; they are never written into the
extracted fields.

**Two categories** (the spec's third, TallyPrime compatibility, is checked per voucher in Phase 7):

* *Extraction*: invalid or unreadable values, missing required fields, unresolved conflicts
  between pages (errors); low confidence and parser warnings (warnings).
* *Accounting*:
  * **Invoices.** Per line: tax = taxable × rate (error), CGST rate = SGST rate and charged
    together, never both IGST and CGST/SGST, taxable = qty × rate − discount (warning; some
    invoices print tax-inclusive rates), and line total.
  * **Invoice header.** Totals = sum of the lines (taxable, CGST, SGST, IGST, cess), and
    grand total = taxable + GST + cess + round-off. A missing round-off with a whole-rupee total
    within ₹1 is a warning, any other difference an error.
  * **Invoice consistency.** Supplier state versus place of supply (CGST+SGST versus IGST; a
    warning, because SEZ supplies are legitimate exceptions), the same GSTIN as supplier and
    buyer, future dates, the due date before the invoice date, and amount payable ≠ grand total.
  * **Bank statements.** Each row has exactly one of debit or credit, with a date that is
    present, not in the future and inside the period.
  * **Statement balances.** The running balance is checked row by row (exact, no tolerance), in
    chronological order, so statements printed newest first are checked bottom up. A mismatch
    that disappears when debit and credit are swapped is reported as *probably in the wrong
    column*. Closing = opening − debits + credits, and it must equal the last row's balance.
  * **Repeated rows.** Identical rows inside one statement are warned about.

Tolerances are explicit: ₹0.01 for one calculated tax amount, ₹0.01 per line for sums of lines,
none for bank balances. `Dr` balances are negative, `Cr` or unmarked positive.

Outcome: **passed**, **passed with warnings**, or **failed** (≥1 error). Results are stored per
extraction run in `validation_results` (`RULES_VERSION` recorded). If validation itself crashes,
the paid-for extraction is kept (it runs in a savepoint), the stage shows *failed*, and **Run
validation again** on the Review page retries it without calling the LLM.

**Duplicate detection.** Matches are stored in `duplicate_matches` (one row per pair, enforced
by a unique key so concurrent detection cannot record a pair twice) with field-by-field evidence
(same / similar / different / unknown). Nothing is rejected automatically.

| Level | Compares | Rules |
|---|---|---|
| 1 | File bytes (SHA-256), at upload | Identical re-upload → recorded as `duplicate`, never processed (Phase 2) |
| 2 | Invoices across documents | Same supplier GSTIN + same invoice number (letters and digits only) → probable. Number equal after mapping OCR look-alikes (O/0, I/L/1, S/5, B/8, Z/2) → probable with matching date or amount, else possible. GSTIN missing + number + date/amount → probable/possible. Same supplier, date and amount, different number → possible |
| 3 | Bank transactions of the **same account** in different statements | Fingerprint (SHA-256 of account, date, direction, amount, reference, normalised description), or same date/direction/amount plus the same reference, description or resulting balance → probable |

Two records that only share a date and an amount are **not** flagged. The later upload is the
candidate duplicate. A reviewer marks a match *duplicate*, *not a duplicate*, or reopens it; every
decision is appended to `duplicate_reviews` (decision, note, time, device). Unreviewed matches that
stop applying after an edit (e.g. a corrected invoice number) are removed; decided ones are kept.

**Eligibility for voucher generation** (computed on demand, never cached) requires: document
processed, validation on the current extraction with **no errors**, and **no unreviewed duplicate
matches** on either side. A confirmed duplicate invoice is never eligible; a confirmed duplicate
bank transaction is left out while the rest of its statement stays eligible. Warnings do not
block; they are shown again at approval (Phase 8).

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/documents/{id}/validation` | Checks, outcome, eligibility and duplicate summary for the current extraction |
| `POST /api/v1/documents/{id}/validate` | Re-run validation and duplicate detection (no LLM call) |
| `GET /api/v1/duplicates?status=open\|resolved\|all` | Matches with evidence, plus identical-file re-uploads and counts |
| `POST /api/v1/duplicates/{id}/review` | Record `duplicate`, `not_duplicate` or `reopened` (optional note) |
| `POST /api/v1/duplicates/review` | Same decision for up to 500 matches at once, all or nothing (overlapping statements) |

Account numbers are masked in all of these, including inside descriptions in duplicate evidence.
Reference numbers (UTR, cheque) are shown, since reviewers need them to compare.

**Observed on the synthetic documents from Phase 4 (2026-11-05).** Validation was run over the
six existing extractions:

* Document 8, the old statement run that put a ₹25,000 deposit in *debit*, now **fails**: the
  running-balance check reports that transaction 2 *balances only if debit and credit are swapped*,
  and the closing balance is off by ₹50,000. The corrected re-run (document 11) passes.
* Document 7, the scanned invoice where OCR read "SOS" as `s0s`, is matched to the same invoice
  (document 6) by the look-alike rule, the case Phase 4 could not detect.
* Re-uploads of the same invoices and the same statement were all matched with their evidence.

## TallyPrime read-only integration (Phase 6)

The backend on Computer A reads master data from TallyPrime over XML/HTTP. The browser never
contacts TallyPrime. **This build cannot write to TallyPrime at all:** the client has no import
method, every request body is built from fixed templates as `<TALLYREQUEST>Export</TALLYREQUEST>`
of an inline TDL collection, and `assert_read_only` refuses anything containing `Import`,
`IMPORTDATA`, `TALLYMESSAGE` or `ACTION=` immediately before it reaches the network (tested).
`TALLY_WRITE_ENABLED` stays `false`; voucher import is Phase 8 and needs explicit approval.

**Network rules.** `TALLY_HOST` must be loopback or a Tailscale address. A hostname is accepted
only if every address it resolves to is. Proxy environment variables are ignored, redirects are
not followed, and responses over `TALLY_MAX_RESPONSE_MB` are refused.

**What is retrieved** (per company, only companies currently *loaded* in TallyPrime):

| Master | Fields | Not retrieved |
|---|---|---|
| Groups | name, aliases, parent, primary group (computed), revenue/balance-sheet, debit/credit nature | — |
| Ledgers | name, aliases, parent and primary group, GSTIN (latest dated registration, checksum-tested), GST registration type and state, tax type, GST duty head and rate, bank account no. and IFSC (bank ledgers), bill-wise | balances, addresses, contacts, PAN |
| Voucher types | name, aliases, base type (Purchase, Payment, …), numbering method, active | — |

A retrieval replaces the company's stored snapshot in one transaction. If anything fails, the
previous snapshot stays and the run is recorded as failed with the reason. Only one retrieval
runs at a time, and runs interrupted by a shutdown are marked failed at the next start. Bank
account numbers are stored in full (to match statements to bank ledgers in Phase 7) and masked
in every response. Logs record counts and timings only, never ledger names, GSTINs or account
numbers.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/tally/status` | Live check: server running, response time, loaded companies, read-only mode |
| `POST /api/v1/tally/sync` | `{"company": "<exact name>"}`: retrieve that loaded company's masters |
| `GET /api/v1/tally/sync-runs` | Retrieval history with counts, warnings and errors |
| `GET /api/v1/tally/companies` | Companies retrieved at least once, with counts |
| `GET /api/v1/tally/companies/{guid}/ledgers` | Search (`q` = name or GSTIN), `group`, `kind` = `gst_party` \| `gst_duty` \| `bank` \| `invalid_gstin`, paged |
| `GET /api/v1/tally/companies/{guid}/groups` | Groups with primary group and nature |
| `GET /api/v1/tally/companies/{guid}/voucher-types` | Voucher types with base type, numbering and active flag |

**Observed with the real TallyPrime 6.2 on `desktop-a0fljal` (2026-11-06):**

* `GET /` answers `TallyPrime Server is Running`; the license server on port 9999 reports
  release 6.2.0 (27317). One company was loaded.
* All masters (40 groups, 1,827 ledgers, 25 voucher types) were read in **0.9 s**, with every
  ledger resolved to its primary group except *Profit & Loss A/c* (it has no group by design).
* TallyPrime sends XML that strict parsers reject: `&#4;` markers and raw control characters
  (`0x02`) inside values. They are removed before parsing and counted per retrieval.
* GSTINs live mostly in the dated `LEDGSTREGDETAILS` list (TallyPrime 4+), not in the old
  `PARTYGSTIN` field (311 versus 184 ledgers), so the latest dated entry is used.
* **3 ledger GSTINs fail the GSTIN check character** (probably mistyped in Tally). They are flagged
  (*GSTIN fails check* filter) so Phase 7 will not match invoices against them silently.
* **An unknown company name is not an error:** TallyPrime answers `STATUS 1` with no records.
  The client therefore checks the company against the loaded list first, and treats a company
  with no groups or voucher types (every company has predefined ones) as a failure.
* When TallyPrime is closed, port 9000 refuses connections while the license server on 9999
  keeps answering; the status page reports *Unreachable* with the reason.

## Voucher generation and XML validation (Phase 7)

For an **eligible** document (validation passed, duplicates reviewed), the *Generate* page
builds TallyPrime vouchers with ledgers a reviewer chooses, renders the TallyPrime import
XML, checks it, and stores it. **Nothing is sent to TallyPrime**: there is no import code
in this phase, the read-only client refuses any import body (tested against the generated
XML), and importing will be a separate, approved operation (Phase 8).

**Voucher forms** (ledgers only; no stock items, as confirmed for this company):

| Source | Voucher | Entries |
|---|---|---|
| Purchase invoice | Purchase (accounting invoice) | Supplier **Cr** grand total (bill-wise: *New Ref* = supplier invoice no.) · Purchase ledger(s) **Dr** taxable value · CGST/SGST/IGST/Cess ledgers **Dr** printed totals · Round-off **Dr/Cr** |
| Bank debit (money out) | Payment | Other ledger **Dr** · Bank **Cr** |
| Bank credit (money in) | Receipt | Bank **Dr** · Other ledger **Cr** |
| Either, other ledger is a bank or cash ledger | Contra | as above |

The taxable value is split across purchase ledgers by GST rate only when every line item
shows a taxable amount and a rate and the lines add up *exactly* to the printed total;
otherwise it goes to one purchase ledger (and the page says why). Amounts are never
derived: if taxable value + taxes + round-off differs from the grand total, no voucher is
built. Payments to bill-wise parties are recorded *On Account* (adjust against bills in
TallyPrime). Supplier invoice number and date go to *Reference* and *Reference date*;
voucher numbers are left to TallyPrime's numbering. Each narration carries a marker such as
`TA-1A2B3C4D5E-INV` (from the document file's SHA-256 and the record), so Phase 8 can find
an imported voucher in TallyPrime and refuse to post it twice.

**Ledger choice.** The page shows *suggestions*, never decisions: ledgers with the same
GSTIN as the invoice, the bank ledger with the statement's account number, GST ledgers
with the matching duty head (and rate), and the ledger chosen last time for the same
supplier, rate, head, bank account or transaction description. No AI is involved. Only an
unambiguous suggestion is pre-selected; the reviewer submits every ledger, and the backend
re-checks each one against the retrieved masters.

**Checks** (category *TallyPrime*, stored with each voucher; errors block it):

* Every ledger and the voucher type exist in the retrieved masters; the voucher type is
  active and of the right base type (Purchase, Payment, Receipt, Contra).
* Each ledger fits its role: party not a tax/bank/purchase ledger (warning if not under
  Sundry Creditors); purchase ledger not a tax/bank/party ledger; tax amounts on GST duty
  ledgers of the same head (warning if the ledger's rate differs from the invoice's); bank
  ledger under a bank group, with the statement's account number when both are known; no
  ledger in two roles.
* The supplier ledger's GSTIN equals the invoice GSTIN; CGST/SGST versus IGST fits the
  supplier's state (from the GSTIN) and the company's state.
* The date lies inside the company's books and not in the future; debits equal credits.
* The XML is parsed back independently: one voucher, `ACTION="Create"`, the chosen company
  and voucher type, valid dates and two-decimal amounts, signs that agree with debit/credit,
  bill allocations equal to their entries, a zero sum, and exactly the draft's entries.

A voucher is **ready** (may go to approval in Phase 8) or **blocked**. Generating again
supersedes the previous voucher for that source (one current voucher per source, enforced
by a partial unique index). A voucher becomes **out of date** when the document is edited or
extracted again, the masters are retrieved again, or the document is no longer eligible;
Phase 8 will refuse to approve it until it is generated again.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/documents/{id}/voucher-setup` | Eligibility, what the voucher(s) need, ledger suggestions (`?company=` GUID) |
| `POST /api/v1/documents/{id}/vouchers` | Generate with the submitted ledgers (`invoice` or `statement` choices) |
| `GET /api/v1/documents/{id}/vouchers` | The document's current vouchers (`?include_superseded=true` for history) |
| `GET /api/v1/vouchers` | Current vouchers of all documents (`?status=ready`, `blocked` or `superseded`) |
| `GET /api/v1/vouchers/{id}` | One voucher with its import XML (bank account numbers masked) |

**Observed with the real masters (2026-11-06):** the company's Purchase, Payment, Receipt and
Contra voucher types use no voucher numbering; there are two CGST 9% ledgers and a generic
CGST ledger, so the CGST suggestion is ambiguous and a person must choose; 12 GSTINs are shared
by more than one ledger, so a same-GSTIN suggestion can list several parties; some bank
ledgers carry the account number in the ledger *name* (not in the bank details), so the bank
account check reports "could not compare" for them, and the name shows the number on screen.

## Approval and controlled import (Phase 8)

The **Vouchers** page lists generated vouchers by state (awaiting approval, approved, import
failed, import unclear, in TallyPrime, rejected, blocked). Every action opens a confirmation
dialog and is re-checked by the backend.

**Who may act.** Until login exists (Phase 9), approve, reject, withdraw and import are accepted
only from a browser **on Computer A itself** (loopback, or the server's own address on both ends
of the connection); any other device gets `403 not_local`. Every decision and every import
attempt records the device address. Reading the vouchers is possible from any allowed device.

**Approval** is tied to the voucher's exact import XML (its SHA-256). Only a *ready* voucher that
is not out of date can be approved, and its warnings must be acknowledged in the dialog.
Rejecting needs a reason; an approval can be withdrawn before import. Generating a voucher
again withdraws the old approval automatically. Decisions are appended to `voucher_approvals`
and never changed.

**Import** is the only operation that writes to TallyPrime. It requires
`TALLY_WRITE_ENABLED=true` (otherwise the TallyPrime client cannot write at all), an approval
for the current XML, a voucher that is not out of date, and the confirmation dialog. Then:

1. The company named in the voucher must be open in TallyPrime; otherwise nothing is sent.
2. **Duplicate-posting protection:** TallyPrime is searched (read-only) for a voucher whose
   narration carries this voucher's marker, over the whole period of the books. If one exists,
   nothing is sent; the voucher is recorded as *already in TallyPrime*.
3. The body must pass `assert_voucher_import` immediately before sending: one `Import Data`
   request to the Vouchers report with exactly one `VOUCHER ACTION="Create"`, no masters, no
   alter/delete, no TDL. The read path (`assert_read_only`) refuses any import body.
4. TallyPrime's answer is parsed (`CREATED`, `ERRORS`, `LINEERROR`, …). After *created*, the
   voucher is read back by its marker and its date, voucher type and every ledger amount are
   compared with what was approved (*verified*).

| Attempt result | Voucher state | Next step |
|---|---|---|
| created / already in TallyPrime | In TallyPrime (locked: no regeneration, rejection or second import) | — |
| refused by TallyPrime (errors) | Import failed | Fix the cause; retry, or reject and generate again |
| not sent (connection, company not open, lookup failed) | Import failed | Retry |
| sent, answer missing or unclear (timeout, lost connection) | Import unclear | Import again: the first step finds the voucher if TallyPrime created it, so it is never posted twice |

Imports run one at a time. Each attempt is committed as *sending* before anything is sent; an
attempt interrupted by a shutdown becomes *unclear* at the next start. All attempts are kept in
`voucher_imports` with TallyPrime's counters, its voucher id and the verification result.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/vouchers/permissions` | Whether this browser may decide/import, and whether writing is enabled |
| `GET /api/v1/vouchers?approval_status=&import_status=` | Filtered lists for the Vouchers page |
| `POST /api/v1/vouchers/{id}/approve` | `{"acknowledge_warnings": true, "note": …}` |
| `POST /api/v1/vouchers/{id}/reject` · `/revoke` | Reject (reason required) or withdraw an approval |
| `POST /api/v1/vouchers/{id}/import` | `{"confirm": true}`: check, send, verify; returns the attempt |
| `GET /api/v1/vouchers/{id}/history` | Every decision and import attempt |

**Live test (2026-11-07), in a renamed restored copy of the company** (*… TEST COPY*, with the real
company closed): a Receipt of ₹25,000 from the synthetic statement was approved and imported
through the page. TallyPrime answered `CREATED 1 · ERRORS 0 · LASTVCHID 32821` in about 2 s, and
the read-back matched. With the voucher then marked *unclear*, a second import found it by its
marker and sent nothing; afterwards exactly one voucher in the copy carried the marker.

**Observed:** the copy restored from a backup keeps the original company's GUID, so retrieving
its masters replaces the stored masters of the real company (same GUID, the copy's name). After
testing in a copy, close it, open the real company and retrieve its masters again; the
application's import always names the company and refuses when that company is not open.

## Login, roles and security (Phase 9)

**Every page and API needs a login**, except the login page itself, static assets and a bare
liveness probe (`GET /api/v1/health/live`). Pages redirect to `/login` (and back afterwards,
to same-site paths only); the API answers `401`.

**Accounts** are created and managed only on Computer A, from the command line; there is no
self-registration and no password is ever passed as an argument:

```powershell
python -m tally_automation users create sudhansu --role admin --display-name "Sudhansu"
python -m tally_automation users list
python -m tally_automation users set-password NAME      # hidden prompt, twice
python -m tally_automation users set-role NAME approver
python -m tally_automation users deactivate NAME        # also: activate, unlock
```

| Role | May |
|---|---|
| viewer | read every page and record |
| reviewer | + upload, reprocess, correct extracted values, re-validate, decide duplicates, retrieve TallyPrime masters, generate vouchers |
| approver | + approve, reject and withdraw vouchers, import them into TallyPrime |
| admin | everything (user management stays on the command line) |

Roles are enforced on the backend for every state-changing endpoint (`403 forbidden`); pages
only hide what the role cannot do. Every action records the user (and the device): uploads,
edits, duplicate decisions, masters retrievals, voucher generation, approvals and imports. With
`REQUIRE_SEPARATE_APPROVER=true`, nobody can approve a voucher they generated. This replaces
Phase 8's interim "approve and import only on Computer A" rule.

**Passwords and sessions.**

* Passwords: scrypt (standard library, OWASP parameters N=2^17, r=8, p=1; ~0.3 s per check),
  at least 12 characters, not containing the username. Hashes are re-computed at login if
  stronger parameters are introduced later.
* Sessions are server-side (`user_sessions`). The browser holds only a random 256-bit token in
  an `HttpOnly`, `SameSite=Strict` cookie; the database stores only its SHA-256. Nothing is put
  in `localStorage`/`sessionStorage`.
* A session ends after `SESSION_IDLE_MINUTES` (60) of inactivity, after `SESSION_MAX_HOURS` (12),
  at logout, and when the password or role changes or the account is deactivated.
* `LOGIN_MAX_FAILURES` (5) wrong passwords lock the account for `LOGIN_LOCKOUT_MINUTES` (15).
  Unknown usernames get the same answer, in the same time, as wrong passwords.
* The cookie gets the `Secure` flag when served over HTTPS. Inside the tailnet the app is served
  over HTTP; Tailscale's WireGuard tunnel already encrypts the connection between devices.

**CSRF.** Every POST/PUT/PATCH/DELETE under `/api/` must carry the session's own CSRF token
in `X-CSRF-Token` (the page receives it in a `<meta>` tag; `api.js` adds it). This comes on top
of the SameSite=Strict cookie and the existing Origin/`Sec-Fetch-Site` check.

**Network.** Requests pass, in order: the trusted-Host check (DNS rebinding), security headers
(CSP without inline scripts, no-store, frame denial, …), the **client allow-list**, the
cross-site check, then login and CSRF. `ALLOWED_CLIENT_NETWORKS` defaults to this computer and
the tailnet; list specific Tailscale IPs to admit only Computer A and Computer B. Only loopback
and Tailscale ranges are accepted in that setting, and the server binds only loopback/Tailscale
addresses (`BIND_HOSTS`, no `0.0.0.0`). Uvicorn ignores `X-Forwarded-*`, so client addresses
cannot be spoofed through headers.

**Dashboard** now also shows vouchers awaiting approval, approved but not imported, in
TallyPrime, failed and unclear imports, and blocked vouchers, each linking to the Vouchers page.

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/auth/login` | `{"username", "password"}`; sets the session cookie |
| `POST /api/v1/auth/logout` | Ends the session |
| `GET /api/v1/auth/me` | The signed-in user and role |
| `POST /api/v1/auth/password` | Change one's own password (ends other sessions) |
| `GET /api/v1/vouchers/stats` | Voucher counts for the dashboard |
| `GET /api/v1/health/live` | Public liveness probe (no details) |

## Deployment on Computer A (Phase 10)

The app runs as one Python process on Computer A, started with Windows by Task Scheduler (no
extra software), kept running by a small supervisor script, and backed up daily. Browsers use
plain HTTP inside the tailnet; Tailscale's WireGuard tunnel encrypts every connection, and the
app keeps seeing each device's real address for the allow-list and the audit trail.

### First deployment

1. **Settings** (`.env`): `APP_ENV=production` (hides `/docs`), `BIND_HOSTS=127.0.0.1,<Computer A
   Tailscale IP>`, and `BACKUP_DIR` on another drive or disk (e.g. `D:\TallyAutomationBackups`).
   Leave `TALLY_WRITE_ENABLED=false` until you are ready to import.
2. **Schema and first administrator:**
   ```powershell
   tally_env\Scripts\python -m tally_automation migrate
   tally_env\Scripts\python -m tally_automation users create NAME --role admin
   ```
3. **Pre-flight check** (also contacts TallyPrime, read-only; `--no-tally` skips that):
   ```powershell
   tally_env\Scripts\python -m tally_automation check
   ```
   `FAIL` lines must be fixed; `WARN` lines are worth reading.
4. **Start with Windows** — in PowerShell opened *as administrator*, from the project folder:
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1 -AllowTailnet
   ```
   This runs the check, then registers two tasks under `\TallyAutomation\` that run as your
   Windows user whether or not anyone is logged in (S4U: no password is stored):
   * **Tally Automation** – at startup (1 minute delay, so Tailscale is up), runs
     `deploy\run-app.ps1`, which starts the app and starts it again 10 s after it stops. After 5
     stops within 5 minutes it gives up (a configuration problem, not a crash).
   * **Tally Automation backup** – daily at 02:00 (`-BackupTime 21:30` to change).
   * `-AllowTailnet` adds one firewall rule: TCP 8000 from `100.64.0.0/10` only (for Computer B).
5. Open `http://127.0.0.1:8000/` on Computer A, or `http://<Computer A Tailscale IP>:8000/` on
   Computer B, and log in.

`deploy\uninstall-tasks.ps1` (as administrator) removes the tasks and the firewall rule; data,
documents, backups and `.env` stay.

### Operating it

| Task | How |
|---|---|
| Is it running? | `Get-ScheduledTask -TaskPath \TallyAutomation\ \| Get-ScheduledTaskInfo`, or `http://127.0.0.1:8000/api/v1/health/live` |
| Logs | `data\logs\app.log` (JSON, rotated), `data\logs\supervisor.log` (starts/stops), `data\logs\app-stderr.log` |
| Backup now | `tally_automation backup` (consistent while the app runs; integrity-checked; keeps `BACKUP_KEEP` copies) |
| Restore | Stop the task, copy the chosen `tally_automation-YYYYMMDD-HHMMSS.db` over `data\tally_automation.db`, copy `uploads\` back into `data\uploads\`, start the task, run `check` |
| Upgrade | Stop the task · `git pull` · `pip install -r` / `-e .` if dependencies changed · `tally_automation migrate` · `tally_automation check` · start the task. The server refuses to start on an outdated schema |
| Allow imports | Set `TALLY_WRITE_ENABLED=true` in `.env`, restart the task |
| Accounts | `tally_automation users …` (see Phase 9) |

(`tally_automation …` = `tally_env\Scripts\python -m tally_automation …` from the project folder.)

### Verified on Computer A (2026-11-07)

* The supervisor started the production launcher; the app answered at once, and after its
  process was killed it was running again ~10 s later. (Tasks themselves are registered by you,
  as administrator; they were not installed by this phase's checks.)
* `backup`: 1.4 MB database copy, integrity ok, 11 stored documents copied.
* `check`: schema current, directories writable, both bind addresses valid, Groq key set,
  TallyPrime answered in 78 ms with one company open, writes disabled. Open items: no
  administrator yet, `APP_ENV=development`, backups on the same drive.

## Performance (Phase 10)

Measured with `tests\perf_volume.py` (300 synthetic invoices from one supplier + 30 two-page
statements, fake LLM; real duplicate detection and validation):

| Request | Time |
|---|---|
| Dashboard figures | 209 ms (was 1,514 ms: eligibility is now computed in bulk) |
| Documents list (200) | 81 ms |
| Duplicates page | 230 ms |
| Re-validate one invoice (with duplicate search over 300) | 79 ms |
| Voucher setup, vouchers list, voucher figures | 5–24 ms |

The real bottlenecks are elsewhere and deliberate: OCR (seconds per scanned page, CPU) and the
Groq rate limit (about one document per minute on the free tier).

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

### First start

Create the first administrator on Computer A (the app logs a warning while no account exists):

```powershell
python -m tally_automation users create NAME --role admin
```

### Access from Computer B

1. Create an account for each person who will use Computer B (`users create …`).
2. Join Computer B to the same tailnet.
2. In `.env`, set `BIND_HOSTS=127.0.0.1,<output of tailscale ip -4>`. Optionally
   set `EXTRA_ALLOWED_HOSTS=<Computer A MagicDNS name>`.
3. Allow inbound TCP `APP_PORT` on Computer A in Windows Firewall, from
   `100.64.0.0/10` only. Do not accept Windows' "allow on all networks" prompt.
4. Optionally narrow `ALLOWED_CLIENT_NETWORKS` to `127.0.0.1/32,<A's IP>/32,<B's IP>/32`.
5. Browse to `http://<Computer A Tailscale IP>:8000/` from Computer B and log in.

Requests with any other `Host` header are rejected with HTTP 400 (DNS-rebinding protection).

## Tests

```powershell
pytest
```

Tests use a temporary SQLite database per test, ignore `.env`, never start a
real server, and never need TallyPrime. Test data is synthetic and labelled as such.
The worker is disabled in tests and driven directly with a fake OCR engine; one test
(marker `ocr`, ~25 s) runs the real PaddleOCR. Skip it with `pytest -m "not ocr"`.

`tests/test_end_to_end.py` drives the whole workflow through the HTTP API with each role:
upload → text → AI extraction (fake LLM) → correction → validation → masters (fake TallyPrime)
→ purchase, payment and contra vouchers → approval → import → read-back, then checks the
dashboard figures and that the logs contain no account numbers, GSTINs, party or ledger names
or tokens. Browser automation (Playwright) was deliberately not added: it needs ~150 MB of
extra packages, the pages hold no business logic, and every page was checked in a browser
during development. `tests/perf_volume.py` is a manual volume timing script (not collected).

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
| Validation reports, never corrects | Calculated values live only in check results; changing a value is always a reviewer edit, audited |
| Duplicates are flagged, never rejected | Same date and amount is common for genuine records; a person decides with the evidence |
| Eligibility computed on demand | Depends on edits, decisions and matches in other documents; a cached flag could go stale |
| Validation runs in a savepoint after extraction | A bug in validation must not throw away a paid LLM extraction |
| TallyPrime client has no write path at all in Phase 6 | Read-only is enforced by construction and by a guard on every request body, not by a setting |
| Masters stored as a per-company snapshot, keyed by Tally GUID | Mapping (Phase 7) needs them offline and consistent; GUIDs survive renames |
| Inline TDL collections instead of built-in reports | Exactly the fields needed, nothing more; no TDL is installed in TallyPrime |
| Purchase vouchers in accounting-invoice form (ledgers only) | The company records purchases without stock items; works with the retrieved masters |
| Ledgers submitted with every generation; memory only suggests | A person decides every mapping; a remembered choice can never post silently |
| One import XML per voucher, stored with its SHA-256 | Phase 8 imports and verifies each voucher individually; the approved bytes are the imported bytes |
| Marker from the file hash in every narration | Lets Phase 8 detect an earlier import in TallyPrime even after a database rebuild |
| Freshness computed on read (content digest, run, masters, eligibility) | An edit after generation can never be approved unnoticed |
| Approve/import only from Computer A until Phase 9 | Login arrives in Phase 9; restricting to the processing computer is enforceable on the backend now (replaced by roles in Phase 9) |
| Server-side sessions, token hashed in the database | Logout, idle timeout and password changes end sessions at once; a database leak reveals no usable token |
| Per-session CSRF token in a header, plus SameSite=Strict and Origin checks | Several independent defences against cross-site requests |
| scrypt from the standard library instead of bcrypt/argon2 | Memory-hard, OWASP-recommended, and needs no extra package |
| Accounts only via the command line on Computer A | No self-registration or admin web page to attack; managing a handful of users needs nothing more |
| Client allow-list of loopback/Tailscale ranges | A second barrier behind Tailscale ACLs and the bind addresses |
| Separate guards for reading and importing, and no import without `TALLY_WRITE_ENABLED` | A bug elsewhere cannot turn a read into a write, or send anything but one new voucher |
| Lookup by marker before every import | Makes retries after an unclear answer safe; protects against posting twice even after a database rebuild |
| Read-back verification after import | TallyPrime's "created" is confirmed against what was approved |

## Known issues

| Issue | Effect | Resolution |
|---|---|---|
| `paddlepaddle 2.6.2` needs `protobuf<=3.20.2` | A newer protobuf breaks Paddle import | `constraints.txt` pins 3.20.2 |
| `opencv-python`, `opencv-contrib-python` and `opencv-python-headless` are all installed | They share one `cv2` module (currently 4.11.0, working) | Kept deliberately: PaddleOCR 2.9.1 requires the first two and albumentations requires the headless one, so removing any breaks `pip check` and a reinstall brings it back. `test_real_paddleocr_reads_synthetic_text` proves the combination works. Revisit with PaddleOCR 3.x |
| Streamlit 1.66 and pyarrow 26 are still installed in `tally_env` (from the earlier UI) | Unused; pyarrow 26 needs NumPy ≥2 | No longer a project dependency. Optional: `pip uninstall streamlit pyarrow` |
| Groq account limit: 8,000 tokens/minute for gpt-oss-120b | One document uses ~4–7k tokens, so about one document per minute; extra documents wait | The client waits for Groq's `retry-after` (up to 90 s, `LLM_MAX_RETRIES` times). Daily limit (200k tokens): `LLM_FALLBACK_MODEL` takes over. For volume, a paid Groq tier |
| OCR can misread letters as similar digits (`SOS` → `s0s`) with high confidence | The value itself cannot be checked against anything but the page | Reviewer compares with the document. Duplicate detection maps look-alikes, so a misread copy of a known invoice is still matched; arithmetic checks catch misread amounts |
| Duplicate candidates are compared in Python after an indexed pre-filter (GSTIN, date, amount; account and dates) | Fine for thousands of documents | Revisit with a normalised invoice-number column if volume grows |
| Validation results become outdated if the rules change | Stored results keep their `rules_version` | **Run validation again** (or `POST /documents/{id}/validate`) re-checks without an LLM call |
| PaddleOCR 2.9 import takes ~20 s | The first OCR'd document after startup waits for the model | Loaded once per process, lazily in the worker thread, so app startup is not delayed |
| No user accounts exist yet on Computer A | Nobody can log in until the first one is created | `python -m tally_automation users create NAME --role admin` |
| The app is served over HTTP inside the tailnet | The session cookie has no `Secure` flag (WireGuard encrypts the link) | Kept deliberately (Phase 10): `tailscale serve` would add HTTPS but make every request appear to come from Computer A, losing device addresses in the allow-list and audit trail |
| Backups go to `data\backups` unless `BACKUP_DIR` is set | A disk failure would take data and backups together | Set `BACKUP_DIR` to another drive or disk (the check warns) |
| Records made before Phase 9 have no user | History shows the device instead | — |
| Computer B is not on the tailnet yet | Uploads only from Computer A | Join it to the tailnet (account `shivamacc1593@`) |
| The Tally host's license server (port 9999) answers anyone on the tailnet, including the license serial number | Low risk inside a private tailnet | Windows Firewall on `desktop-a0fljal`: allow 9000 only from `100.111.30.101` and block 9999 from the tailnet if not needed |
| TallyPrime must be running with the company loaded | When it is closed, status shows *Unreachable* and retrieval is impossible | Open TallyPrime and the company, then **Check connection** |
| 3 ledger GSTINs in TallyPrime fail the check character | Invoices cannot be matched to those suppliers by GSTIN | Correct them in TallyPrime; the *GSTIN fails check* filter lists them |
| Some bank ledger names contain the full account number | The name appears unmasked on the TallyPrime and Generate pages (names cannot be masked without losing which ledger it is) | Rename the ledgers in TallyPrime (e.g. keep the last 4 digits) and enter the number in the ledger's bank details |
| Bill-wise payments are recorded *On Account* | Supplier outstanding is not reduced bill by bill | Adjust against bills in TallyPrime; bill matching may come later |
| No voucher can be generated for a supplier or ledger missing in TallyPrime | Masters are never created by this application | Create the ledger in TallyPrime, retrieve masters again, then generate |
| A company restored from a backup keeps the original's GUID | Retrieving the copy's masters replaces the real company's stored masters | After testing in a copy, retrieve the real company's masters again |
| A voucher changed or deleted in TallyPrime after import is not tracked | The application shows the state at import time | Make corrections in TallyPrime; the marker in the narration links it back |
| SQLite stores datetimes without an offset | Naive values would reach the browser and be misread as local time | `UtcDateTime` column type stores UTC, always returns UTC-aware values and refuses naive input |

## TallyPrime connectivity prerequisites (Phase 6)

1. Computer A, Computer B and the Tally host are on the same tailnet. Tailscale
   ACLs, if used, allow A → Tally host:9000 and B → A:`APP_PORT`.
2. TallyPrime 6.2: F1 Help → Settings → Connectivity → Client/Server configuration.
   Set TallyPrime to act as *Server* (or *Both*) and note the port (default 9000).
3. Windows Firewall on the Tally host allows that port **only** from `100.64.0.0/10`.
4. A company is loaded in TallyPrime for read-only testing.

## Phases

1. Foundation ✅
2. Upload & processing foundation ✅
3. PDF text & OCR ✅
4. AI extraction ✅
5. Duplicates & accounting validation ✅
6. Tally read-only ✅
7. Voucher generation ✅
8. Approval & controlled import ✅
9. Dashboard, security & networking ✅
10. End-to-end testing & deployment ✅
