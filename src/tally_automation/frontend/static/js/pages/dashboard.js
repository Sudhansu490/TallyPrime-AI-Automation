// Dashboard page: backend health and document statistics.

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

  setBadge(
    document.getElementById("health-llm"),
    health.llm_provider ? "ok" : "neutral",
    health.llm_provider ? `${health.llm_provider} · ${health.llm_model}` : "Not configured",
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

async function loadStats() {
  const message = document.getElementById("stats-message");
  const grid = document.getElementById("stats-grid");
  try {
    const stats = await api.get("/documents/stats");
    const show = (id, value) => { document.getElementById(id).textContent = value === null ? "—" : String(value); };
    show("stat-total", stats.total);
    show("stat-queued", stats.queued + stats.processing);
    show("stat-text-extracted", stats.text_extracted);
    show("stat-processed", stats.processed);
    show("stat-failed", stats.failed);
    show("stat-duplicates", stats.duplicates);
    show("stat-possible-duplicates", stats.possible_duplicates);
    show("stat-validation-failed", stats.validation_failed);
    show("stat-ready", stats.ready_for_vouchers);
    show("stat-approvals", stats.pending_approvals); // null until Phase 8: shown as "—", never as 0
    clear(message);
    grid.hidden = false;
  } catch (error) {
    grid.hidden = true;
    showError(message, error);
  }
}

function refreshAll() {
  loadHealth();
  loadStats();
}

document.getElementById("refresh-health").addEventListener("click", refreshAll);
refreshAll();
