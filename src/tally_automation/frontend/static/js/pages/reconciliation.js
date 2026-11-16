// Bank reconciliation: monthly and financial-year balances of one bank account, missing statements, and
// the account's TallyPrime bank ledger. Every figure is worked out by the backend from the reviewed
// statements; this page only displays it. Blank means the statements do not show the figure: it is never
// shown as zero.

import { api } from "../api.js";
import { clear, el, formatDate, formatDateTime, formatINR, showError, showMessage } from "../ui.js";

const $ = (id) => document.getElementById(id);
const state = { key: null, account: null, tab: "monthly", loaded: {} };

const STATUS = {
  matched: ["Matched", "ok"],
  mismatch: ["Mismatch", "error"],
  missing: ["Missing statement", "error"],
  partial: ["Partly covered", "warning"],
  incomplete: ["Figures incomplete", "neutral"],
  conflict: ["Statements disagree", "error"],
  unknown: ["Cannot compare", "neutral"],
};
const SOURCE = {
  statement_opening: "Printed opening balance",
  statement_closing: "Printed closing balance",
  row_balance: "Running balance printed on the statement",
  last_row_balance: "Balance after the last transaction (no closing printed)",
  derived_first_row: "Worked out from the first transaction (no opening printed)",
};
const VALIDATION = {
  passed: ["Passed", "ok"], passed_with_warnings: ["Warnings", "warning"], failed: ["Failed (unverified)", "error"],
};

const badge = (text, kind) => el("span", { className: `badge badge-${kind}`, text });
const statusBadge = (status) => badge(...(STATUS[status] ?? [status, "neutral"]));

function money(value) {
  return value === null || value === undefined ? "—" : formatINR(value);
}

function figureCell(figure) {
  const cell = el("td", { className: "num" }, [el("span", { text: money(figure.value) })]);
  const source = figure.value === null ? figure.note : (SOURCE[figure.source] ?? figure.source);
  if (source) cell.append(el("span", { className: "figure-source", text: source }));
  return cell;
}

function differenceCell(value) {
  const nonzero = value !== null && value !== undefined && Number(value) !== 0;
  return el("td", { className: `num${nonzero ? " diff-nonzero" : ""}`, text: money(value) });
}

function linkCell(link, previousLabel) {
  if (!link) return el("td", { text: "—" });
  const cell = el("td");
  if (link.status === "unknown") {
    cell.append(badge("Cannot compare", "neutral"),
      el("span", { className: "figure-source", text: link.previous_closing.value === null
        ? `${previousLabel} closing not known` : "Opening not known" }));
    return cell;
  }
  cell.append(statusBadge(link.status));
  const text = link.status === "matched"
    ? (link.same_statement ? "Same statement" : `Equals ${previousLabel.toLowerCase()} closing`)
    : `Previous closing ${money(link.previous_closing.value)}, difference ${money(link.difference)}`;
  cell.append(el("span", { className: "figure-source", text }));
  return cell;
}

function rangeText(r) {
  return r.from === r.to ? formatDate(r.from) : `${formatDate(r.from)} – ${formatDate(r.to)}`;
}

