// Extracted data review: shows every extracted field with its evidence,
// confidence and status, and lets a reviewer correct values. All checks run on
// the backend; this page only displays results and sends edits.

import { api } from "../api.js";
import {
  DOCUMENT_TYPE_LABELS, clear, el, formatDate, formatDateTime, formatINR, showError, showMessage,
} from "../ui.js";

const $ = (id) => document.getElementById(id);
const state = { documentId: null, extraction: null, editing: null };

const MONEY_TYPES = new Set(["amount", "balance"]);
const percent = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);

// ---- Value display ----------------------------------------------------------------

function displayValue(field) {
  if (field.raw === null) return "—";
  if (field.value === null) return field.raw; // unparsable: show exactly what was read
  if (MONEY_TYPES.has(field.type)) return formatINR(field.value);
  if (field.type === "date") return formatDate(field.value);
  if (field.type === "percent") return `${field.value}%`;
  return field.value;
}

function isLow(field) {
  return field.flags.includes("low_confidence") || field.flags.includes("low_ocr_confidence");
}

function tags(field) {
  const out = [];
  if (field.method === "edited") out.push(el("span", { className: "tag tag-edited", text: "Edited" }));
  if (field.method === "calculated") out.push(el("span", { className: "tag tag-calculated", text: "Calculated" }));
  if (field.method === "table") out.push(el("span", { className: "tag tag-calculated", text: "From table" }));
  if (isLow(field)) out.push(el("span", { className: "tag tag-low", text: "Low confidence" }));
  if (field.status === "invalid") out.push(el("span", { className: "tag tag-invalid", text: "Invalid" }));
  else if (field.status === "warning") out.push(el("span", { className: "tag tag-warning", text: "Check" }));
  if (field.flags.includes("required_missing")) out.push(el("span", { className: "tag tag-invalid", text: "Required" }));
  if (field.flags.includes("conflict")) out.push(el("span", { className: "tag tag-warning", text: "Conflict" }));
  return out;
}

function cellClass(field) {
  if (field.status === "invalid" || field.flags.includes("required_missing")) return "cell-invalid";
  if (field.status === "warning") return "cell-warning";
  if (field.method === "edited") return "cell-edited";
  if (field.raw === null) return "cell-missing";
  return "";
}

// ---- Rendering ----------------------------------------------------------------------

function renderOverview(data) {
  const run = data.run;
  $("run-meta").textContent =
    `${DOCUMENT_TYPE_LABELS[data.document_type]} · “${data.original_filename}” · attempt ${run.attempt} · ` +
    `${run.provider} ${run.model} · prompt ${run.prompt_version} · ${run.llm_calls} call(s), ` +
    `${run.prompt_tokens + run.completion_tokens} tokens, ${run.seconds}s · ${formatDateTime(run.created_at)}`;
  const issues = $("document-issues");
  clear(issues);
  for (const issue of data.document_issues) {
    issues.append(el("p", { className: "alert alert-warning", text: issue, attrs: { role: "alert" } }));
  }
  const s = data.summary;
  const summary = $("summary");
  clear(summary);
  for (const [label, value] of [
    ["Fields", s.fields], ["Invalid", s.invalid], ["To check", s.warning],
    ["Low confidence", s.low_confidence], ["Not in document", s.missing], ["Edited", s.edited],
  ]) {
    summary.append(el("div", { className: "stat" }, [el("dt", { text: label }), el("dd", { text: value })]));
  }
  $("overview").hidden = false;
}

function evidenceText(field) {
  if (!field.evidence) return "—";
  return field.page ? `“${field.evidence}” (p. ${field.page})` : `“${field.evidence}”`;
}

