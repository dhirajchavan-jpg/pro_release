import { apiConfig } from "../config/api.js";

export class ApiError extends Error {
  constructor(message, { status = 0, data = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

function readResponseBody(response) {
  if (response.status === 204) {
    return Promise.resolve(null);
  }

  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("application/json") ? response.json() : response.text();
}

function errorMessage(data, status) {
  const detail = data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") return detail.message || detail.code || JSON.stringify(detail);
  if (typeof data?.message === "string") return data.message;
  return `Request failed with status ${status}.`;
}

export async function apiRequest(path, options = {}) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), apiConfig.timeoutMs);
  const isFormData = options.body instanceof FormData;
  const headers = new Headers(options.headers);
  const method = String(options.method || "GET").toUpperCase();

  if (!isFormData && options.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  if (!["GET", "HEAD", "OPTIONS"].includes(method) && !headers.has("Idempotency-Key")) {
    headers.set("Idempotency-Key", window.crypto.randomUUID());
  }

  try {
    const response = await fetch(`${apiConfig.baseUrl}${path}`, {
      ...options,
      method,
      headers,
      credentials: "include",
      signal: options.signal || controller.signal,
    });
    const data = await readResponseBody(response);

    if (!response.ok) {
      const message = errorMessage(data, response.status);
      throw new ApiError(message, { status: response.status, data });
    }

    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new ApiError(`Request timed out after ${apiConfig.timeoutMs} ms.`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

export async function downloadRequest(path) {
  const response = await fetch(`${apiConfig.baseUrl}${path}`, { credentials: "include" });
  if (!response.ok) {
    let data = null;
    try { data = await readResponseBody(response); } catch { data = null; }
    throw new ApiError(errorMessage(data, response.status), { status: response.status, data });
  }
  return response.blob();
}

export const api = Object.freeze({
  get: (path, options) => apiRequest(path, { ...options, method: "GET" }),
  post: (path, body, options) => apiRequest(path, {
    ...options,
    method: "POST",
    body: body instanceof FormData ? body : JSON.stringify(body),
  }),
  patch: (path, body, options) => apiRequest(path, {
    ...options,
    method: "PATCH",
    body: body instanceof FormData ? body : JSON.stringify(body),
  }),
  delete: (path, options) => apiRequest(path, { ...options, method: "DELETE" }),
});
