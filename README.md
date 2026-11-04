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
Computer B (browser) ──Tailscale──▶ Computer A: Streamlit :8501 ──loopback──▶ FastAPI 127.0.0.1:8000
                                                                    │
                                                                    └──Tailscale──▶ TallyPrime host :9000
```

- Only Streamlit listens on a network interface, and only on Computer A's
  Tailscale IP. FastAPI stays on `127.0.0.1`.
- No port is ever opened to the public internet.

## Layout

```
src/tally_automation/
  config.py          settings from environment / .env
  logging_config.py  JSON logs with redaction of sensitive fields
  security.py        password hashing (stdlib scrypt)
  api/               FastAPI app factory, dependencies, routes
  db/                SQLAlchemy base, session/engine, models
  ui/                Streamlit dashboard
migrations/          Alembic environment and revisions
tests/               pytest suite (synthetic data only)
data/                runtime data: database, uploads, logs (gitignored)
```

Modules for documents, OCR, extraction, validation, duplicates and Tally are
added in their respective phases.

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

API (Computer A, loopback only):

```powershell
uvicorn tally_automation.api.main:build_app --factory --host 127.0.0.1 --port 8000
```

Dashboard, local only (development):

```powershell
streamlit run src/tally_automation/ui/app.py --server.address 127.0.0.1 --server.port 8501
```

Dashboard for Computer B (only once dashboard authentication exists, Phase 9):
replace `127.0.0.1` with Computer A's Tailscale IP (`tailscale ip -4`).

Health check: `GET http://127.0.0.1:8000/api/v1/health`.
Interactive API docs at `/docs` are disabled when `APP_ENV=production`.

## Tests

```powershell
pytest
```

Tests use a temporary SQLite database per test, ignore `.env`, and never need
TallyPrime. Test data is synthetic and labelled as such.

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

## Known issues

| Issue | Effect | Resolution |
|---|---|---|
| `paddlepaddle 2.6.2` needs `protobuf<=3.20.2`; Streamlit declares `>=5.26` | Without the constraint, pip upgrades protobuf and Paddle fails to import | `constraints.txt` pins 3.20.2 (Streamlit verified to run with it) |
| `pyarrow 26` needs NumPy ≥2; Paddle 2.6.2 needs NumPy <2 | `st.table` / `st.dataframe` fail in the shared venv | Decide before Phase 2: separate UI venv, or move OCR to PaddleOCR 3.x |
| `opencv-python`, `opencv-contrib-python`, `opencv-python-headless` all installed | They overwrite the same `cv2` module | Reduce to one in Phase 3 |
| Tailscale on Computer A is logged out | No tailnet access to Computer B or the Tally host | Run `tailscale up` |
| SQLite `DateTime(timezone=True)` | SQLite stores no offset; values are written in UTC | Treat stored times as UTC |

## TallyPrime connectivity prerequisites (Phase 6)

1. Computer A, Computer B and the Tally host are on the same tailnet.
2. TallyPrime 6.2: F1 → Settings → Connectivity → Client/Server configuration,
   TallyPrime acts as *Server* (or *Both*), port noted (default 9000).
3. Windows Firewall on the Tally host allows that port **only** from the
   Tailscale range (100.64.0.0/10).
4. A company name for read-only testing.

## Phases

1. Foundation ✅ · 2. Upload & processing foundation · 3. PDF text & OCR ·
4. AI extraction · 5. Duplicates & accounting validation · 6. Tally read-only ·
7. Voucher generation · 8. Approval & controlled import · 9. Dashboard,
security & networking · 10. End-to-end testing & deployment