function renderHeader(data) {
  const body = $("header-body");
  clear(body);
  for (const field of data.data.fields) {
    const edit = el("button", { className: "btn btn-secondary btn-small", text: "Edit", attrs: { type: "button" } });
    edit.addEventListener("click", () => openEditor(data.data.entity, data.data.id, field, null));
    body.append(el("tr", { className: cellClass(field) }, [
      el("th", { text: field.label, attrs: { scope: "row" } }),
      el("td", { className: "value", text: displayValue(field), attrs: { title: field.raw ?? "" } }),
      el("td", {}, [...tags(field), el("div", { className: "small muted", text: field.messages.join(" ") })]),
      el("td", { className: "evidence small", text: evidenceText(field) }),
      el("td", { text: percent(field.confidence) }),
      el("td", {}, [edit]),
    ]));
  }
  $("header-heading").textContent = data.document_type === "bank_statement" ? "Statement details" : "Invoice details";
  $("header-card").hidden = false;
}

function renderRows(data) {
  const head = $("rows-head");
  const body = $("rows-body");
  clear(head);
  clear(body);
  head.append(el("th", { text: "#" }));
  for (const column of data.data.row_columns) head.append(el("th", { text: column.label }));
  head.append(el("th", { text: "Conf." }));

  for (const row of data.data.rows) {
    const tr = el("tr", { attrs: { title: row.row_text ? `Row as printed (p. ${row.page ?? "?"}): ${row.row_text}` : "" } });
    tr.append(el("td", { text: row.number }));
    for (const field of row.fields) {
      const button = el("button", {
        className: `cell-button ${cellClass(field)}`,
        text: displayValue(field),
        attrs: { type: "button", "aria-label": `${field.label} row ${row.number}: ${displayValue(field)}. Edit` },
      });
      button.addEventListener("click", () => openEditor(data.data.row_entity, row.id, field, row));
      tr.append(el("td", { className: MONEY_TYPES.has(field.type) ? "num" : "" }, [button, ...tags(field)]));
    }
    tr.append(el("td", { text: percent(row.confidence) }));
    body.append(tr);
  }
  $("rows-heading").textContent = data.document_type === "bank_statement"
    ? `Transactions (${data.data.rows.length})` : `Line items (${data.data.rows.length})`;
  $("rows-card").hidden = false;
}

function render(data) {
  state.extraction = data;
  renderOverview(data);
  renderHeader(data);
  renderRows(data);
}

// ---- Validation -----------------------------------------------------------------------

const OUTCOME = {
  passed: ["Passed", "ok"],
  passed_with_warnings: ["Passed with warnings", "warning"],
  failed: ["Failed", "error"],
};
const SEVERITY = { error: ["Error", "error"], warning: ["Warning", "warning"], info: ["Info", "neutral"] };

function checkItem(check) {
  const [label, kind] = check.passed ? ["OK", "ok"] : SEVERITY[check.severity];
  const rowName = state.extraction?.document_type === "bank_statement" ? "Transaction" : "Line";
  const where = check.row ? `${rowName} ${check.row}: ` : "";
  const item = el("li", { className: `check check-${check.passed ? "passed" : check.severity}` }, [
    el("span", { className: `badge badge-${kind}`, text: label }),
    el("span", { className: "check-category small muted", text: check.category === "extraction" ? "Extraction" : "Accounting" }),
    el("span", { text: `${where}${check.message}` }),
  ]);
  const numbers = [];
  if (check.calculated !== null) numbers.push(`calculated ${formatINR(check.calculated)}`);
  if (check.printed !== null && check.category === "accounting") numbers.push(`printed ${formatINR(check.printed)}`);
  if (check.difference !== null && !check.passed) numbers.push(`difference ${formatINR(check.difference)}`);
  if (numbers.length) {
    const parts = [el("span", { text: numbers.join(" · ") })];
    if (check.calculated !== null) parts.unshift(el("span", { className: "tag tag-calculated", text: "Calculated" }));
    item.append(el("div", { className: "small check-numbers" }, parts));
  }
  return item;
}

