// TallyPrime integration (read-only): connection status, loaded companies,
// retrieval of masters, and browsing of the stored groups / ledgers / voucher
// types. All TallyPrime communication happens on the backend.

import { api } from "../api.js";
import {
  clear, el, formatDate, formatDateTime, setBadge, showError, showMessage, statusBadge,
} from "../ui.js";

const $ = (id) => document.getElementById(id);
const PAGE = 100;
const state = { company: null, offset: 0, total: 0, tab: "ledgers", searchTimer: null, defaultCompany: null };
const dash = "—";

// ---- Connection ----------------------------------------------------------------------

function renderStatus(s) {
  $("conn-host").textContent = s.configured ? `${s.host}:${s.port}` : "Not configured";
  if (!s.configured) setBadge($("conn-server"), "neutral", "Not configured");
  else if (s.server_running) setBadge($("conn-server"), "ok", "Running");
  else if (s.reachable) setBadge($("conn-server"), "warning", "Unexpected answer");
  else setBadge($("conn-server"), "error", "Unreachable");
  $("conn-time").textContent = s.milliseconds === null ? dash : `${s.milliseconds} ms`;
  setBadge($("conn-mode"), s.write_enabled ? "warning" : "ok",
    s.write_enabled ? "Read-only (TALLY_WRITE_ENABLED is on, unused before Phase 8)" : "Read-only");
  $("conn-detail").textContent = `${s.message}` + (s.checked_at ? ` · checked ${formatDateTime(s.checked_at)}` : "");
  $("connection-grid").hidden = false;
  state.defaultCompany = s.default_company;
  renderLoaded(s);
}

function renderLoaded(s) {
  const body = $("loaded-body");
  clear(body);
  if (!s.loaded_companies.length) {
    body.append(el("tr", {}, [el("td", {
      className: "muted", attrs: { colspan: "4" },
      text: s.server_running ? "No company is loaded in TallyPrime. Open one there, then check again." : "Not connected.",
    })]));
    return;
  }
  for (const c of s.loaded_companies) {
    const button = el("button", { className: "btn btn-small", text: "Retrieve masters", attrs: { type: "button" } });
    button.addEventListener("click", () => retrieve(c.name, button));
    const name = el("td", {}, [el("strong", { text: c.name })]);
    if (c.name === s.default_company) name.append(document.createTextNode(" "), el("span", { className: "badge badge-info", text: "default" }));
    body.append(el("tr", {}, [name, el("td", { text: formatDate(c.books_from) }), el("td", { text: c.state ?? dash }),
      el("td", {}, [button])]));
  }
}

async function checkConnection() {
  const button = $("check");
  button.disabled = true;
  showMessage($("connection-message"), "loading", "Contacting TallyPrime…");
  try {
    renderStatus(await api.get("/tally/status", { timeoutMs: 45000 }));
    clear($("connection-message"));
  } catch (error) {
    showError($("connection-message"), error);
  } finally {
    button.disabled = false;
  }
}

async function retrieve(company, button) {
  const ok = window.confirm(
    `Retrieve groups, ledgers and voucher types of “${company}” from TallyPrime?\n\n` +
    "This only reads from TallyPrime; nothing there is changed. The stored copy in this application is replaced.",
  );
  if (!ok) return;
  button.disabled = true;
  showMessage($("sync-message"), "loading", `Retrieving masters of “${company}”…`);
  try {
    const run = await api.post("/tally/sync", { company }, { timeoutMs: 300000 });
    showMessage($("sync-message"), "success",
      `Retrieved ${run.group_count} groups, ${run.ledger_count} ledgers and ${run.voucher_type_count} voucher types.` +
      (run.warnings.length ? ` Notes: ${run.warnings.join(" ")}` : ""));
    state.company = run.company_guid;
    await Promise.all([loadCompanies(), loadRuns()]);
  } catch (error) {
    showError($("sync-message"), error);
    loadRuns();
  } finally {
    button.disabled = false;
  }
}

// ---- Stored masters ------------------------------------------------------------------

async function loadCompanies() {
  const select = $("company-select");
  try {
    const companies = await api.get("/tally/companies");
    clear(select);
    if (!companies.length) {
      select.append(el("option", { text: "Nothing retrieved yet", attrs: { value: "" } }));
      $("masters-meta").textContent = "Retrieve a company's masters above to browse them here.";
      for (const id of ["ledgers-body", "groups-body", "vtypes-body"]) clear($(id));
      $("ledger-range").textContent = "";
      return;
    }
    for (const c of companies) select.append(el("option", { text: c.name, attrs: { value: c.guid } }));
    const preferred = companies.find((c) => c.guid === state.company)
      ?? companies.find((c) => c.name === state.defaultCompany) ?? companies[0];
    state.company = preferred.guid;
    select.value = preferred.guid;
    $("masters-meta").textContent =
      `${preferred.group_count} groups · ${preferred.ledger_count} ledgers · ${preferred.voucher_type_count} voucher types · ` +
      `retrieved ${formatDateTime(preferred.last_synced_at)}`;
    await loadTab();
  } catch (error) {
    showError($("masters-message"), error);
  }
}

const yesNo = (v) => (v === null ? dash : v ? "Yes" : "No");

