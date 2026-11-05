// Processing page: document list with stage statuses, refreshed by polling,
// plus a read-only viewer for extracted text and a reprocess action for failures.
// Polling pauses while the tab is hidden and stops when nothing is in flight.

import { api } from "../api.js";
import {
  DOCUMENT_TYPE_LABELS, el, formatBytes, formatDateTime, showError, showMessage, statusBadge, clear,
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

function actionsCell(doc) {
  const cell = el("td");
  if (doc.status === "text_extracted") {
    const button = el("button", { className: "btn btn-secondary btn-small", text: "View text", attrs: { type: "button" } });
    button.addEventListener("click", () => showText(doc));
    cell.append(button);
  } else if (doc.status === "failed") {
    const button = el("button", { className: "btn btn-secondary btn-small", text: "Reprocess", attrs: { type: "button" } });
    button.addEventListener("click", () => reprocess(doc, button));
    cell.append(button);
  }
  return cell;
}

async function reprocess(doc, button) {
  const ok = window.confirm(
    `Queue “${doc.original_filename}” (#${doc.id}) for processing again?

The failed attempt stays on record.`,
  );
  if (!ok) return;
  button.disabled = true;
  try {
    await api.post(`/documents/${doc.id}/reprocess`);
    await load(); // the row now shows Queued, and polling resumes
  } catch (error) {
    showError($("list-message"), error);
    button.disabled = false;
  }
}

// ---- Extracted text viewer ------------------------------------------------------

const SOURCE_LABELS = { embedded: "Embedded PDF text", ocr: "OCR" };
const percent = (value) => (value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`);

function renderPage(page, threshold) {
  const source = page.text_source === "ocr" ? `${SOURCE_LABELS.ocr} (${page.ocr_engine})` : SOURCE_LABELS.embedded;
  const heading = el("h3", {}, [
    el("span", { text: `Page ${page.page_number}` }),
    el("span", { className: `badge badge-${page.text_source === "ocr" ? "warning" : "ok"}`, text: source }),
    el("span", { className: "muted small", text: `${page.char_count} letters/digits` }),
  ]);
  const section = el("div", { className: "text-page" }, [heading]);

  if (page.ocr_reason) section.append(el("p", { className: "small muted", text: `OCR used because ${page.ocr_reason}.` }));
  if (page.ocr_lines) {
    section.append(el("p", {
      className: "small muted",
      text: `OCR confidence: mean ${percent(page.ocr_mean_confidence)}, lowest ${percent(page.ocr_min_confidence)} · ` +
        `${page.ocr_lines.length} lines, ${page.ocr_low_confidence_lines} below ${percent(threshold)}` +
        (page.render_dpi ? ` · rendered at ${page.render_dpi} DPI` : ""),
    }));
    const low = page.ocr_lines.filter((line) => line.low_confidence);
    if (low.length) {
      section.append(el("ul", { className: "ocr-lines" }, low.map((line) => el("li", {}, [
        el("span", { className: "low-conf", text: line.text }),
        el("span", { className: "muted", text: ` — ${percent(line.confidence)}` }),
      ]))));
    }
  }
  if (page.char_count === 0) {
    section.append(el("p", { className: "alert alert-warning", text: "No readable text was found on this page." }));
  } else {
    section.append(el("pre", { className: "page-text", text: page.text }));
  }
  // Raw outputs, for checking what normalisation changed or what the other method saw.
  for (const [label, raw] of [["Raw embedded PDF text", page.embedded_text], ["Raw OCR text", page.ocr_text]]) {
    if (raw === null || raw === undefined) continue;
    section.append(el("details", {}, [
      el("summary", { className: "small", text: label }),
      el("pre", { className: "page-text", text: raw || "(empty)" }),
    ]));
  }
  return section;
}

async function showText(doc) {
  const panel = $("text-panel");
  panel.hidden = false;
  $("text-heading").textContent = `Extracted text — #${doc.id} ${doc.original_filename}`;
  $("text-meta").textContent = "";
  clear($("text-pages"));
  showMessage($("text-message"), "loading", "Loading extracted text…");
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const data = await api.get(`/documents/${doc.id}/text`);
    clear($("text-message"));
    const ocrPages = data.pages.filter((p) => p.text_source === "ocr").length;
    $("text-meta").textContent =
      `Attempt ${data.attempt} · ${data.pages.length} page${data.pages.length === 1 ? "" : "s"} · ${ocrPages} from OCR`;
    $("text-pages").append(...data.pages.map((page) => renderPage(page, data.low_confidence_threshold)));
  } catch (error) {
    showError($("text-message"), error);
  }
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
      actionsCell(doc),
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
$("text-close").addEventListener("click", () => { $("text-panel").hidden = true; });
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