function renderValidation(v) {
  const [outcomeText, outcomeKind] = OUTCOME[v.outcome];
  const status = $("validation-status");
  clear(status);
  status.append(el("p", {}, [
    el("span", { className: `badge badge-${outcomeKind}`, text: outcomeText }),
    el("span", { text: ` ${v.error_count} error(s), ${v.warning_count} warning(s) · rules ${v.rules_version} · ${formatDateTime(v.validated_at)}` }),
  ]));
  const e = v.eligibility;
  status.append(e.eligible
    ? el("p", { className: "alert alert-success" }, [
      el("span", { text: "Eligible for voucher generation. " }),
      el("a", { text: "Generate TallyPrime vouchers", attrs: { href: `/generate?document=${v.document_id}` } }),
    ])
    : el("div", { className: "alert alert-warning" }, [
      el("strong", { text: "Not yet eligible for voucher generation:" }),
      el("ul", {}, e.reasons.map((r) => el("li", { text: r }))),
    ]));
  if (e.excluded_transaction_ids.length) {
    status.append(el("p", { className: "small muted", text: `${e.excluded_transaction_ids.length} transaction(s) confirmed as duplicates will be left out.` }));
  }

  const dups = $("validation-duplicates");
  clear(dups);
  const open = v.duplicates.filter((d) => d.status === "open");
  if (v.duplicates.length) {
    dups.append(el("p", {}, [
      el("span", { className: `badge badge-${open.length ? "warning" : "neutral"}`, text: `${v.duplicates.length} possible duplicate(s)` }),
      el("span", { text: open.length ? ` ${open.length} not reviewed yet. ` : " All reviewed. " }),
      el("a", { text: "Open duplicate management", attrs: { href: "/duplicates" } }),
    ]));
  }

  const problems = $("validation-problems");
  clear(problems);
  const failing = v.checks.filter((c) => !c.passed && c.severity !== "info");
  const order = { error: 0, warning: 1 };
  failing.sort((a, b) => order[a.severity] - order[b.severity] || (a.row ?? 0) - (b.row ?? 0));
  if (failing.length) problems.append(el("ul", { className: "check-list" }, failing.map(checkItem)));
  else problems.append(el("p", { className: "muted", text: "No errors or warnings." }));

  const passed = v.checks.filter((c) => c.passed || c.severity === "info");
  const list = $("validation-passed");
  clear(list);
  for (const c of passed) list.append(checkItem(c));
  $("validation-passed-summary").textContent = `Checks that passed or could not run (${passed.length})`;
  $("validation-card").hidden = false;
}

async function loadValidation() {
  clear($("validation-message"));
  try {
    renderValidation(await api.get(`/documents/${state.documentId}/validation`));
  } catch (error) {
    for (const id of ["validation-status", "validation-problems", "validation-duplicates", "validation-passed"]) clear($(id));
    showError($("validation-message"), error);
    $("validation-card").hidden = false;
  }
}

async function revalidate() {
  const button = $("revalidate");
  button.disabled = true;
  showMessage($("validation-message"), "loading", "Validating…");
  try {
    renderValidation(await api.post(`/documents/${state.documentId}/validate`));
    showMessage($("validation-message"), "success", "Validation and duplicate detection ran again.");
  } catch (error) {
    showError($("validation-message"), error);
  } finally {
    button.disabled = false;
  }
}

// ---- Editing ------------------------------------------------------------------------

function fact(label, value) {
  return [el("dt", { text: label }), el("dd", { text: value ?? "—" })];
}

async function loadHistory(entity, id, fieldName) {
  const list = $("edit-history");
  clear(list);
  try {
    const edits = (await api.get(`/extractions/${entity}/${id}/edits`)).filter((e) => e.field_name === fieldName);
    if (!edits.length) list.append(el("li", { className: "muted", text: "No edits yet." }));
    for (const e of edits) {
      const who = e.edited_by ?? `device ${e.client_address ?? "unknown"} (before login existed)`;
      list.append(el("li", {
        text: `${formatDateTime(e.edited_at)} · ${who}: “${e.old_raw ?? "—"}” → “${e.new_raw ?? "—"}”` + (e.reason ? ` · ${e.reason}` : ""),
      }));
    }
  } catch (error) {
    list.append(el("li", { className: "error-text", text: error.message }));
  }
}

