// Bank reconciliation: monthly and financial-year balances of one bank account, missing statements, and
// the account's TallyPrime bank ledger. Every figure is worked out by the backend from the reviewed
// statements; this page only displays it. Blank means the statements do not show the figure: it is never
// shown as zero.

import { api } from "../api.js";
import { clear, el, formatDate, formatDateTime, formatINR, showError, showMessage } from "../ui.js";

const $ = (id) => document.getElementById(id);
const state = { key: null, account: null, tab: "monthly", loaded: {}, run: null, runId: null };

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
    setTransactionDefaults(account);
    state.run = null;
    state.runId = null;
    $("txn-result").hidden = true;
    clear($("txn-message"));
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
    events.append(el("li", { text: `${formatDateTime(e.at)} — ${eventText(e)} by ${e.user ?? "unknown"}${e.note ? ` — “${e.note}”` : ""}` }));
  }
  if (a.overlaps.some((o) => !o.agree)) {
    showMessage($("page-message"), "warning", "Some statements of this account overlap and disagree: the months concerned are shown as “Statements disagree”.");
  }
  loadSavedRuns();
}

function eventText(e) {
  const d = e.detail ?? {};
  const pair = () => [...(d.bank ?? []).map((b) => `statement #${b[0]}/${b[1]} ${money(b[3])}`),
    ...(d.tally ?? []).map((t) => `${t[1] ?? "voucher"} ${t[2] ?? ""} ${money(t[3])}`)].join(" ↔ ");
  switch (e.kind) {
    case "ledger_linked": return `Linked to ledger “${d.ledger_name}”`;
    case "ledger_unlinked": return "Ledger unlinked";
    case "run_saved": return `Saved “${d.label}”`;
    case "report_downloaded": return `Downloaded ${String(d.format).toUpperCase()} of “${d.label}”`;
    case "match_approved": return `Approved match: ${pair()}`;
    case "match_rejected": return `Rejected match: ${pair()}`;
    case "match_reopened": return `Reopened match: ${pair()}`;
    default: return e.kind;
  }
}

async function loadSavedRuns() {
  const list = $("saved-runs");
  const select = $("run-select");
  try {
    const runs = await api.get(`/reconciliation/accounts/${state.key}/runs`);
    clear(list);
    clear(select);
    if (!runs.length) list.append(el("li", { className: "muted", text: "None saved yet." }));
    for (const r of runs) {
      const item = el("li", { text: `#${r.id} ${r.label} — saved ${formatDateTime(r.created_at)} by ${r.created_by ?? "unknown"}` +
        `${r.stale ? " (statements corrected since)" : ""} ` });
      item.append(el("a", { text: "Excel", attrs: { href: reportUrl(r.id, "xlsx") } }), el("span", { text: " · " }),
        el("a", { text: "PDF", attrs: { href: reportUrl(r.id, "pdf") } }));
      list.append(item);
    }
    const comparisons = runs.filter((r) => r.kind === "transactions");
    select.append(el("option", { text: comparisons.length ? "Choose a saved comparison…" : "None saved yet", attrs: { value: "" } }));
    for (const r of comparisons) {
      select.append(el("option", { text: `#${r.id} ${r.label}${r.stale ? " (out of date)" : ""}`, attrs: { value: String(r.id) } }));
    }
    if (state.runId && comparisons.some((r) => r.id === state.runId)) select.value = String(state.runId);
  } catch (error) {
    showError($("txn-message"), error);
  }
}

// ---- Saved reports -------------------------------------------------------------------------------

const reportUrl = (id, format) => `/api/v1/reconciliation/runs/${id}/report.${format}`;

async function saveAndDownload(kind, format) {
  const message = $(`${kind}-message`);
  const body = kind === "monthly"
    ? { kind, month_from: $("month-from").value, month_to: $("month-to").value }
    : { kind, fy_from: $("fy-from").value, fy_to: $("fy-to").value };
  showMessage(message, "loading", "Saving the reconciliation…");
  try {
    const run = await api.post(`/reconciliation/accounts/${state.key}/runs`, body);
    showMessage(message, "success", `Saved as reconciliation #${run.id}. The ${format === "xlsx" ? "Excel" : "PDF"} file is downloading.`);
    window.location.href = reportUrl(run.id, format);
    loadSavedRuns();
  } catch (error) {
    showError(message, error);
  }
}

// ---- Transactions with TallyPrime ----------------------------------------------------------------

const MATCH_STATUS = {
  matched: ["Matched", "ok"], uncertain: ["Uncertain", "warning"], partial: ["Partly matched", "warning"],
  duplicate: ["Duplicate in TallyPrime", "error"], bank_only: ["Only in the statement", "error"],
  tally_only: ["Only in TallyPrime", "error"], not_compared: ["Not compared", "neutral"],
};

