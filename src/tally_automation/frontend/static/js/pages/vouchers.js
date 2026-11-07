// Voucher review (Phase 8): approve, reject or revoke generated vouchers, and
// import approved ones into TallyPrime. Every action needs an explicit
// confirmation here and is re-checked by the backend, which accepts decisions
// and imports only from Computer A until login exists (Phase 9).

import { api } from "../api.js";
import { clear, el, formatINR, showError, showMessage } from "../ui.js";
import { KIND_LABELS, failingChecks, renderVouchers } from "../voucher_view.js";

const $ = (id) => document.getElementById(id);
const PAGE = 50;
const state = { permissions: null, offset: 0, items: [], pending: null };

const FILTERS = {
  awaiting: "status=ready&approval_status=pending&import_status=not_imported",
  approved: "approval_status=approved&import_status=not_imported",
  failed: "import_status=failed",
  uncertain: "import_status=uncertain",
  imported: "import_status=imported",
  rejected: "approval_status=rejected",
  blocked: "status=blocked",
  all: "",
};

// ---- Confirmation dialog -----------------------------------------------------------------

function openDialog({ title, body, confirm, note = "optional", ack = null, run }) {
  $("action-title").textContent = title;
  clear($("action-body"));
  $("action-body").append(...body);
  $("ack-wrap").hidden = !ack;
  $("ack").checked = false;
  if (ack) $("ack-text").textContent = ack;
  $("note-label").textContent = note === "required" ? "Reason (required)" : "Note (optional)";
  $("note-label").hidden = $("action-note").hidden = note === "none";
  $("action-note").value = "";
  $("action-confirm").textContent = confirm;
  $("action-confirm").disabled = false;
  clear($("action-message"));
  state.pending = { run, note, ack: Boolean(ack) };
  $("action-dialog").showModal();
}

async function confirmDialog(event) {
  event.preventDefault();
  const { run, note, ack } = state.pending;
  const text = $("action-note").value.trim();
  if (note === "required" && !text) return showMessage($("action-message"), "error", "Give a reason.");
  if (ack && !$("ack").checked) return showMessage($("action-message"), "error", "Tick the box to confirm.");
  $("action-confirm").disabled = true;
  showMessage($("action-message"), "loading", "Working…");
  try {
    const result = await run(text || null);
    const [kind, message] = typeof result === "string" ? ["success", result] : [result.kind, result.message];
    $("action-dialog").close();
    showMessage($("page-message"), kind, message);
    await load();
  } catch (error) {
    showError($("action-message"), error);
    $("action-confirm").disabled = false;
  }
}

// ---- Actions ---------------------------------------------------------------------------------

function summary(v) {
  return el("p", { text: `${KIND_LABELS[v.voucher_kind]} · ${v.source_label} of document #${v.document_id} · ` +
    `${formatINR(v.amount)} · party ${v.party_ledger_name ?? "—"} · company ${v.company_name}` });
}

function approveDialog(v) {
  const warnings = failingChecks(v.checks).filter((c) => c.severity === "warning");
  const body = [summary(v)];
  if (warnings.length) {
    body.push(el("p", { className: "small", text: "Warnings you are accepting:" }),
      el("ul", { className: "small" }, warnings.map((w) => el("li", { text: w.message }))));
  }
  openDialog({
    title: "Approve voucher", body, confirm: "Approve",
    ack: warnings.length ? `I have read the ${warnings.length} warning(s) and approve this voucher anyway.` : null,
    run: async (note) => {
      await api.post(`/vouchers/${v.id}/approve`, { acknowledge_warnings: warnings.length > 0, note });
      return "Approved. Nothing was sent to TallyPrime; import it when ready.";
    },
  });
}

function rejectDialog(v) {
  openDialog({
    title: "Reject voucher", body: [summary(v), el("p", { className: "small", text: "Fix the cause (data or ledgers) and generate it again." })],
    confirm: "Reject", note: "required",
    run: async (note) => { await api.post(`/vouchers/${v.id}/reject`, { note }); return "Rejected."; },
  });
}

function revokeDialog(v) {
  openDialog({
    title: "Withdraw approval", body: [summary(v)], confirm: "Withdraw approval",
    run: async (note) => { await api.post(`/vouchers/${v.id}/revoke`, { note }); return "Approval withdrawn."; },
  });
}

function importDialog(v) {
  const retry = v.import_status === "uncertain" || v.import_status === "failed";
  openDialog({
    title: "Import into TallyPrime",
    body: [
      summary(v),
      el("p", { className: "alert alert-warning small", text:
        `This creates the voucher in the TallyPrime company “${v.company_name}”. ` +
        "TallyPrime is first checked for this voucher's marker; if it is already there, nothing is sent." }),
      ...(retry ? [el("p", { className: "small", text: "This is a new attempt after an earlier one that did not finish cleanly." })] : []),
    ],
    confirm: "Import now", note: "none",
    ack: "I want to create this voucher in TallyPrime now.",
    run: async () => {
      const attempt = await api.post(`/vouchers/${v.id}/import`, { confirm: true }, { timeoutMs: 300000 });
      const done = attempt.status === "created" || attempt.status === "already_present";
      return { kind: done ? (attempt.verified ? "success" : "warning") : "error", message: attempt.message };
    },
  });
}

function button(text, handler, { secondary = false, disabled = false, title = "" } = {}) {
  const b = el("button", { className: `btn btn-small${secondary ? " btn-secondary" : ""}`, text, attrs: { type: "button", title } });
  b.disabled = disabled;
  b.addEventListener("click", handler);
  return b;
}

function actionsFor(v) {
  const p = state.permissions;
  if (!p?.local || v.status === "superseded") return [];
  const out = [];
  const stale = v.stale_reasons.length > 0;
  const locked = ["importing", "imported", "uncertain"].includes(v.import_status);
  if (v.status === "ready" && v.approval_status !== "approved" && !locked) {
    out.push(button("Approve…", () => approveDialog(v), { disabled: stale, title: stale ? "Out of date: generate it again" : "" }));
  }
  if (v.approval_status === "approved" && ["not_imported", "failed", "uncertain"].includes(v.import_status)) {
    out.push(button(v.import_status === "not_imported" ? "Import into TallyPrime…" : "Try import again…", () => importDialog(v),
      { disabled: !p.write_enabled || stale, title: !p.write_enabled ? "TALLY_WRITE_ENABLED is false" : stale ? "Out of date" : "" }));
  }
  if (v.approval_status === "approved" && !locked) out.push(button("Withdraw approval…", () => revokeDialog(v), { secondary: true }));
  if (v.approval_status !== "rejected" && !locked) out.push(button("Reject…", () => rejectDialog(v), { secondary: true }));
  return out;
}

// ---- Loading ----------------------------------------------------------------------------------

async function loadPermissions() {
  try {
    const p = await api.get("/vouchers/permissions");
    state.permissions = p;
    showMessage($("permissions"), p.local && p.write_enabled ? "success" : "warning", p.message);
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
    renderVouchers($("vouchers"), state.items, (v) => ({ actions: actionsFor(v), showDocument: true }));
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
$("action-form").addEventListener("submit", confirmDialog);
$("action-cancel").addEventListener("click", () => $("action-dialog").close());

const initial = new URLSearchParams(window.location.search).get("show");
if (initial && FILTERS[initial] !== undefined) $("filter").value = initial;
await loadPermissions();
load();
