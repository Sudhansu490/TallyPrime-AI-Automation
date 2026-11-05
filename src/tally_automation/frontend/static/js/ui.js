// Shared UI helpers: safe DOM construction, status/error display, formatting.
//
// XSS rule: dynamic values are inserted with textContent only. This module
// never uses innerHTML, insertAdjacentHTML or document.write.

const EMPTY = "—";

/**
 * Create an element with optional class, text and children.
 * @param {string} tag
 * @param {{className?: string, text?: unknown, attrs?: Record<string,string>}} [props]
 * @param {Node[]} [children]
 */
export function el(tag, { className, text, attrs } = {}, children = []) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (attrs) {
    for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  }
  for (const child of children) node.append(child);
  return node;
}

/** Remove all children of a node. */
export function clear(node) {
  node.replaceChildren();
}

/**
 * Show a message in a container, replacing previous content.
 * @param {HTMLElement} container
 * @param {"info"|"success"|"warning"|"error"|"loading"} kind
 * @param {string} message
 */
export function showMessage(container, kind, message) {
  const role = kind === "error" ? "alert" : "status";
  container.replaceChildren(el("p", { className: `alert alert-${kind}`, text: message, attrs: { role } }));
}

/** Show an error (ApiError or anything else) without hiding the backend's message. */
export function showError(container, error) {
  const message = error && error.message ? error.message : "An unexpected error occurred.";
  showMessage(container, "error", message);
}

/**
 * Set a status badge's text and colour.
 * @param {HTMLElement} node
 * @param {"ok"|"warning"|"error"|"neutral"} state
 * @param {string} text
 */
export function setBadge(node, state, text) {
  node.replaceChildren(el("span", { className: `badge badge-${state}`, text }));
}

/** Short labels and badge colours for document, job and stage statuses. */
const STATUS_DISPLAY = {
  queued: ["Queued", "neutral"],
  processing: ["Processing", "info"],
  running: ["Running", "info"],
  processed: ["Processed", "ok"],
  succeeded: ["Succeeded", "ok"],
  failed: ["Failed", "error"],
  duplicate: ["Duplicate", "warning"],
  cancelled: ["Cancelled", "neutral"],
  pending: ["Pending", "neutral"],
  skipped: ["Skipped", "neutral"],
};

/** A badge element for a backend status value. Unknown values are shown verbatim. */
export function statusBadge(status) {
  const [label, state] = STATUS_DISPLAY[status] ?? [String(status), "neutral"];
  return el("span", { className: `badge badge-${state}`, text: label });
}

export const DOCUMENT_TYPE_LABELS = {
  purchase_invoice: "Purchase invoice",
  bank_statement: "Bank statement",
};

// ---- Formatting -----------------------------------------------------------

const inrFormatter = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const AMOUNT_PATTERN = /^-?\d+(\.\d+)?$/;

/**
 * Format an amount as Indian Rupees (lakh/crore grouping), e.g. ₹1,23,456.70.
 * The backend sends amounts as decimal strings; this is display-only and
 * never feeds back into any calculation.
 * @param {string|number|null|undefined} value
 */
export function formatINR(value) {
  if (value === null || value === undefined || value === "") return EMPTY;
  const text = String(value).trim();
  if (!AMOUNT_PATTERN.test(text)) return text; // show unexpected input as-is, never guess
  return inrFormatter.format(Number(text));
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/**
 * Format an ISO date (YYYY-MM-DD) as "04 Nov 2026". Parsed manually so the
 * browser's timezone can never shift the calendar date.
 * @param {string|null|undefined} value
 */
export function formatDate(value) {
  if (!value) return EMPTY;
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value));
  if (!match) return String(value);
  const [, year, month, day] = match;
  const monthName = MONTHS[Number(month) - 1];
  return monthName ? `${day} ${monthName} ${year}` : String(value);
}

/**
 * Format an ISO timestamp in the viewer's local time, e.g. "04 Nov 2026, 17:05".
 * @param {string|null|undefined} value
 */
export function formatDateTime(value) {
  if (!value) return EMPTY;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("en-IN", {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

/**
 * Mask an account number, keeping only the last four characters: "XXXXXX1234".
 * The backend masks too; this is a second line of defence for display.
 * @param {string|null|undefined} value
 */
export function maskAccount(value) {
  if (!value) return EMPTY;
  const text = String(value).replace(/\s+/g, "");
  if (text.length <= 4) return "X".repeat(text.length);
  return "X".repeat(text.length - 4) + text.slice(-4);
}

/** Format a byte count as "512 B", "1.4 MB". */
export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || Number.isNaN(Number(bytes))) return EMPTY;
  const n = Number(bytes);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
