// Dashboard page: shows backend health. Document statistics arrive in Phase 2.

import { ApiError, api } from "../api.js";
import { formatDateTime, setBadge, showError, showMessage, clear } from "../ui.js";

const STATUS_STATE = { ok: "ok", degraded: "warning", error: "error", not_configured: "neutral" };

function render(health) {
  const grid = document.getElementById("health-grid");
  setBadge(document.getElementById("health-app"), STATUS_STATE[health.status] ?? "error", health.status.toUpperCase());

  const db = health.database;
  setBadge(
    document.getElementById("health-database"),
    STATUS_STATE[db.status] ?? "error",
    db.detail ? `${db.status.toUpperCase()}: ${db.detail}` : db.status.toUpperCase(),
  );

  setBadge(
    document.getElementById("health-tally"),
    "neutral",
    health.tally_configured ? "Configured (not tested)" : "Not configured",
  );
  setBadge(
    document.getElementById("health-tally-writes"),
    health.tally_write_enabled ? "warning" : "ok",
    health.tally_write_enabled ? "ENABLED" : "Disabled",
  );

  document.getElementById("health-meta").textContent =
    `Version ${health.version} · environment: ${health.environment} · checked ${formatDateTime(new Date().toISOString())}`;
  grid.hidden = false;
}

async function loadHealth() {
  const message = document.getElementById("health-message");
  const button = document.getElementById("refresh-health");
  button.disabled = true;
  showMessage(message, "loading", "Checking system status…");
  try {
    render(await api.get("/health"));
    clear(message);
  } catch (error) {
    // A degraded backend answers 503 with a full health body: show it too.
    if (error instanceof ApiError && error.status === 503 && error.body && error.body.database) {
      render(error.body);
      showMessage(message, "warning", "The application is running with problems. See the details below.");
    } else {
      document.getElementById("health-grid").hidden = true;
      showError(message, error);
    }
  } finally {
    button.disabled = false;
  }
}

document.getElementById("refresh-health").addEventListener("click", loadHealth);
loadHealth();
