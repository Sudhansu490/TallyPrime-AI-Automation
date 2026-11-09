// Voucher review: generated vouchers grouped by document, with ONE action per
// document, "Approve & import into TallyPrime". The approver's click (confirmed
// once in a dialog) approves every ready voucher of the document and imports it
// straight away; the backend re-checks the role, TALLY_WRITE_ENABLED, staleness
// and TallyPrime's markers so nothing is ever posted twice.

import { api } from "../api.js";
import { clear, el, formatINR, showError, showMessage } from "../ui.js";
import { approveImportButton, voucherBlock } from "../voucher_view.js";

const $ = (id) => document.getElementById(id);
const PAGE = 50;
const state = { permissions: null, offset: 0, items: [] };

const FILTERS = {
  awaiting: "status=ready&approval_status=pending&import_status=not_imported",
  failed: "import_status=failed",
  uncertain: "import_status=uncertain",
  imported: "import_status=imported",
  rejected: "approval_status=rejected",
  blocked: "status=blocked",
  all: "",
};

function done(kind, message) {
  showMessage($("page-message"), kind, message);
  load();
}

function render() {
  const container = $("vouchers");
  clear(container);
  if (!state.items.length) {
    container.append(el("p", { className: "muted", text: "No vouchers." }));
    return;
  }
  const groups = new Map();
  for (const v of state.items) {
    if (!groups.has(v.document_id)) groups.set(v.document_id, []);
    groups.get(v.document_id).push(v);
  }
  for (const [documentId, vouchers] of groups) {
    const total = vouchers.reduce((sum, v) => sum + Number(v.amount ?? 0), 0);
    const section = el("section", { className: "document-group" }, [
      el("div", { className: "section-header" }, [
        el("h2", {}, [
          el("a", { text: `Document #${documentId}`, attrs: { href: `/generate?document=${documentId}` } }),
          el("span", { className: "small muted", text: ` · ${vouchers.length} voucher(s) · ${formatINR(total)}` }),
        ]),
        approveImportButton(documentId, vouchers, state.permissions, done),
      ]),
    ]);
    const list = el("details", {}, [el("summary", { className: "small", text: "Show the vouchers" })]);
    for (const v of vouchers) list.append(voucherBlock(v));
    section.append(list);
    container.append(section);
  }
}

async function loadPermissions() {
  try {
    const p = await api.get("/vouchers/permissions");
    state.permissions = p;
    showMessage($("permissions"), p.can_approve && p.write_enabled ? "success" : "info", p.message);
  } catch (error) {
    showError($("permissions"), error);
  }
}

async function load({ more = false } = {}) {
  if (!more) state.offset = 0;
  const filter = FILTERS[$("filter").value] ?? "";
  try {
    const page = await api.get(`/vouchers?limit=${PAGE}&offset=${state.offset}${filter ? `&${filter}` : ""}`);
    state.items = more ? state.items.concat(page) : page;
    render();
    $("more").hidden = page.length < PAGE;
  } catch (error) {
    showError($("page-message"), error);
  }
}

// The result of the last action stays visible until the reviewer changes the view.
$("filter").addEventListener("change", () => { clear($("page-message")); load(); });
$("reload").addEventListener("click", async () => { clear($("page-message")); await loadPermissions(); load(); });
$("filters").addEventListener("submit", (e) => e.preventDefault());
$("more").addEventListener("click", () => { state.offset += PAGE; load({ more: true }); });

const initial = new URLSearchParams(window.location.search).get("show");
if (initial && FILTERS[initial] !== undefined) $("filter").value = initial;
await loadPermissions();
load();
