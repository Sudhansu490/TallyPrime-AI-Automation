// Shared JSON API client. Every page talks to the backend only through this.
// Same-origin only: requests go to /api/v1 on the server that served the page.

const API_PREFIX = "/api/v1";
const DEFAULT_TIMEOUT_MS = 15000;

/** Error raised for network failures, timeouts and non-2xx responses. */
export class ApiError extends Error {
  /**
   * @param {string} message  Human-readable message, safe to display.
   * @param {object} [info]
   * @param {number|null} [info.status]  HTTP status, or null for network errors.
   * @param {unknown} [info.body]  Parsed JSON body of the error response, if any.
   */
  constructor(message, { status = null, body = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

function describeDetail(body) {
  // FastAPI errors are {"detail": "..."} or {"detail": [{msg, loc}, ...]}.
  const detail = body && typeof body === "object" ? body.detail : null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (d && d.msg ? String(d.msg) : String(d))).join("; ");
  }
  return null;
}

/**
 * Call a JSON endpoint.
 * @param {string} path  Path below /api/v1, e.g. "/health".
 * @param {object} [options]
 * @param {string} [options.method]
 * @param {unknown} [options.body]  Serialised as JSON when provided.
 * @param {number} [options.timeoutMs]
 * @returns {Promise<unknown>} Parsed JSON body (null for 204).
 */
export async function apiRequest(path, { method = "GET", body, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const headers = { Accept: "application/json" };
  const init = { method, headers, credentials: "same-origin", signal: controller.signal, cache: "no-store" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(`${API_PREFIX}${path}`, init);
  } catch (err) {
    const message = err && err.name === "AbortError"
      ? "The server did not respond in time."
      : "Could not reach the server. Check the network connection.";
    throw new ApiError(message);
  } finally {
    clearTimeout(timer);
  }

  if (response.status === 204) return null;

  let data = null;
  const isJson = (response.headers.get("Content-Type") || "").includes("application/json");
  if (isJson) {
    try {
      data = await response.json();
    } catch {
      throw new ApiError("The server returned an invalid response.", { status: response.status });
    }
  }

  if (!response.ok) {
    const detail = describeDetail(data);
    const message = detail ? `${detail} (HTTP ${response.status})` : `Request failed (HTTP ${response.status}).`;
    throw new ApiError(message, { status: response.status, body: data });
  }
  if (!isJson) {
    throw new ApiError("The server returned a non-JSON response.", { status: response.status });
  }
  return data;
}

export const api = {
  get: (path, options) => apiRequest(path, { ...options, method: "GET" }),
};