async function runTransactions() {
  const message = $("txn-message");
  const from = $("txn-from").value;
  const to = $("txn-to").value;
  if (!from || !to) { showMessage(message, "warning", "Choose the period first."); return; }
  $("txn-run").disabled = true;
  showMessage(message, "loading", "Reading the period from TallyPrime (read only) and comparing… this can take a minute.");
  try {
    const run = await api.post(`/reconciliation/accounts/${state.key}/runs`,
      { kind: "transactions", date_from: from, date_to: to, window_days: Number($("txn-window").value) }, { timeoutMs: 600000 });
    state.runId = run.id;
    showMessage(message, "success", `Compared and saved as #${run.id}: ${run.result.bank_transactions} statement transaction(s), ` +
      `${run.result.tally_entries} TallyPrime entr${run.result.tally_entries === 1 ? "y" : "ies"} on the ledger.`);
    renderRun(run);
    loadSavedRuns();
  } catch (error) {
    showError(message, error);
  } finally {
    $("txn-run").disabled = false;
  }
}

async function openRun(id) {
  if (!id) { $("txn-result").hidden = true; for (const k of ["run-xlsx", "run-pdf"]) $(k).hidden = true; return; }
  try {
    const run = await api.get(`/reconciliation/runs/${id}`);
    state.runId = run.id;
    clear($("txn-message"));
    renderRun(run);
  } catch (error) {
    showError($("txn-message"), error);
  }
}

function renderRun(run) {
  state.run = run;
  $("txn-result").hidden = false;
  $("run-xlsx").href = reportUrl(run.id, "xlsx");
  $("run-pdf").href = reportUrl(run.id, "pdf");
  $("run-xlsx").hidden = $("run-pdf").hidden = false;
  const r = run.result;
  if (run.stale) {
    showMessage($("txn-message"), "warning", "The statements have been corrected since this comparison was saved: compare again for current figures.");
  }
  const counts = $("txn-counts");
  clear(counts);
  const add = (term, value) => counts.append(el("div", { className: "stat" }, [el("dt", { text: term }), el("dd", { text: String(value) })]));
  add("Matched", r.counts.matched + r.counts.approved);
  add("Awaiting approval", r.counts.awaiting_approval);
  add("Only in the statement", r.counts.bank_only);
  add("Only in TallyPrime", r.counts.tally_only);
  add("Duplicates in TallyPrime", r.counts.duplicate);
  add("Not compared", r.counts.excluded + r.counts.unreadable);

  const st = r.statement;
  const brs = $("brs-body");
  clear(brs);
  const line = (label, value, strong) => brs.append(el("tr", {}, [
    el(strong ? "th" : "td", { text: label, attrs: strong ? { scope: "row" } : {} }),
    el("td", { className: `num${strong && value !== null && Number(value) !== 0 && label.startsWith("Unexplained") ? " diff-nonzero" : ""}`, text: money(value) }),
  ]));
  line(`Balance per TallyPrime on ${formatDate(r.from)} (start of day)`, st.tally_opening);
  line(`Balance per statement on ${formatDate(r.from)} (start of day)`, st.bank_opening);
  line("Difference brought forward", st.brought_forward);
  line(`Balance per TallyPrime on ${formatDate(r.to)}`, st.tally_closing, true);
  line("+ Transactions only in the statement", st.bank_only_total);
  line("− Entries only in TallyPrime (incl. duplicates)", st.tally_only_total);
  line("+ Differences inside matches", st.match_differences);
  line("= Expected balance per statement", st.expected_bank_closing, true);
  line(`Balance per statement on ${formatDate(r.to)}`, st.bank_closing, true);
  line("Unexplained difference", st.unexplained, true);
  for (const note of [...(r.tally_notes ?? []),
    ...(r.balances.bank_opening.value === null ? [`Statement opening: ${r.balances.bank_opening.note}`] : []),
    ...(r.balances.bank_closing.value === null ? [`Statement closing: ${r.balances.bank_closing.note}`] : [])]) {
    brs.append(el("tr", {}, [el("td", { className: "muted small", text: note, attrs: { colspan: "2" } })]));
  }
  if (st.awaiting_approval) {
    brs.append(el("tr", {}, [el("td", { className: "muted small", attrs: { colspan: "2" },
      text: `${st.awaiting_approval} proposed match(es) await approval; they count as matched until rejected.` })]));
  }
  renderTransactions();
}

function sideCells(items, kind) {
  if (!items.length) return [el("td", { text: "—" }), el("td", { className: "num", text: "—" })];
  const lines = items.map((i) => kind === "bank"
    ? `${formatDate(i.date)} · #${i.document_id}/${i.row} · ${i.description ?? ""}`
    : `${formatDate(i.date)} · ${[i.voucher_type, i.number].filter(Boolean).join(" ")} · ${i.narration ?? ""}`);
  const total = items.reduce((sum, i) => sum + Number(i.signed), 0);
  return [el("td", { className: "details" }, lines.map((t) => el("div", { text: t }))),
    el("td", { className: "num", text: items.length === 1 ? money(items[0].signed) : formatINR(total.toFixed(2)) })];
}

