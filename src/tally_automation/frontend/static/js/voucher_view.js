// Shared display of generated vouchers (Generate and Voucher Review pages).
// Display only: every decision and every check is made by the backend.

import { api } from "./api.js";
import { clear, el, formatDate, formatDateTime, formatINR } from "./ui.js";

const dash = "—";
export const KIND_LABELS = { purchase: "Purchase", payment: "Payment", receipt: "Receipt", contra: "Contra" };
const STATUS = { ready: ["Ready", "ok"], blocked: ["Blocked", "error"], superseded: ["Replaced", "neutral"] };
const APPROVAL = { pending: ["Awaiting approval", "info"], approved: ["Approved", "ok"], rejected: ["Rejected", "error"] };
const IMPORT = {
  not_imported: ["Not imported", "neutral"], importing: ["Importing…", "info"], imported: ["In TallyPrime", "ok"],
  failed: ["Import failed", "error"], uncertain: ["Import unclear", "warning"],
};
const SEVERITY = { error: ["Error", "error"], warning: ["Warning", "warning"], info: ["Info", "neutral"] };
const CATEGORY = { extraction: "Extraction", accounting: "Accounting", tally: "TallyPrime" };
const ATTEMPT = {
  sending: "sending", created: "created in TallyPrime", already_present: "already in TallyPrime (not sent again)",
  refused: "refused by TallyPrime", not_sent: "not sent", uncertain: "unclear answer",
};

const money = (v) => (/^-?\d/.test(String(v)) ? formatINR(v) : v);

export function checkItem(check) {
  const [label, kind] = check.passed ? ["OK", "ok"] : SEVERITY[check.severity];
  const item = el("li", { className: `check check-${check.passed ? "passed" : check.severity}` }, [
    el("span", { className: `badge badge-${kind}`, text: label }),
    el("span", { className: "check-category small muted", text: CATEGORY[check.category] ?? check.category }),
    el("span", { text: check.message }),
  ]);
  const numbers = [];
  if (check.calculated !== null) numbers.push(`calculated ${money(check.calculated)}`);
  if (check.printed !== null) numbers.push(`on the document ${money(check.printed)}`);
  if (check.difference !== null && !check.passed) numbers.push(`difference ${formatINR(check.difference)}`);
  if (numbers.length) item.append(el("div", { className: "small check-numbers", text: numbers.join(" · ") }));
  return item;
}

export function failingChecks(checks) {
  const order = { error: 0, warning: 1 };
  return checks.filter((c) => !c.passed && c.severity !== "info").sort((a, b) => order[a.severity] - order[b.severity]);
}

function checkLists(checks) {
  const failing = failingChecks(checks);
  const rest = checks.filter((c) => c.passed || c.severity === "info");
  const parts = [];
  if (failing.length) parts.push(el("ul", { className: "check-list" }, failing.map(checkItem)));
  parts.push(el("details", {}, [
    el("summary", { className: "small", text: `Checks that passed or are for information (${rest.length})` }),
    el("ul", { className: "check-list" }, rest.map(checkItem)),
  ]));
  return parts;
}

function entriesTable(entries) {
  return el("div", { className: "table-wrap" }, [el("table", { className: "table" }, [
    el("thead", {}, [el("tr", {}, ["Ledger", "Role", "Debit", "Credit", "Bill"].map((h) => el("th", { text: h })))]),
    el("tbody", {}, entries.map((e) => el("tr", {}, [
      el("td", { text: e.ledger }), el("td", { className: "small muted", text: e.role }),
      el("td", { className: "num", text: e.debit ? formatINR(e.debit) : "" }),
      el("td", { className: "num", text: e.credit ? formatINR(e.credit) : "" }),
      el("td", { className: "small", text: e.bill ? `${e.bill.type}${e.bill.name ? ` ${e.bill.name}` : ""}` : "" }),
    ]))),
  ])]);
}

function lazyDetails(summary, load) {
  const details = el("details", {}, [el("summary", { className: "small", text: summary })]);
  let loaded = false;
  details.addEventListener("toggle", async () => {
    if (!details.open || loaded) return;
    loaded = true;
    try {
      details.append(...(await load()));
    } catch (error) {
      details.append(el("p", { className: "error-text", text: error.message }));
    }
  });
  return details;
}

