// Upload page: client-side pre-check, then sequential uploads with progress.
// The pre-check only saves a round trip; the backend re-validates everything.

import { api, uploadForm } from "../api.js";
import { DOCUMENT_TYPE_LABELS, el, formatBytes, showError, showMessage, clear } from "../ui.js";

const MAX_FILES_PER_BATCH = 50;

const state = {
  limits: null,   // from /documents/upload-limits
  queue: [],      // {id, file, documentType, status, message, progress, row}
  uploading: false,
  nextId: 1,
};

const $ = (id) => document.getElementById(id);

function selectedType() {
  return document.querySelector('input[name="document_type"]:checked').value;
}

function extensionOf(name) {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot).toLowerCase() : "";
}

function precheck(file, documentType) {
  const allowed = state.limits.allowed_extensions[documentType];
  const ext = extensionOf(file.name);
  if (!allowed.includes(ext)) {
    return `${ext || "Files without an extension"} not accepted for ${DOCUMENT_TYPE_LABELS[documentType].toLowerCase()}s (allowed: ${allowed.join(", ")}).`;
  }
  if (file.size === 0) return "The file is empty.";
  if (file.size > state.limits.max_size_bytes) {
    return `Larger than the ${formatBytes(state.limits.max_size_bytes)} limit.`;
  }
  return null;
}

function updateHints() {
  if (!state.limits) return;
  const type = selectedType();
  const allowed = state.limits.allowed_extensions[type];
  $("type-hint").textContent =
    `${DOCUMENT_TYPE_LABELS[type]}s: ${allowed.join(", ")} · up to ${formatBytes(state.limits.max_size_bytes)} per file` +
    ` · PDFs up to ${state.limits.max_pdf_pages} pages.`;
  $("dropzone-hint").textContent = `Accepted: ${allowed.join(", ")}`;
  $("file-input").setAttribute("accept", allowed.join(","));
}

// ---- Queue rendering --------------------------------------------------------

function renderResult(item) {
  const cell = item.row.resultCell;
  clear(cell);
  const badgeFor = {
    ready: ["Ready", "neutral"], rejected: ["Not allowed", "error"], uploading: ["Uploading", "info"],
    accepted: ["Accepted", "ok"], duplicate: ["Duplicate", "warning"], failed: ["Rejected", "error"],
  };
  const [label, badgeState] = badgeFor[item.status];
  cell.append(el("span", { className: `badge badge-${badgeState}`, text: label }));
  if (item.message) cell.append(el("div", { className: "small", text: item.message }));
  if (item.documentId) {
    // Opens the Processing page on exactly this uploaded document.
    cell.append(el("div", { className: "small muted", text: `S.No ${item.serial} · document #${item.documentId}` }));
    cell.append(el("a", {
      className: "btn btn-small", text: "Processing",
      attrs: { href: `/processing?document=${item.documentId}`, title: `Show the processing of document #${item.documentId}` },
    }));
  }
  item.row.progress.value = item.progress;
  item.row.progress.hidden = item.status === "rejected";
}

function addRow(item) {
  const progress = el("progress", { attrs: { max: "1", value: "0", "aria-label": `Upload progress for ${item.file.name}` } });
  const resultCell = el("td");
  const tr = el("tr", {}, [
    el("td", { className: "filename", text: item.file.name }),
    el("td", { text: DOCUMENT_TYPE_LABELS[item.documentType] }),
    el("td", { text: formatBytes(item.file.size) }),
    el("td", {}, [progress]),
    resultCell,
  ]);
  item.row = { tr, progress, resultCell };
  $("queue-body").append(tr);
  renderResult(item);
}

function refreshControls() {
  const pending = state.queue.filter((i) => i.status === "ready").length;
  const finished = state.queue.filter((i) => !["ready", "uploading"].includes(i.status)).length;
  $("start-upload").disabled = state.uploading || pending === 0;
  $("start-upload").textContent = pending > 0 ? `Upload ${pending} file${pending === 1 ? "" : "s"}` : "Upload";
  $("clear-queue").disabled = state.uploading || finished === 0;
  $("queue-empty").hidden = state.queue.length > 0;
  $("queue-table-wrap").hidden = state.queue.length === 0;
  for (const input of document.querySelectorAll('input[name="document_type"]')) input.disabled = state.uploading;
  $("file-input").disabled = state.uploading || !state.limits;
}

