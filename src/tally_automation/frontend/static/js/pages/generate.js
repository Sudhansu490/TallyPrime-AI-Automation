// Voucher generation (Phase 7): a reviewer chooses the TallyPrime ledgers for a
// document's voucher(s); the backend builds the import XML, checks it against
// the retrieved masters and stores it. Nothing is sent to TallyPrime here.
// Suggestions are shown as suggestions: the reviewer submits every ledger.

import { api } from "../api.js";
import {
  DOCUMENT_TYPE_LABELS, clear, el, formatDate, formatDateTime, formatINR, showError, showMessage,
} from "../ui.js";
import { KIND_LABELS, checkItem, renderVouchers } from "../voucher_view.js";

const $ = (id) => document.getElementById(id);
const state = { documentId: null, companyGuid: null, setup: null, pickers: {}, rows: [] };
const dash = "—";


// ---- Ledger picker -------------------------------------------------------------------

function option(text, value) {
  return el("option", { text, attrs: { value } });
}

/** A select of suggested ledgers plus a search over all retrieved ledgers of the company. */
function ledgerPicker(label, suggestions = [], onChange = null) {
  const select = el("select", { attrs: { "aria-label": label, required: "" } });
  const search = el("input", {
    className: "picker-search",
    attrs: { type: "search", placeholder: "Search all ledgers…", maxlength: "100", autocomplete: "off", "aria-label": `Search ledgers: ${label}` },
  });
  const suggested = el("optgroup", { attrs: { label: "Suggested" } });
  const results = el("optgroup", { attrs: { label: "Search results" } });
  select.append(option(suggestions.length ? "Choose a ledger…" : "No suggestion — search below", ""));
  for (const s of suggestions) suggested.append(option(`${s.name} — ${s.reason}${s.parent ? ` (${s.parent})` : ""}`, s.guid));
  if (suggestions.length) select.append(suggested);
  select.append(results);
  results.hidden = true;
  // Pre-select only an unambiguous suggestion; the reviewer still submits it.
  if (suggestions.length === 1 || suggestions[0]?.reason === "Chosen last time") select.value = suggestions[0].guid;

  let timer = null;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = search.value.trim();
      clear(results);
      results.hidden = !q;
      if (!q) return;
      try {
        const data = await api.get(`/tally/companies/${encodeURIComponent(state.companyGuid)}/ledgers?q=${encodeURIComponent(q)}&limit=25`);
        for (const l of data.items) results.append(option(`${l.name} (${l.parent ?? "Primary"})${l.gstin ? ` · ${l.gstin}` : ""}`, l.guid));
        if (!data.items.length) results.append(el("option", { text: "No ledger matches", attrs: { value: "", disabled: "" } }));
        else if (!select.value && data.items.length === 1) { select.value = data.items[0].guid; onChange?.(); }
      } catch (error) {
        results.append(el("option", { text: error.message, attrs: { value: "", disabled: "" } }));
      }
    }, 300);
  });
  if (onChange) select.addEventListener("change", onChange);
  return { element: el("div", { className: "picker" }, [select, search]), value: () => select.value || null, select };
}

function voucherTypeSelect(label, options = []) {
  const select = el("select", { attrs: { "aria-label": label } });
  if (!options.length) select.append(option("No active voucher type of this kind", ""));
  for (const o of options) select.append(option(`${o.name} — ${o.reason}`, o.name));
  return select;
}

// ---- Setup ---------------------------------------------------------------------------------

function fact(label, value) {
  return el("div", { className: "stat" }, [el("dt", { text: label }), el("dd", { text: value ?? dash })]);
}

function mappingRow(entry, side, amount, picker) {
  return el("tr", {}, [el("td", { text: entry }), el("td", { text: side }), el("td", { className: "num", text: formatINR(amount) }),
    el("td", {}, [picker.element ?? picker])]);
}

function renderInvoice(setup) {
  const inv = setup.invoice;
  const s = setup.suggestions;
  const summary = $("invoice-summary");
  clear(summary);
  summary.append(
    fact("Supplier", inv.supplier_name), fact("GSTIN", inv.supplier_gstin), fact("Invoice", inv.invoice_number),
    fact("Date", formatDate(inv.invoice_date)), fact("Grand total", formatINR(inv.grand_total)),
  );
  const planChecks = $("plan-checks");
  clear(planChecks);
  for (const c of inv.checks.filter((c) => !c.passed || c.severity === "info")) planChecks.append(checkItem(c));

  const body = $("invoice-rows");
  clear(body);
  state.pickers = { purchase: {}, duty: {} };
  if (!s) return;
  const typeSelect = voucherTypeSelect("Purchase voucher type", s.voucher_types.purchase);
  state.pickers.voucherType = typeSelect;
  body.append(mappingRow("Voucher type", dash, null, typeSelect));
  state.pickers.party = ledgerPicker("Supplier ledger", s.party);
  body.append(mappingRow("Supplier (party)", "Cr", inv.grand_total, state.pickers.party));
  for (const b of inv.buckets) {
    state.pickers.purchase[b.key] = ledgerPicker(`Purchase ledger: ${b.label}`, s.purchase[b.key] ?? []);
    body.append(mappingRow(b.label, "Dr", b.taxable, state.pickers.purchase[b.key]));
  }
  for (const t of inv.taxes) {
    state.pickers.duty[t.head] = ledgerPicker(`${t.head} ledger`, s.duty[t.head] ?? []);
    body.append(mappingRow(t.head, "Dr", t.amount, state.pickers.duty[t.head]));
  }
  if (Number(inv.round_off) !== 0) {
    state.pickers.roundOff = ledgerPicker("Round-off ledger", s.round_off);
    const amount = String(inv.round_off).replace(/^-/, "");
    body.append(mappingRow("Round-off", Number(inv.round_off) > 0 ? "Dr" : "Cr", amount, state.pickers.roundOff));
  }
}