async function historyParts(id) {
  const h = await api.get(`/vouchers/${id}/history`);
  const items = [
    ...h.approvals.map((a) => ({ at: a.decided_at, text: `${a.decision}` +
      (a.warnings_acknowledged ? ` (${a.warnings_acknowledged} warning(s) acknowledged)` : "") +
      ` · device ${a.client_address ?? "unknown"}` + (a.note ? ` · “${a.note}”` : "") })),
    ...h.imports.map((i) => ({ at: i.started_at, text: `import attempt ${i.attempt}: ${ATTEMPT[i.status] ?? i.status}` +
      ` · ${i.company_name} at ${i.host}:${i.port}` + (i.tally_master_id ? ` · TallyPrime id ${i.tally_master_id}` : "") +
      (i.verified === true ? " · verified" : i.verified === false ? " · not verified" : "") +
      (i.message ? ` · ${i.message}` : "") })),
  ].sort((a, b) => String(a.at).localeCompare(String(b.at)));
  if (!items.length) return [el("p", { className: "small muted", text: "No decisions or import attempts yet." })];
  return [el("ul", { className: "small history" }, items.map((i) => el("li", { text: `${formatDateTime(i.at)} · ${i.text}` })))];
}

/** One voucher. ``actions`` are buttons supplied by the page. */
export function voucherBlock(v, { actions = [], showDocument = false } = {}) {
  const block = el("article", { className: "voucher" });
  const badges = [el("span", { className: `badge badge-${STATUS[v.status][1]}`, text: STATUS[v.status][0] })];
  if (v.status !== "superseded") {
    if (v.status === "ready" || v.approval_status !== "pending") {
      badges.push(el("span", { className: `badge badge-${APPROVAL[v.approval_status][1]}`, text: APPROVAL[v.approval_status][0] }));
    }
    if (v.import_status !== "not_imported") {
      badges.push(el("span", { className: `badge badge-${IMPORT[v.import_status][1]}`, text: IMPORT[v.import_status][0] }));
    }
  }
  block.append(el("h3", {}, [
    ...badges,
    el("span", { text: `${KIND_LABELS[v.voucher_kind]} · ${v.source_label} · ${formatDate(v.voucher_date)} · ${formatINR(v.amount)}` }),
  ]));
  const meta = el("p", { className: "small muted" }, [el("span", { text:
    `${v.company_name} · voucher type ${v.voucher_type_name ?? dash} · party ${v.party_ledger_name ?? dash}` +
    (v.reference ? ` · reference ${v.reference}` : "") +
    ` · marker ${v.marker} · ${v.error_count} error(s), ${v.warning_count} warning(s) · generated ${formatDateTime(v.generated_at)}` +
    (v.imported_at ? ` · imported ${formatDateTime(v.imported_at)}` : "") + (v.tally_master_id ? ` (TallyPrime id ${v.tally_master_id})` : "") + " · " })]);
  if (showDocument) meta.append(el("a", { text: `document #${v.document_id}`, attrs: { href: `/generate?document=${v.document_id}` } }));
  block.append(meta);
  if (v.stale_reasons.length && v.status !== "superseded" && v.import_status !== "imported") {
    block.append(el("div", { className: "alert alert-warning small" }, [
      el("strong", { text: "Out of date: " }), el("span", { text: v.stale_reasons.join(" ") }),
    ]));
  }
  if (actions.length) block.append(el("div", { className: "actions voucher-actions" }, actions));
  if (v.narration) block.append(el("p", { className: "small", text: `Narration: ${v.narration}` }));
  if (v.entries.length) block.append(entriesTable(v.entries));
  block.append(...checkLists(v.checks));
  block.append(lazyDetails("Import XML", async () => {
    const detail = await api.get(`/vouchers/${v.id}`);
    const parts = [el("pre", { className: "xml", text: detail.xml ?? "No XML: the voucher could not be built." })];
    if (detail.xml_sha256) parts.push(el("p", { className: "small muted", text: `SHA-256 ${detail.xml_sha256}` }));
    return parts;
  }));
  block.append(lazyDetails("Approval and import history", () => historyParts(v.id)));
  return block;
}

export function renderVouchers(container, vouchers, options) {
  clear(container);
  if (!vouchers.length) container.append(el("p", { className: "muted", text: "No vouchers." }));
  for (const v of vouchers) container.append(voucherBlock(v, typeof options === "function" ? options(v) : options));
}