function detailRow(period, columns) {
  const items = [];
  if (period.document_ids.length) items.push(`Statements used: ${period.document_ids.map((id) => `document #${id}`).join(", ")}`);
  for (const r of period.missing_ranges) items.push(`Not covered by any statement: ${rangeText(r)}`);
  if (period.missing_months?.length) items.push(`Months without a statement: ${period.missing_months.map(monthText).join(", ")}`);
  for (const b of period.breaks) {
    items.push(`Balance jump on ${formatDate(b.on)}: document #${b.previous_document_id} closes at ${money(b.previous_closing)}, ` +
      `document #${b.next_document_id} opens at ${money(b.next_opening)}` +
      (b.status === "mismatch" ? ` (difference ${money(b.difference)})` : " (cannot compare)"));
  }
  for (const c of period.conflicts) items.push(`Documents #${c.document_ids.join(" and #")} (${rangeText(c)}): ${c.reason}`);
  if (period.unverified) items.push("Includes a statement whose validation failed: figures are unverified.");
  if (period.inferred) items.push("Includes a statement whose period was taken from its transaction dates.");
  for (const n of period.notes) items.push(n);
  for (const s of period.segments ?? []) {
    items.push(`Document #${s.document_id}, ${formatDate(s.from)} – ${formatDate(s.to)}: opening ${money(s.opening.value)}, ` +
      `credits ${money(s.total_credits)}, debits ${money(s.total_debits)}, closing ${money(s.closing.value)} ` +
      `(${s.transaction_count} transaction${s.transaction_count === 1 ? "" : "s"})`);
  }
  const list = el("ul", {}, [...new Set(items)].map((t) => el("li", { text: t })));
  return el("tr", { className: "recon-detail", attrs: { hidden: "" } }, [el("td", { attrs: { colspan: String(columns) } }, [list])]);
}

function periodRows(period, label, previousLabel) {
  const detail = detailRow(period, 10);
  const toggle = el("button", { className: "btn btn-secondary btn-small", text: "Details",
    attrs: { type: "button", "aria-expanded": "false" } });
  toggle.addEventListener("click", () => {
    detail.hidden = !detail.hidden;
    toggle.setAttribute("aria-expanded", String(!detail.hidden));
  });
  const row = el("tr", {}, [
    el("th", { text: label, attrs: { scope: "row" } }),
    figureCell(period.opening),
    el("td", { className: "num", text: money(period.total_credits) }),
    el("td", { className: "num", text: money(period.total_debits) }),
    el("td", { className: "num", text: money(period.calculated_closing) }),
    figureCell(period.closing),
    differenceCell(period.difference),
    linkCell(period.previous, previousLabel),
    el("td", {}, [statusBadge(period.status)]),
    el("td", {}, [toggle]),
  ]);
  return [row, detail];
}

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthText = (label) => `${MONTH_NAMES[Number(label.slice(5, 7)) - 1]} ${label.slice(0, 4)}`;

// ---- Accounts --------------------------------------------------------------------------------

async function loadAccounts() {
  const select = $("account-select");
  showMessage($("page-message"), "loading", "Loading bank accounts…");
  try {
    const data = await api.get("/reconciliation/accounts");
    clear($("page-message"));
    clear(select);
    const unassigned = $("unassigned");
    clear(unassigned);
    if (data.unassigned.length) {
      unassigned.append(el("p", { className: "alert alert-warning", text:
        `${data.unassigned.length} statement(s) show no account number and are not reconciled: ` +
        data.unassigned.map((s) => `#${s.document_id} “${s.filename}”`).join(", ") +
        ". Enter the account number on the Review page." }));
    }
    if (!data.accounts.length) {
      showMessage($("page-message"), "info", "No processed bank statements yet. Upload and review statements first.");
      for (const id of ["account-card", "summary-card", "views-card"]) $(id).hidden = true;
      return;
    }
    for (const a of data.accounts) {
      const label = `${a.account_number}${a.bank_names.length ? ` — ${a.bank_names.join(", ")}` : ""} (${a.statement_count} statement${a.statement_count === 1 ? "" : "s"})`;
      select.append(el("option", { text: label, attrs: { value: a.key } }));
    }
    const wanted = new URLSearchParams(window.location.search).get("account");
    state.key = data.accounts.some((a) => a.key === wanted) ? wanted : (data.accounts.some((a) => a.key === state.key) ? state.key : data.accounts[0].key);
    select.value = state.key;
    await loadAccount();
  } catch (error) {
    showError($("page-message"), error);
  }
}

async function loadAccount() {
  state.loaded = {};
  try {
    const account = await api.get(`/reconciliation/accounts/${state.key}`);
    state.account = account;
    renderAccount(account);
    renderStatements(account);
    setDefaultRanges(account);
    for (const id of ["account-card", "summary-card", "views-card"]) $(id).hidden = false;
    await loadTab();
  } catch (error) {
    showError($("page-message"), error);
  }
}

function renderAccount(a) {
  const list = $("account-details");
  clear(list);
  const add = (term, value) => list.append(el("dt", { text: term }), el("dd", { text: value }));
  add("Account number", a.account_number);
  add("Bank", a.bank_names.join(", ") || "—");
  add("Statements", `${a.statement_count}${a.unverified_count ? ` (${a.unverified_count} unverified)` : ""}` +
    `${a.unusable_count ? `, ${a.unusable_count} without a usable period` : ""}`);
  add("Covered", a.first_day ? `${formatDate(a.first_day)} – ${formatDate(a.last_day)} (with any gaps listed under Missing statements)` : "—");
  renderLedger(a);
}

// ---- Ledger link -------------------------------------------------------------------------------

function renderLedger(a) {
  const current = $("ledger-current");
  clear(current);
  if (a.ledger) {
    current.append(el("p", {}, [
      badge("Linked", "ok"), el("span", { text: ` ${a.ledger.ledger_name}` }),
      el("span", { className: "muted small", text: ` — ${a.ledger.company_name ?? a.ledger.company_guid}, since ${formatDateTime(a.ledger.linked_at)}` }),
    ]));
  } else {
    current.append(el("p", {}, [badge("Not linked", "neutral"), el("span", { className: "muted small", text: " Choose the bank ledger below." })]));
  }
  $("ledger-unlink").hidden = !a.ledger;
  const suggestions = $("ledger-suggestions");
  clear(suggestions);
  const offered = (a.suggestions ?? []).filter((s) => !a.ledger || s.ledger_guid !== a.ledger.ledger_guid);
  if (offered.length) {
    suggestions.append(el("p", { className: "small muted", text: "Suggested:" }));
    const wrap = el("div", { className: "suggestion-list" });
    for (const s of offered.slice(0, 5)) {
      const button = el("button", { className: "btn btn-secondary btn-small", text: `${s.ledger_name} (${s.company_name})`,
        attrs: { type: "button", title: s.reason } });
      button.addEventListener("click", async () => {
        $("ledger-company").value = s.company_guid;
        await loadLedgers(s.ledger_guid);
      });
      wrap.append(button);
    }
    suggestions.append(wrap);
  }
  loadCompanies(a);
}

async function loadCompanies(a) {
  const select = $("ledger-company");
  clear(select);
  try {
    const companies = await api.get("/tally/companies");
    if (!companies.length) {
      select.append(el("option", { text: "No retrieved masters", attrs: { value: "" } }));
      clear($("ledger-select"));
      return;
    }
    for (const c of companies) select.append(el("option", { text: c.name, attrs: { value: c.guid } }));
    const preferred = a.ledger?.company_guid ?? a.suggestions?.[0]?.company_guid;
    if (preferred && companies.some((c) => c.guid === preferred)) select.value = preferred;
    await loadLedgers(a.ledger?.ledger_guid ?? a.suggestions?.[0]?.ledger_guid);
  } catch (error) {
    showError($("ledger-message"), error);
  }
}

async function loadLedgers(selected) {
  const select = $("ledger-select");
  clear(select);
  const company = $("ledger-company").value;
  if (!company) return;
  try {
    const ledgers = await api.get(`/reconciliation/companies/${encodeURIComponent(company)}/bank-ledgers`);
    select.append(el("option", { text: ledgers.length ? "Choose a bank ledger…" : "No bank ledgers", attrs: { value: "" } }));
    for (const l of ledgers) {
      const extra = l.account_number ? ` (${l.account_number})` : "";
      select.append(el("option", { text: `${l.name}${extra}`, attrs: { value: l.guid } }));
    }
    if (selected && ledgers.some((l) => l.guid === selected)) select.value = selected;
  } catch (error) {
    showError($("ledger-message"), error);
  }
}

async function saveLink(ledgerGuid) {
  const message = $("ledger-message");
  const company = $("ledger-company").value;
  if (ledgerGuid !== null && !ledgerGuid) {
    showMessage(message, "warning", "Choose a bank ledger first.");
    return;
  }
  showMessage(message, "loading", ledgerGuid ? "Linking…" : "Unlinking…");
  try {
    await api.post(`/reconciliation/accounts/${state.key}/ledger`, {
      company_guid: ledgerGuid ? company : null, ledger_guid: ledgerGuid, note: $("ledger-note").value.trim() || null,
    });
    $("ledger-note").value = "";
    showMessage(message, "success", ledgerGuid ? "Ledger linked. The change is recorded in the history." : "Ledger unlinked.");
    const account = await api.get(`/reconciliation/accounts/${state.key}`);
    state.account = account;
    renderLedger(account);
    renderStatements(account);
  } catch (error) {
    showError(message, error);
  }
}

// ---- Views --------------------------------------------------------------------------------------

function setDefaultRanges(a) {
  const first = a.first_day ? a.first_day.slice(0, 7) : "";
  const last = a.last_day ? a.last_day.slice(0, 7) : "";
  $("month-from").value = first;
  $("month-to").value = last;
  const fy = (iso) => { const y = Number(iso.slice(0, 4)); return Number(iso.slice(5, 7)) >= 4 ? y : y - 1; };
  for (const id of ["fy-from", "fy-to"]) clear($(id));
  if (!a.first_day) return;
  const firstFy = fy(a.first_day) - 1;
  const lastFy = fy(a.last_day) + 1;
  for (let y = firstFy; y <= lastFy; y += 1) {
    const label = `${y}-${String((y + 1) % 100).padStart(2, "0")}`;
    for (const id of ["fy-from", "fy-to"]) $(id).append(el("option", { text: label, attrs: { value: label } }));
  }
  $("fy-from").value = `${fy(a.first_day)}-${String((fy(a.first_day) + 1) % 100).padStart(2, "0")}`;
  $("fy-to").value = `${fy(a.last_day)}-${String((fy(a.last_day) + 1) % 100).padStart(2, "0")}`;
}

function renderSummary(summary, unit) {
  const grid = $("summary");
  clear(grid);
  const add = (term, value) => grid.append(el("div", { className: "stat" }, [el("dt", { text: term }), el("dd", { text: String(value ?? 0) })]));
  add(`${unit} matched`, summary.matched);
  add(`${unit} with a mismatch`, summary.mismatch);
  add("Opening ≠ previous closing", summary.opening_mismatches);
  add(`${unit} missing`, summary.missing);
  add(`${unit} partly covered`, summary.partial);
  add("Incomplete or in conflict", (summary.incomplete ?? 0) + (summary.conflict ?? 0));
}

async function loadMonthly() {
  const message = $("monthly-message");
  const body = $("monthly-body");
  const from = $("month-from").value;
  const to = $("month-to").value;
  if (!from || !to) { clear(body); return; }
  showMessage(message, "loading", "Working out monthly balances…");
  try {
    const data = await api.get(`/reconciliation/accounts/${state.key}/monthly?from=${from}&to=${to}`);
    clear(message);
    clear(body);
    for (const p of data.periods) body.append(...periodRows(p, monthText(p.label), "Previous month"));
    renderSummary(data.summary, "Months");
  } catch (error) {
    clear(body);
    showError(message, error);
  }
}

async function loadYearly() {
  const message = $("yearly-message");
  const body = $("yearly-body");
  const from = $("fy-from").value;
  const to = $("fy-to").value;
  if (!from || !to) { clear(body); return; }
  showMessage(message, "loading", "Working out financial-year balances…");
  try {
    const data = await api.get(`/reconciliation/accounts/${state.key}/yearly?fy_from=${from}&fy_to=${to}`);
    clear(message);
    clear(body);
    for (const p of data.periods) body.append(...periodRows(p, `FY ${p.label}`, "Previous year"));
    renderSummary(data.summary, "Years");
  } catch (error) {
    clear(body);
    showError(message, error);
  }
}

async function loadMissing() {
  const message = $("missing-message");
  const content = $("missing-content");
  showMessage(message, "loading", "Looking for missing statements…");
  try {
    const c = await api.get(`/reconciliation/accounts/${state.key}/coverage`);
    clear(message);
    clear(content);
    if (!c.from) {
      content.append(el("p", { className: "muted", text: "No statement of this account can be placed in time." }));
    } else {
      content.append(el("p", { className: "small muted", text: `The statements run from ${formatDate(c.from)} to ${formatDate(c.to)}. ` +
        "Months and financial years are judged in full, so days before the first and after the last statement count as " +
        "missing there. Missing periods are not given any balance." }));
      const section = (title, items, render, empty) => {
        content.append(el("h3", { className: "small-heading", text: title }));
        content.append(items.length ? el("ul", { className: "plain-list" }, items.map((i) => el("li", { text: render(i) })))
          : el("p", { className: "muted small", text: empty }));
      };
      section(`Gaps between ${formatDate(c.from)} and ${formatDate(c.to)}`, c.missing,
        (r) => `${rangeText(r)} (${r.days} day${r.days === 1 ? "" : "s"})`, "None: the statements follow on from each other without a gap.");
      section("Months without any statement", c.missing_months, monthText, "None.");
      section("Months covered only in part", c.partial_months, (m) => `${monthText(m.month)}: ${m.missing_days} day(s) missing (${m.missing.map(rangeText).join(", ")})`, "None.");
      section("Financial years without any statement", c.missing_years, (y) => `FY ${y}`, "None.");
      section("Financial years covered only in part", c.partial_years, (y) => `FY ${y.year}: ${y.missing_days} day(s) missing`, "None.");
    }
    if (c.unusable_statements.length) {
      content.append(el("p", { className: "alert alert-warning", text: "Statements that cannot be placed in time: " +
        c.unusable_statements.map((s) => `#${s.document_id} “${s.filename}” — ${s.notes.join(" ")}`).join("; ") }));
    }
  } catch (error) {
    showError(message, error);
  }
}

function renderStatements(a) {
  const body = $("statements-body");
  clear(body);
  for (const s of a.statements) {
    const [vText, vKind] = VALIDATION[s.validation] ?? ["Not validated", "neutral"];
    const period = s.period_from ? `${formatDate(s.period_from)} – ${formatDate(s.period_to)}${s.period_inferred ? " (from transaction dates)" : ""}` : "Unknown";
    body.append(el("tr", {}, [
      el("td", {}, [el("a", { text: `#${s.document_id} ${s.filename}`, attrs: { href: `/review?document=${s.document_id}` } })]),
      el("td", { text: period }),
      figureCell(s.opening),
      figureCell(s.closing),
      el("td", { className: "num", text: String(s.transaction_count) }),
      el("td", {}, [badge(vText, vKind)]),
      el("td", { className: "details", text: s.notes.join(" ") || "—" }),
    ]));
  }
  const events = $("events");
  clear(events);
  if (!a.events.length) events.append(el("li", { className: "muted", text: "No changes yet." }));
  for (const e of a.events) {
    const what = e.kind === "ledger_linked" ? `Linked to ledger “${e.detail.ledger_name}”` : "Ledger unlinked";
    events.append(el("li", { text: `${formatDateTime(e.at)} — ${what} by ${e.user ?? "unknown"}${e.note ? ` — “${e.note}”` : ""}` }));
  }
  if (a.overlaps.some((o) => !o.agree)) {
    showMessage($("page-message"), "warning", "Some statements of this account overlap and disagree: the months concerned are shown as “Statements disagree”.");
  }
}

async function loadTab() {
  if (state.tab === "monthly") await loadMonthly();
  else if (state.tab === "yearly") await loadYearly();
  else if (state.tab === "missing") await loadMissing();
}

function selectTab(tab) {
  state.tab = tab;
  for (const name of ["monthly", "yearly", "missing", "statements"]) {
    $(`tab-${name}`).setAttribute("aria-selected", String(name === tab));
    $(`panel-${name}`).hidden = name !== tab;
  }
  loadTab();
}

// ---- Wiring -------------------------------------------------------------------------------------

$("account-select").addEventListener("change", (e) => { state.key = e.target.value; loadAccount(); });
$("reload").addEventListener("click", loadAccounts);
$("ledger-company").addEventListener("change", () => loadLedgers());
$("ledger-form").addEventListener("submit", (e) => { e.preventDefault(); saveLink($("ledger-select").value); });
$("ledger-unlink").addEventListener("click", () => saveLink(null));
$("monthly-form").addEventListener("submit", (e) => { e.preventDefault(); loadMonthly(); });
$("yearly-form").addEventListener("submit", (e) => { e.preventDefault(); loadYearly(); });
for (const name of ["monthly", "yearly", "missing", "statements"]) $(`tab-${name}`).addEventListener("click", () => selectTab(name));

loadAccounts();