function ledgerRow(l) {
  const name = el("td", {}, [el("strong", { text: l.name })]);
  if (l.aliases.length) name.append(el("div", { className: "small muted", text: `Alias: ${l.aliases.join(", ")}` }));
  const gstin = el("td", { text: l.gstin ?? dash });
  if (l.gstin_valid === false) gstin.append(el("span", { className: "tag tag-invalid", text: "Check character fails" }));
  const tax = l.gst_duty_head ? `${l.gst_duty_head}${l.tax_rate ? ` ${l.tax_rate}%` : ""}` : (l.tax_type && l.tax_type !== "Others" ? l.tax_type : dash);
  const bank = l.bank_account_number ? `${l.bank_account_number}${l.ifsc ? ` · ${l.ifsc}` : ""}` : dash;
  return el("tr", {}, [
    name,
    el("td", { text: l.parent ?? "Primary", attrs: { title: l.primary_group ? `Primary group: ${l.primary_group}` : "" } }),
    gstin,
    el("td", { text: [l.gst_registration_type, l.state].filter(Boolean).join(" · ") || dash }),
    el("td", { text: tax }),
    el("td", { text: bank }),
  ]);
}

async function loadLedgers() {
  const params = new URLSearchParams({ limit: String(PAGE), offset: String(state.offset) });
  const q = $("ledger-q").value.trim();
  const kind = $("ledger-kind").value;
  if (q) params.set("q", q);
  if (kind) params.set("kind", kind);
  const data = await api.get(`/tally/companies/${encodeURIComponent(state.company)}/ledgers?${params}`);
  state.total = data.total;
  const body = $("ledgers-body");
  clear(body);
  if (!data.items.length) body.append(el("tr", {}, [el("td", { className: "muted", text: "No ledgers match.", attrs: { colspan: "6" } })]));
  for (const l of data.items) body.append(ledgerRow(l));
  const end = Math.min(state.offset + data.items.length, data.total);
  $("ledger-range").textContent = data.total ? `${state.offset + 1}–${end} of ${data.total}` : "";
  $("ledger-prev").disabled = state.offset === 0;
  $("ledger-next").disabled = end >= data.total;
}

async function loadGroups() {
  const groups = await api.get(`/tally/companies/${encodeURIComponent(state.company)}/groups`);
  const body = $("groups-body");
  clear(body);
  for (const g of groups) {
    const nature = g.is_revenue === null ? dash
      : `${g.is_revenue ? "Income/expense" : "Balance sheet"}${g.is_deemed_positive === null ? "" : g.is_deemed_positive ? ", debit" : ", credit"}`;
    body.append(el("tr", {}, [
      el("td", { text: g.name + (g.aliases.length ? ` (${g.aliases.join(", ")})` : "") }),
      el("td", { text: g.parent ?? "Primary" }),
      el("td", { text: g.primary_group ?? dash }),
      el("td", { text: nature }),
    ]));
  }
}

async function loadVoucherTypes() {
  const types = await api.get(`/tally/companies/${encodeURIComponent(state.company)}/voucher-types`);
  const body = $("vtypes-body");
  clear(body);
  for (const v of types) {
    body.append(el("tr", {}, [
      el("td", { text: v.name }), el("td", { text: v.parent ?? dash }),
      el("td", { text: v.numbering_method ?? dash }), el("td", { text: yesNo(v.is_active) }),
    ]));
  }
}

async function loadTab() {
  if (!state.company) return;
  clear($("masters-message"));
  try {
    if (state.tab === "ledgers") await loadLedgers();
    else if (state.tab === "groups") await loadGroups();
    else await loadVoucherTypes();
  } catch (error) {
    showError($("masters-message"), error);
  }
}

function selectTab(tab) {
  state.tab = tab;
  for (const [name, button, panel] of [["ledgers", "tab-ledgers", "panel-ledgers"], ["groups", "tab-groups", "panel-groups"], ["vtypes", "tab-vtypes", "panel-vtypes"]]) {
    $(button).setAttribute("aria-selected", String(name === tab));
    $(panel).hidden = name !== tab;
  }
  loadTab();
}

// ---- History ---------------------------------------------------------------------------

async function loadRuns() {
  const body = $("runs-body");
  try {
    const runs = await api.get("/tally/sync-runs?limit=10");
    clear(body);
    if (!runs.length) body.append(el("tr", {}, [el("td", { className: "muted", text: "No retrievals yet.", attrs: { colspan: "7" } })]));
    for (const r of runs) {
      const notes = r.error_message ?? [
        ...r.warnings,
        r.removed_characters ? `${r.removed_characters} invalid control character(s) removed from Tally's XML.` : "",
      ].filter(Boolean).join(" ");
      body.append(el("tr", {}, [
        el("td", { text: formatDateTime(r.started_at) }), el("td", { text: r.company_name }),
        el("td", {}, [statusBadge(r.status)]), el("td", { text: r.group_count ?? dash }),
        el("td", { text: r.ledger_count ?? dash }), el("td", { text: r.voucher_type_count ?? dash }),
        el("td", { className: r.error_message ? "error-text small" : "small muted", text: notes || dash }),
      ]));
    }
  } catch (error) {
    clear(body);
    body.append(el("tr", {}, [el("td", { className: "error-text", text: error.message, attrs: { colspan: "7" } })]));
  }
}

// ---- Wiring ----------------------------------------------------------------------------

$("check").addEventListener("click", checkConnection);
$("company-select").addEventListener("change", (e) => { state.company = e.target.value; state.offset = 0; loadCompanies(); });
$("tab-ledgers").addEventListener("click", () => selectTab("ledgers"));
$("tab-groups").addEventListener("click", () => selectTab("groups"));
$("tab-vtypes").addEventListener("click", () => selectTab("vtypes"));
$("ledger-filters").addEventListener("submit", (e) => e.preventDefault());
$("ledger-kind").addEventListener("change", () => { state.offset = 0; loadTab(); });
$("ledger-q").addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => { state.offset = 0; loadTab(); }, 300);
});
$("ledger-prev").addEventListener("click", () => { state.offset = Math.max(0, state.offset - PAGE); loadTab(); });
$("ledger-next").addEventListener("click", () => { state.offset += PAGE; loadTab(); });

checkConnection();
loadCompanies();
loadRuns();