function transactionRows() {
  const r = state.run.result;
  const rows = r.matches.map((m) => ({ status: m.status, match: m, bank: m.bank, tally: m.tally, reason: m.reason, difference: m.difference }));
  rows.push(...r.bank_only.map((b) => ({ status: "bank_only", bank: [b], tally: [], reason: b.from_rejected_match ? "Match rejected." : "No TallyPrime entry found." })));
  rows.push(...r.tally_only.map((t) => ({ status: "tally_only", bank: [], tally: [t], reason: t.from_rejected_match ? "Match rejected." : "Not found on the statement." })));
  rows.push(...[...r.excluded, ...r.unreadable].map((x) => ({ status: "not_compared", bank: [], tally: [], reason: x.reason, item: x })));
  return rows;
}

function renderTransactions() {
  const body = $("txn-body");
  clear(body);
  const filter = $("txn-filter").value;
  const rows = transactionRows().filter((row) => {
    const rejected = row.match?.decision?.decision === "rejected";
    if (filter === "all") return true;
    if (filter === "attention") return row.status !== "matched" && !(row.match?.needs_approval && row.match.decision);
    if (filter === "awaiting") return row.match?.needs_approval && !row.match.decision;
    if (filter === "matched") return row.match && !rejected && (row.status === "matched" || row.match.decision);
    return row.status === filter;
  });
  if (!rows.length) body.append(el("tr", {}, [el("td", { className: "muted", text: "Nothing to show.", attrs: { colspan: "8" } })]));
  for (const row of rows) {
    const [label, kind] = MATCH_STATUS[row.status] ?? [row.status, "neutral"];
    const cells = [el("td", {}, [badge(label, kind)])];
    if (row.status === "not_compared") {
      cells.push(el("td", { className: "details", attrs: { colspan: "5" },
        text: `#${row.item.document_id}/${row.item.row} ${row.item.date ? formatDate(row.item.date) : ""} ${row.item.description ?? ""}` }));
    } else {
      cells.push(...sideCells(row.bank, "bank"), ...sideCells(row.tally, "tally"), differenceCell(row.difference ?? null));
    }
    cells.push(el("td", { className: "details", text: row.reason }), decisionCell(row.match));
    body.append(el("tr", {}, cells));
  }
}

function decisionCell(match) {
  const cell = el("td");
  if (!match || match.status === "duplicate") return cell;
  if (match.decision) {
    const d = match.decision;
    cell.append(badge(d.decision === "approved" ? "Approved" : "Rejected", d.decision === "approved" ? "ok" : "error"),
      el("span", { className: "figure-source", text: `${d.by ?? "unknown"}, ${formatDateTime(d.at)}${d.note ? ` — “${d.note}”` : ""}` }));
    cell.append(decisionButton("Reopen", match, "reopened"));
    return cell;
  }
  if (match.needs_approval) cell.append(decisionButton("Approve", match, "approved"));
  cell.append(decisionButton("Reject", match, "rejected"));
  return cell;
}

function decisionButton(text, match, decision) {
  const button = el("button", { className: "btn btn-secondary btn-small", text, attrs: { type: "button" } });
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const run = await api.post(`/reconciliation/runs/${state.runId}/matches/${match.signature}`,
        { decision, note: $("decision-note").value.trim() || null });
      renderRun(run);
    } catch (error) {
      showError($("txn-message"), error);
      button.disabled = false;
    }
  });
  return button;
}

function setTransactionDefaults(a) {
  if (!a.last_day) return;
  const last = a.last_day;
  const firstOfMonth = `${last.slice(0, 7)}-01`;
  $("txn-from").value = a.first_day > firstOfMonth ? a.first_day : firstOfMonth;
  $("txn-to").value = last;
}

async function loadTab() {
  if (state.tab === "monthly") await loadMonthly();
  else if (state.tab === "yearly") await loadYearly();
  else if (state.tab === "missing") await loadMissing();
  else if (state.tab === "transactions" && state.runId && !state.run) await openRun(state.runId);
}

const TABS = ["monthly", "yearly", "transactions", "missing", "statements"];

function selectTab(tab) {
  state.tab = tab;
  for (const name of TABS) {
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
for (const kind of ["monthly", "yearly"]) {
  for (const format of ["xlsx", "pdf"]) $(`${kind}-${format}`).addEventListener("click", () => saveAndDownload(kind, format));
}
$("txn-form").addEventListener("submit", (e) => { e.preventDefault(); runTransactions(); });
$("run-select").addEventListener("change", (e) => openRun(Number(e.target.value) || null));
$("txn-filter").addEventListener("change", () => { if (state.run) renderTransactions(); });
$("txn-filters").addEventListener("submit", (e) => e.preventDefault());
$("txn-saved").addEventListener("submit", (e) => e.preventDefault());
for (const name of TABS) $(`tab-${name}`).addEventListener("click", () => selectTab(name));

loadAccounts();