function openEditor(entity, id, field, row) {
  state.editing = { entity, id, field };
  $("edit-title").textContent = row ? `Edit ${field.label} (row ${row.number})` : `Edit ${field.label}`;
  const facts = $("edit-facts");
  clear(facts);
  facts.append(
    ...fact("Current value", field.raw),
    ...fact("Read as", field.value === null ? "not interpretable" : displayValue(field)),
    ...fact("Status", `${field.status}${field.messages.length ? ` — ${field.messages.join(" ")}` : ""}`),
    ...fact("Evidence", evidenceText(field)),
    ...fact("Confidence", percent(field.confidence)),
  );
  if (field.original) {
    facts.append(...fact("Originally extracted", `${field.original.raw ?? "—"} (${evidenceText(field.original)})`));
  }
  if (field.type === "account_number") {
    facts.append(...fact("Note", "Account numbers are shown masked. Type the full number to replace it."));
  }
  $("edit-value").value = field.type === "account_number" ? "" : field.raw ?? "";
  $("edit-reason").value = "";
  clear($("edit-message"));
  $("edit-save").disabled = false;
  $("edit-history-wrap").open = false;
  loadHistory(entity, id, field.name);
  $("edit-dialog").showModal();
  $("edit-value").focus();
}

async function saveEdit(event) {
  event.preventDefault();
  const { entity, id, field } = state.editing;
  const value = $("edit-value").value.trim();
  const reason = $("edit-reason").value.trim();
  $("edit-save").disabled = true;
  showMessage($("edit-message"), "loading", "Saving…");
  try {
    const result = await api.patch(`/extractions/${entity}/${id}/fields/${field.name}`, {
      value: value === "" ? null : value,
      reason: reason === "" ? null : reason,
    });
    $("edit-dialog").close();
    await loadExtraction();
    const v = result.validation;
    showMessage($("page-message"), "success", `${field.label} updated. The change is recorded in the edit history. ` +
      (v ? `Validation ran again: ${OUTCOME[v.outcome][0].toLowerCase()} (${v.error_count} error(s), ${v.warning_count} warning(s)).`
        : "Validation could not run; use “Run validation again”."));
  } catch (error) {
    showError($("edit-message"), error);
    $("edit-save").disabled = false;
  }
}

// ---- Loading -------------------------------------------------------------------------

async function loadDocuments() {
  const select = $("document-select");
  try {
    const data = await api.get("/documents?status=processed&limit=200");
    clear(select);
    select.append(el("option", { text: data.items.length ? "Choose a document…" : "No processed documents yet", attrs: { value: "" } }));
    for (const doc of data.items) {
      select.append(el("option", {
        text: `#${doc.id} · ${DOCUMENT_TYPE_LABELS[doc.document_type]} · ${doc.original_filename}`,
        attrs: { value: String(doc.id) },
      }));
    }
    if (state.documentId) select.value = String(state.documentId);
  } catch (error) {
    showError($("page-message"), error);
  }
}

async function loadExtraction() {
  if (!state.documentId) return;
  for (const id of ["overview", "validation-card", "header-card", "rows-card"]) $(id).hidden = true;
  showMessage($("page-message"), "loading", "Loading extracted data…");
  try {
    render(await api.get(`/documents/${state.documentId}/extraction`));
    clear($("page-message"));
    await loadValidation();
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
  loadExtraction();
}

$("document-select").addEventListener("change", (event) => selectDocument(event.target.value));
$("reload").addEventListener("click", () => { loadDocuments(); loadExtraction(); });
$("picker").addEventListener("submit", (event) => event.preventDefault());
$("edit-form").addEventListener("submit", saveEdit);
$("edit-cancel").addEventListener("click", () => $("edit-dialog").close());
$("revalidate").addEventListener("click", revalidate);

const initial = Number(new URLSearchParams(window.location.search).get("document"));
state.documentId = Number.isInteger(initial) && initial > 0 ? initial : null;
loadDocuments();
loadExtraction();