function renderStatement(setup) {
  const st = setup.statement;
  const s = setup.suggestions;
  const head = $("statement-head");
  clear(head);
  state.rows = [];
  const body = $("transaction-rows");
  clear(body);
  if (!s) return;
  state.pickers = { bank: ledgerPicker("Bank ledger", s.bank), types: {} };
  head.append(el("label", {}, [el("span", { text: `Bank ledger for account ${st.account_number ?? "(not read)"}` }), state.pickers.bank.element]));
  for (const kind of ["payment", "receipt", "contra"]) {
    state.pickers.types[kind] = voucherTypeSelect(`${KIND_LABELS[kind]} voucher type`, s.voucher_types[kind]);
    head.append(el("label", {}, [el("span", { text: `${KIND_LABELS[kind]} voucher type` }), state.pickers.types[kind]]));
  }
  for (const t of st.transactions) {
    const problems = t.checks.filter((c) => !c.passed && c.severity === "error");
    const usable = !t.excluded && !problems.length;
    const box = el("input", { attrs: { type: "checkbox", "aria-label": `Generate a voucher for transaction ${t.sequence}` } });
    const picker = usable ? ledgerPicker(`Other ledger for transaction ${t.sequence}`, s.counter[String(t.id)] ?? [], () => { box.checked = true; }) : null;
    box.disabled = !usable;
    box.checked = Boolean(usable && picker.value());
    const description = el("td", { className: "details" }, [el("span", { text: t.description ?? dash })]);
    if (t.reference) description.append(el("div", { className: "small muted", text: `Ref ${t.reference}` }));
    if (t.excluded) description.append(el("div", { className: "small", text: "Confirmed duplicate — no voucher." }));
    for (const p of problems) description.append(el("div", { className: "small error-text", text: p.message }));
    body.append(el("tr", {}, [
      el("td", {}, [box]), el("td", { text: String(t.sequence) }), el("td", { text: formatDate(t.date) }), description,
      el("td", { className: "num", text: t.money_out === true ? formatINR(t.amount) : "" }),
      el("td", { className: "num", text: t.money_out === false ? formatINR(t.amount) : "" }),
      el("td", {}, picker ? [picker.element] : [el("span", { className: "muted", text: dash })]),
    ]));
    if (picker) state.rows.push({ id: t.id, sequence: t.sequence, box, picker });
  }
}

function renderSetup(setup) {
  state.setup = setup;
  const elig = $("eligibility");
  clear(elig);
  elig.append(setup.eligibility.eligible
    ? el("p", { className: "alert alert-success", text: "The document passed validation and duplicate review: it is eligible for voucher generation." })
    : el("div", { className: "alert alert-warning" }, [
      el("strong", { text: "Not eligible for voucher generation yet:" }),
      el("ul", {}, setup.eligibility.reasons.map((r) => el("li", { text: r }))),
      el("a", { text: "Open it on the Review page", attrs: { href: `/review?document=${setup.document_id}` } }),
    ]));
  const c = setup.company;
  $("company-meta").textContent = c
    ? `Masters retrieved ${formatDateTime(c.last_synced_at)} · books from ${formatDate(c.books_from)} · ${c.state ?? "state unknown"}`
    : "No TallyPrime masters retrieved yet. Retrieve them on the TallyPrime page first.";
  const isInvoice = setup.document_type === "purchase_invoice";
  $("invoice-mapping").hidden = !isInvoice;
  $("statement-mapping").hidden = isInvoice;
  if (isInvoice) renderInvoice(setup); else renderStatement(setup);
  $("mapping-form").hidden = !c || !setup.eligibility.eligible;
  $("setup-card").hidden = false;
}

// ---- Generate --------------------------------------------------------------------------------

function invoicePayload() {
  const p = state.pickers;
  const missing = [];
  const need = (picker, label) => { const v = picker.value(); if (!v) missing.push(label); return v; };
  const body = {
    voucher_type: p.voucherType.value || (missing.push("voucher type"), ""),
    party: need(p.party, "supplier"),
    purchase: {}, duty: {}, round_off: p.roundOff ? need(p.roundOff, "round-off") : null,
  };
  for (const [key, picker] of Object.entries(p.purchase)) body.purchase[key] = need(picker, "purchase ledger");
  for (const [head, picker] of Object.entries(p.duty)) body.duty[head] = need(picker, `${head} ledger`);
  return { body: { invoice: body }, missing };
}

