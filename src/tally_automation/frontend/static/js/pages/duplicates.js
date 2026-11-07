// Duplicate management: business-level matches (Levels 2 and 3) with their
// evidence, reviewer decisions, and the identical-file re-uploads (Level 1).
// The backend detects and records everything; this page only displays and
// sends decisions.

import { api } from "../api.js";
import {
  DOCUMENT_TYPE_LABELS, clear, el, formatDate, formatDateTime, formatINR, showError, showMessage,
} from "../ui.js";

const $ = (id) => document.getElementById(id);
const state = { pending: null };

const KIND_LABELS = { invoice: "Invoice", bank_transaction: "Bank transaction" };
const STATUS_TEXT = { open: "To review", duplicate: "Duplicate", not_duplicate: "Not a duplicate" };
const STATUS_BADGE = { open: "warning", duplicate: "error", not_duplicate: "ok" };
const MATCH_BADGE = { same: ["Same", "ok"], similar: ["Similar", "warning"], different: ["Different", "error"], unknown: ["Unknown", "neutral"] };
const DECISION_TEXT = { duplicate: "Marked duplicate", not_duplicate: "Marked not a duplicate", reopened: "Reopened" };
const MONEY_FIELDS = new Set(["grand_total", "debit", "credit", "balance"]);
const DATE_FIELDS = new Set(["invoice_date", "txn_date"]);

const badge = (text, kind) => el("span", { className: `badge badge-${kind}`, text });

function docLabel(doc) {
  return `#${doc.id} “${doc.original_filename}”`;
}

function show(fieldName, value) {
  if (value === null || value === undefined) return "—";
  if (MONEY_FIELDS.has(fieldName)) return formatINR(value);
  if (DATE_FIELDS.has(fieldName)) return formatDate(value);
  return String(value);
}

// ---- Rendering ----------------------------------------------------------------------

function evidenceTable(match) {
  const rows = match.evidence.map((e) => {
    const [text, kind] = MATCH_BADGE[e.match] ?? [e.match, "neutral"];
    return el("tr", {}, [
      el("th", { text: e.label, attrs: { scope: "row" } }),
      el("td", { text: show(e.field, e.this) }),
      el("td", { text: show(e.field, e.other) }),
      el("td", {}, [badge(text, kind)]),
    ]);
  });
  return el("div", { className: "table-wrap" }, [el("table", { className: "table evidence-table" }, [
    el("thead", {}, [el("tr", {}, [
      el("th", { text: "Field" }),
      el("th", { text: `${match.record} (later, #${match.document.id})` }),
      el("th", { text: `${match.other_record} (earlier, #${match.other_document.id})` }),
      el("th", { text: "Evidence" }),
    ])]),
    el("tbody", {}, rows),
  ])]);
}

function decisionButtons(matches) {
  const open = matches.filter((m) => m.status === "open");
  const decided = matches.filter((m) => m.status !== "open");
  const button = (text, primary, onClick) => {
    const node = el("button", { className: `btn btn-small ${primary ? "" : "btn-secondary"}`, text, attrs: { type: "button" } });
    node.addEventListener("click", onClick);
    return node;
  };
  const many = matches.length > 1;
  const out = [];
  if (open.length) {
    out.push(button(many ? `All ${open.length} are duplicates` : "Duplicate", true,
      () => ask(open, "duplicate")));
    out.push(button(many ? `None are duplicates` : "Not a duplicate", false,
      () => ask(open, "not_duplicate")));
  }
  if (decided.length && !many) out.push(button("Reopen", false, () => ask(decided, "reopened")));
  return el("div", { className: "actions" }, out);
}

function history(match) {
  if (!match.reviews.length) return null;
  return el("details", {}, [
    el("summary", { className: "small", text: `Decision history (${match.reviews.length})` }),
    el("ul", { className: "small" }, match.reviews.map((r) => {
      const who = r.reviewed_by ?? `device ${r.client_address ?? "unknown"} (before login existed)`;
      return el("li", { text: `${formatDateTime(r.reviewed_at)} · ${who}: ${DECISION_TEXT[r.decision]}` + (r.note ? ` · “${r.note}”` : "") });
    })),
  ]);
}

function matchBlock(match, single) {
  const block = el("div", { className: "match" }, [
    el("div", { className: "match-head" }, [
      badge(STATUS_TEXT[match.status], STATUS_BADGE[match.status]),
      badge(match.strength === "probable" ? "Probable" : "Possible", match.strength === "probable" ? "error" : "warning"),
      el("strong", { text: `${match.record} ↔ ${match.other_record}` }),
    ]),
    el("p", { className: "small", text: match.explanation }),
    evidenceTable(match),
  ]);
  if (match.status !== "open") {
    block.append(el("p", {
      className: "small muted",
      text: `Decided ${formatDateTime(match.resolved_at)}` + (match.resolution_note ? ` · “${match.resolution_note}”` : ""),
    }));
  }
  if (!single) block.append(decisionButtons([match]));
  const h = history(match);
  if (h) block.append(h);
  return block;
}

