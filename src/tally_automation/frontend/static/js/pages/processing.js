// Processing page: document list with stage statuses, refreshed by polling.
// Polling pauses while the tab is hidden and stops when nothing is in flight.

import { api } from "../api.js";
import {
  DOCUMENT_TYPE_LABELS, el, formatBytes, formatDateTime, showError, statusBadge, clear,
} from "../ui.js";

const PAGE_SIZE = 50;
const POLL_INTERVAL_MS = 5000;
const IN_FLIGHT = new Set(["queued", "processing"]);

const state = { offset: 0, total: 0, timer: null, loading: false, lastLoaded: null };
const $ = (id) => document.getElementById(id);

function query() {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(state.offset) });
  const status = $("filter-status").value;
  const type = $("filter-type").value;
  if (status) params.set("status", status);
  if (type) params.set("document_type", type);
  return `/documents?${params}`;
}

function stageCell(job, stage) {
  return el("td", {}, [job ? statusBadge(job.stages[stage]) : el("span", { className: "muted", text: "—" })]);
}

function detailsCell(doc) {
  const cell = el("td", { className: "details" });
  const job = doc.latest_job;
  if (doc.duplicate) {
    cell.append(el("div", { text: doc.duplicate.description }));
    cell.append(el("div", {
      className: "small muted",
      text: `Original: “${doc.duplicate.duplicate_of.original_filename}”, uploaded ${formatDateTime(doc.duplicate.duplicate_of.uploaded_at)}`,
    }));
  } else if (job && job.error_message) {
    const where = job.error_stage ? `${job.error_stage.replace("_", " ")}: ` : "";
    cell.append(el("div", { className: "error-text", text: `${where}${job.error_message}` }));
  }
  cell.append(el("div", {
    className: "small muted",
    text: `${formatBytes(doc.file.size_bytes)} · ${doc.file.page_count} page${doc.file.page_count === 1 ? "" : "s"} · SHA-256 ${doc.file.sha256.slice(0, 12)}…`,
    attrs: { title: `SHA-256 ${doc.file.sha256}` },
  }));
  return cell;
}

function renderRows(items) {
  const body = $("list-body");
  clear(body);
  for (const doc of items) {
    const job = doc.latest_job;
    body.append(el("tr", {}, [
      el("td", { text: doc.id }),
      el("td", { className: "filename", text: doc.original_filename }),
      el("td", { text: DOCUMENT_TYPE_LABELS[doc.document_type] ?? doc.document_type }),
      el("td", { text: formatDateTime(doc.uploaded_at) }),
      el("td", {}, [statusBadge(doc.status)]),
      stageCell(job, "text_extraction"),
      stageCell(job, "ocr"),
      stageCell(job, "ai_extraction"),
      stageCell(job, "validation"),
      detailsCell(doc),
    ]));
  }
  $("list-wrap").hidden = items.length === 0;
  $("list-empty").hidden = items.length !== 0;
}

function renderPager() {
  const from = state.total === 0 ? 0 : state.offset + 1;
  const to = Math.min(state.offset + PAGE_SIZE, state.total);
  $("page-info").textContent = `${from}–${to} of ${state.total}`;
  $("prev-page").disabled = state.offset === 0;
  $("next-page").disabled = state.offset + PAGE_SIZE >= state.total;
}

function schedule(items) {
  clearTimeout(state.timer);
  state.timer = null;
  const anyInFlight = items.some((d) => IN_FLIGHT.has(d.status));
  const polling = anyInFlight && document.visibilityState === "visible";
  if (polling) state.timer = setTimeout(load, POLL_INTERVAL_MS);
  const when = state.lastLoaded ? `Updated ${state.lastLoaded.toLocaleTimeString("en-IN", { hour12: false })}` : "";
  $("refresh-info").textContent = polling ? `${when} · auto-refresh every 5 s` : when;
}

let lastItems = [];

async function load() {
  if (state.loading) return;
  state.loading = true;
  $("refresh").disabled = true;
  try {
    const data = await api.get(query());
    state.total = data.total;
    state.lastLoaded = new Date();
    lastItems = data.items;
    clear($("list-message"));
    renderRows(data.items);
    renderPager();
  } catch (error) {
    // Keep the last good table visible, but never hide the error.
    showError($("list-message"), error);
  } finally {
    state.loading = false;
    $("refresh").disabled = false;
    schedule(lastItems);
  }
}

function resetAndLoad() {
  state.offset = 0;
  load();
}

$("refresh").addEventListener("click", load);
$("filter-status").addEventListener("change", resetAndLoad);
$("filter-type").addEventListener("change", resetAndLoad);
$("filters").addEventListener("submit", (event) => event.preventDefault());
$("prev-page").addEventListener("click", () => { state.offset = Math.max(0, state.offset - PAGE_SIZE); load(); });
$("next-page").addEventListener("click", () => { state.offset += PAGE_SIZE; load(); });
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") load();
  else schedule(lastItems);
});
load();
