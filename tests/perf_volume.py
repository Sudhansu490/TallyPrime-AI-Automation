"""Manual volume check (not collected by pytest): SYNTHETIC documents only.

    tally_env\\Scripts\\python tests\\perf_volume.py [invoices] [statements]

Builds a temporary database with N synthetic invoices (same supplier, different
numbers) and M two-page statements, each processed through the real worker with
a fake LLM, then times the endpoints the pages call. Prints timings only.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from auth_helpers import login_as  # noqa: E402
from extraction_fakes import (  # noqa: E402
    INVOICE_TEXT, STATEMENT_PAGE_1, STATEMENT_PAGE_2, FakeLlm, field, invoice_reply, statement_page1_reply,
    statement_page2_reply,
)
from tally_automation.api.main import create_app  # noqa: E402
from tally_automation.config import PROJECT_ROOT, Environment, Settings  # noqa: E402
from test_extraction_api import process, text_pdf, upload  # noqa: E402


def main(invoices: int, statements: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ta-perf-"))
    settings = Settings(_env_file=None, app_env=Environment.TEST, data_dir=tmp / "data",
                        database_url=f"sqlite:///{(tmp / 'perf.db').as_posix()}", processing_worker_enabled=False)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        login_as(client)
        started = time.perf_counter()
        for i in range(invoices):
            number = f"SYN-{i:05d}"
            upload(client, text_pdf(INVOICE_TEXT.replace("SYN-0001", number) + f"\nCopy {i}"))
            process(client, FakeLlm(invoice_reply(header={
                "invoice_number": field(number), "total_taxable_value": field("1,000.00"), "total_igst": field("180.00"),
            })))
        for i in range(statements):
            upload(client, text_pdf(STATEMENT_PAGE_1, STATEMENT_PAGE_2 + f"\nCopy {i}"), "bank_statement")
            process(client, FakeLlm(statement_page1_reply(), statement_page2_reply()), pages_per_call=1)
        print(f"built {invoices} invoices + {statements} statements in {time.perf_counter() - started:.1f} s")

        def timed(label: str, path: str, method: str = "GET") -> None:
            t = time.perf_counter()
            response = client.request(method, path)
            print(f"{label:<40} {response.status_code} {1000 * (time.perf_counter() - t):8.0f} ms")

        timed("dashboard stats", "/api/v1/documents/stats")
        timed("documents list (200)", "/api/v1/documents?limit=200")
        timed("processing list (50)", "/api/v1/documents?limit=50")
        timed("duplicates", "/api/v1/duplicates")
        timed("validation of last invoice", f"/api/v1/documents/{invoices}/validation")
        timed("re-validate last invoice", f"/api/v1/documents/{invoices}/validate", "POST")
        timed("voucher setup (invoice)", f"/api/v1/documents/{invoices}/voucher-setup")
        timed("vouchers list", "/api/v1/vouchers")
        timed("voucher stats", "/api/v1/vouchers/stats")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 300, int(sys.argv[2]) if len(sys.argv) > 2 else 30)