function renderMatches(matches) {
  const container = $("matches");
  clear(container);
  if (!matches.length) {
    container.append(el("p", { className: "muted", text: "Nothing to show for this filter." }));
    return;
  }
  // Group by document pair: overlapping statements give many matches per pair.
  const groups = new Map();
  for (const m of matches) {
    const key = `${m.kind}:${m.document.id}:${m.other_document.id}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(m);
  }
  for (const group of groups.values()) {
    const first = group[0];
    const card = el("article", { className: "match-group" }, [
      el("h3", {}, [
        badge(KIND_LABELS[first.kind], "info"),
        el("span", { text: `${docLabel(first.document)} — later upload` }),
        el("span", { className: "muted", text: "resembles" }),
        el("span", { text: `${docLabel(first.other_document)} — earlier upload` }),
      ]),
      el("p", { className: "small" }, [
        el("a", { text: "Review later document", attrs: { href: `/review?document=${first.document.id}` } }),
        document.createTextNode(" · "),
        el("a", { text: "Review earlier document", attrs: { href: `/review?document=${first.other_document.id}` } }),
        document.createTextNode(` · ${DOCUMENT_TYPE_LABELS[first.document.document_type]} · ${group.length} match${group.length === 1 ? "" : "es"}`),
      ]),
    ]);
    if (group.length > 1) card.append(decisionButtons(group));
    for (const m of group) card.append(matchBlock(m, group.length === 1));
    if (group.length === 1) card.append(decisionButtons(group));
    container.append(card);
  }
}

function renderExact(items) {
  const body = $("exact-body");
  clear(body);
  if (!items.length) {
    body.append(el("tr", {}, [el("td", { className: "muted", text: "No identical re-uploads.", attrs: { colspan: "3" } })]));
    return;
  }
  for (const item of items) {
    body.append(el("tr", {}, [
      el("td", { text: `${docLabel(item.document)} · ${formatDateTime(item.document.uploaded_at)}` }),
      el("td", { text: `${docLabel(item.duplicate_of)} · ${formatDateTime(item.duplicate_of.uploaded_at)}` }),
      el("td", { className: "small muted", text: `${item.sha256.slice(0, 16)}…`, attrs: { title: item.sha256 } }),
    ]));
  }
}

// ---- Decisions ----------------------------------------------------------------------

function ask(matches, decision) {
  state.pending = { ids: matches.map((m) => m.id), decision };
  const n = matches.length;
  const what = n === 1 ? `${matches[0].record} ↔ ${matches[0].other_record}` : `${n} matches`;
  $("decision-title").textContent = {
    duplicate: "Confirm duplicate", not_duplicate: "Confirm not a duplicate", reopened: "Reopen decision",
  }[decision];
  const excluded = matches[0].kind === "invoice"
    ? "the later invoice is kept out of voucher generation"
    : "the later transaction is left out of its statement's vouchers";
  $("decision-summary").textContent = {
    duplicate: `${what}: the later record will be treated as a copy; ${excluded}.`,
    not_duplicate: `${what}: both records are genuine and both stay eligible.`,
    reopened: `${what}: the decision is withdrawn and the match must be reviewed again.`,
  }[decision];
  $("decision-note").value = "";
  clear($("decision-message"));
  $("decision-save").disabled = false;
  $("decision-dialog").showModal();
  $("decision-note").focus();
}

async function saveDecision(event) {
  event.preventDefault();
  const { ids, decision } = state.pending;
  const note = $("decision-note").value.trim() || null;
  $("decision-save").disabled = true;
  showMessage($("decision-message"), "loading", "Saving…");
  try {
    if (ids.length === 1) await api.post(`/duplicates/${ids[0]}/review`, { decision, note });
    else await api.post("/duplicates/review", { match_ids: ids, decision, note });
    $("decision-dialog").close();
    await load();
    showMessage($("page-message"), "success", "Decision recorded.");
  } catch (error) {
    showError($("decision-message"), error);
    $("decision-save").disabled = false;
  }
}

// ---- Loading ------------------------------------------------------------------------

async function load() {
  showMessage($("page-message"), "loading", "Loading duplicates…");
  try {
    const data = await api.get(`/duplicates?status=${encodeURIComponent($("filter-status").value)}`);
    $("count-open").textContent = data.counts.open;
    $("count-duplicate").textContent = data.counts.duplicate;
    $("count-not-duplicate").textContent = data.counts.not_duplicate;
    $("count-exact").textContent = data.counts.exact_files;
    $("counts").hidden = false;
    renderMatches(data.matches);
    renderExact(data.exact_files);
    clear($("page-message"));
  } catch (error) {
    showError($("page-message"), error);
  }
}

$("filter-status").addEventListener("change", load);
$("reload").addEventListener("click", load);
$("filters").addEventListener("submit", (event) => event.preventDefault());
$("decision-form").addEventListener("submit", saveDecision);
$("decision-cancel").addEventListener("click", () => $("decision-dialog").close());
load();