function statementPayload() {
  const p = state.pickers;
  const missing = [];
  const bank = p.bank.value();
  if (!bank) missing.push("bank ledger");
  const transactions = [];
  for (const row of state.rows) {
    if (!row.box.checked) continue;
    const ledger = row.picker.value();
    if (!ledger) missing.push(`other ledger of transaction ${row.sequence}`);
    else transactions.push({ transaction_id: row.id, ledger });
  }
  if (!transactions.length && !missing.length) missing.push("at least one ticked transaction");
  const types = {};
  for (const [kind, select] of Object.entries(p.types)) if (select.value) types[kind] = select.value;
  return { body: { statement: { bank, voucher_types: types, transactions } }, missing };
}

async function generate(event) {
  event.preventDefault();
  const { body, missing } = state.setup.document_type === "purchase_invoice" ? invoicePayload() : statementPayload();
  if (missing.length) {
    showMessage($("generate-message"), "error", `Choose: ${missing.join(", ")}.`);
    return;
  }
  const button = $("generate");
  button.disabled = true;
  showMessage($("generate-message"), "loading", "Generating and checking…");
  try {
    const vouchers = await api.post(`/documents/${state.documentId}/vouchers`, { company_guid: state.companyGuid, ...body });
    const ready = vouchers.filter((v) => v.status === "ready").length;
    showMessage($("generate-message"), ready === vouchers.length ? "success" : "warning",
      `Generated ${vouchers.length} voucher(s): ${ready} ready for approval, ${vouchers.length - ready} blocked. ` +
      "Nothing was sent to TallyPrime. Approve and import them on the Vouchers page.");
    await loadVouchers();
    $("vouchers-card").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError($("generate-message"), error);
  } finally {
    button.disabled = false;
  }
}

// ---- Generated vouchers --------------------------------------------------------------------------

async function loadVouchers() {
  const container = $("vouchers");
  clear($("vouchers-message"));
  try {
    const all = $("show-superseded").checked;
    const vouchers = await api.get(`/documents/${state.documentId}/vouchers${all ? "?include_superseded=true" : ""}`);
    renderVouchers(container, vouchers, {});
    if (!vouchers.length) container.firstChild.textContent = "No vouchers generated for this document yet.";
    $("vouchers-card").hidden = false;
  } catch (error) {
    showError($("vouchers-message"), error);
    $("vouchers-card").hidden = false;
  }
}

// ---- Loading ----------------------------------------------------------------------------------------

async function loadCompanies() {
  const select = $("company-select");
  clear(select);
  const companies = await api.get("/tally/companies");
  if (!companies.length) select.append(option("No masters retrieved", ""));
  for (const c of companies) select.append(option(c.name, c.guid));
  if (state.companyGuid) select.value = state.companyGuid;
}

async function loadSetup() {
  if (!state.documentId) return;
  $("setup-card").hidden = true;
  $("vouchers-card").hidden = true;
  clear($("generate-message"));
  showMessage($("page-message"), "loading", "Loading…");
  try {
    const query = state.companyGuid ? `?company=${encodeURIComponent(state.companyGuid)}` : "";
    const setup = await api.get(`/documents/${state.documentId}/voucher-setup${query}`);
    state.companyGuid = setup.company?.guid ?? null;
    await loadCompanies();
    renderSetup(setup);
    clear($("page-message"));
    await loadVouchers();
  } catch (error) {
    showError($("page-message"), error);
  }
}

async function loadDocuments() {
  const select = $("document-select");
  try {
    const data = await api.get("/documents?status=processed&limit=200");
    clear(select);
    select.append(option(data.items.length ? "Choose a document…" : "No processed documents yet", ""));
    for (const doc of data.items) {
      select.append(option(`#${doc.id} · ${DOCUMENT_TYPE_LABELS[doc.document_type]} · ${doc.original_filename}`, String(doc.id)));
    }
    if (state.documentId) select.value = String(state.documentId);
  } catch (error) {
    showError($("page-message"), error);
  }
}

function selectDocument(id) {
  state.documentId = id ? Number(id) : null;
  const url = new URL(window.location.href);
  if (state.documentId) url.searchParams.set("document", String(state.documentId));
  else url.searchParams.delete("document");
  window.history.replaceState(null, "", url);
  loadSetup();
}

$("document-select").addEventListener("change", (e) => selectDocument(e.target.value));
$("company-select").addEventListener("change", (e) => { state.companyGuid = e.target.value || null; loadSetup(); });
$("reload").addEventListener("click", () => { loadDocuments(); loadSetup(); });
$("picker").addEventListener("submit", (e) => e.preventDefault());
$("mapping-form").addEventListener("submit", generate);
$("show-superseded").addEventListener("change", loadVouchers);

const initial = Number(new URLSearchParams(window.location.search).get("document"));
state.documentId = Number.isInteger(initial) && initial > 0 ? initial : null;
loadDocuments();
loadSetup();