function addFiles(fileList) {
  if (!state.limits || state.uploading) return;
  const files = Array.from(fileList);
  const room = MAX_FILES_PER_BATCH - state.queue.filter((i) => i.status === "ready").length;
  if (files.length > room) {
    showMessage($("queue-message"), "warning", `At most ${MAX_FILES_PER_BATCH} files per batch; ${files.length - Math.max(room, 0)} file(s) were not added.`);
  } else {
    clear($("queue-message"));
  }
  const documentType = selectedType();
  for (const file of files.slice(0, Math.max(room, 0))) {
    const problem = precheck(file, documentType);
    const item = {
      id: state.nextId++, file, documentType, progress: 0, documentId: null,
      status: problem ? "rejected" : "ready", message: problem,
    };
    state.queue.push(item);
    addRow(item);
  }
  refreshControls();
}

// ---- Uploading ----------------------------------------------------------------

async function uploadOne(item) {
  item.status = "uploading";
  item.message = null;
  renderResult(item);
  const form = new FormData();
  form.append("document_type", item.documentType);
  form.append("file", item.file, item.file.name);
  try {
    const doc = await uploadForm("/documents", form, {
      onProgress: (fraction) => { item.row.progress.value = fraction; },
    });
    item.progress = 1;
    item.documentId = doc.id;
    item.serial = doc.serial ?? doc.id;
    if (doc.status === "duplicate") {
      item.status = "duplicate";
      const original = doc.duplicate.duplicate_of;
      item.message = `Identical to document #${original.id} (“${original.original_filename}”). Not processed again.`;
    } else {
      item.status = "accepted";
      item.message = `Queued for processing.`;
    }
  } catch (error) {
    item.status = "failed";
    item.progress = 0;
    item.message = error.message; // the backend's reason, unaltered
  }
  renderResult(item);
}

async function startUpload() {
  state.uploading = true;
  refreshControls();
  // Sequential: keeps per-file progress meaningful and avoids SQLite write contention.
  for (const item of state.queue.filter((i) => i.status === "ready")) {
    await uploadOne(item);
  }
  state.uploading = false;
  refreshControls();
  const counts = { accepted: 0, duplicate: 0, failed: 0 };
  for (const item of state.queue) if (item.status in counts) counts[item.status] += 1;
  const kind = counts.failed > 0 ? "warning" : "success";
  showMessage($("queue-message"), kind,
    `Finished: ${counts.accepted} accepted, ${counts.duplicate} duplicate, ${counts.failed} rejected.`);
}

function clearFinished() {
  state.queue = state.queue.filter((item) => {
    const keep = ["ready", "uploading"].includes(item.status);
    if (!keep) item.row.tr.remove();
    return keep;
  });
  clear($("queue-message"));
  refreshControls();
}

// ---- Wiring -------------------------------------------------------------------

function wireDropzone() {
  const zone = $("dropzone");
  for (const type of ["dragenter", "dragover"]) {
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      if (!state.uploading) zone.classList.add("dropzone-active");
    });
  }
  for (const type of ["dragleave", "drop"]) {
    zone.addEventListener(type, () => zone.classList.remove("dropzone-active"));
  }
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    if (event.dataTransfer && event.dataTransfer.files.length) addFiles(event.dataTransfer.files);
  });
  // Stop a file dropped outside the zone from navigating away from the page.
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());
}

async function init() {
  refreshControls();
  try {
    state.limits = await api.get("/documents/upload-limits");
  } catch (error) {
    showError($("limits-message"), error);
    return;
  }
  updateHints();
  refreshControls();
}

for (const radio of document.querySelectorAll('input[name="document_type"]')) {
  radio.addEventListener("change", updateHints);
}
$("file-input").addEventListener("change", (event) => {
  addFiles(event.target.files);
  event.target.value = ""; // allow re-selecting the same file
});
$("start-upload").addEventListener("click", startUpload);
$("clear-queue").addEventListener("click", clearFinished);
window.addEventListener("beforeunload", (event) => {
  if (state.uploading) event.preventDefault();
});
wireDropzone();
init();
