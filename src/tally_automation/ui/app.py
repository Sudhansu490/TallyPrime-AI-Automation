"""Streamlit dashboard entry point.

Run with: streamlit run src/tally_automation/ui/app.py
The UI calls the backend API server-side; the browser never talks to FastAPI.
"""

from __future__ import annotations

import httpx
import streamlit as st

from tally_automation.config import get_settings

PLANNED_PAGES = [
    ("Upload Documents", "Phase 2"),
    ("Document Processing", "Phase 2–3"),
    ("Extracted Data Review", "Phase 4"),
    ("Duplicate Management", "Phase 5"),
    ("TallyPrime Integration", "Phase 6"),
    ("Voucher Review", "Phase 7–8"),
]


@st.cache_data(ttl=10, show_spinner=False)
def fetch_health(api_base_url: str) -> tuple[dict | None, str | None]:
    try:
        response = httpx.get(f"{api_base_url}/api/v1/health", timeout=5)
        return response.json(), None
    except httpx.HTTPError as exc:
        return None, f"Backend API unreachable ({type(exc).__name__})."
    except ValueError:
        return None, "Backend API returned an invalid response."


def main() -> None:
    settings = get_settings()
    st.set_page_config(page_title=settings.app_name, layout="wide")
    st.title(settings.app_name)
    st.caption("Phase 1 foundation. Processing features are added in later phases.")

    st.subheader("System status")
    health, error = fetch_health(settings.api_base_url)
    if error:
        st.error(error)
    else:
        col1, col2, col3 = st.columns(3)
        col1.metric("API", health["status"].upper())
        col2.metric("Database", health["database"]["status"].upper())
        col3.metric("Tally writes", "ENABLED" if health["tally_write_enabled"] else "DISABLED")
        st.caption(f"Version {health['version']} · environment: {health['environment']}")

    if st.button("Refresh status"):
        fetch_health.clear()
        st.rerun()

    st.subheader("Planned modules")
    # Plain markdown: st.table/st.dataframe need pyarrow, which currently cannot
    # load alongside PaddlePaddle's NumPy 1.x pin (see README, Known issues).
    st.markdown("\n".join(f"- **{name}** — {phase}" for name, phase in PLANNED_PAGES))


main()
