"use strict";
/* shared browser/API/runtime helpers. */

export function installRuntime(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */

  function byId(id) {
    return document.getElementById(id);
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === "className") node.className = attrs[key];
        else if (key === "text") node.textContent = attrs[key];
        else if (key.slice(0, 2) === "on" && typeof attrs[key] === "function") node.addEventListener(key.slice(2), attrs[key]);
        else node.setAttribute(key, attrs[key]);
      });
    }
    (children || []).forEach(function (child) {
      node.appendChild(child);
    });
    return node;
  }

  function renderMessage(box, text, kind) {
    box.replaceChildren();
    box.className = "message";
    if (!text) return;
    box.classList.add("toast-message");
    if (kind) box.classList.add(kind);
    box.setAttribute("role", kind === "error" ? "alert" : "status");
    box.appendChild(el("span", { className: "toast-copy", text: text }));
    var dismiss = el("button", {
      className: "toast-dismiss",
      type: "button",
      text: "\u00d7",
      "aria-label": "Dismiss notification",
    });
    dismiss.addEventListener("click", function () { renderMessage(box, ""); });
    box.appendChild(dismiss);
  }

  function showMessage(text, kind) {
    renderMessage(byId("message"), text, kind);
  }

  function reviewer() {
    return byId("reviewer-name").value.trim();
  }

  function syncReviewerIdentity() {
    var name = reviewer() || "Local procurement operator";
    var initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map(function (part) {
      return part.charAt(0).toUpperCase();
    }).join("") || "LO";
    byId("reviewer-display").textContent = name;
    byId("reviewer-avatar").textContent = initials;
    byId("reviewer-avatar").setAttribute("aria-label", "Signed in as " + name);
  }

  function requireReviewer() {
    if (reviewer()) return true;
    showMessage("Enter the reviewer name to continue.", "error");
    byId("reviewer-name").focus();
    return false;
  }

  function nowIso() {
    return new Date().toISOString();
  }

  function apiUrl(path) {
    if (/^https?:\/\//.test(path)) {
      var absolute = new URL(path);
      if (/^\/v1\/workbench\//.test(absolute.pathname)) {
        return ctx.API_BASE + absolute.pathname + absolute.search;
      }
      return path;
    }
    if (/^\/workbench\/account\/projects(\/|$)/.test(path)) {
      return ctx.API_BASE + "/v1/workbench/projects" + path.slice("/workbench/account/projects".length);
    }
    if (/^\/workbench\/(projects|jobs|field-schema)(\/|$)/.test(path)) {
      return ctx.API_BASE + "/v1" + path;
    }
    return ctx.API_BASE + path;
  }

  function apiFetch(path, options) {
    options = options || {};
    options.credentials = "include";
    var method = String(options.method || "GET").toUpperCase();
    var storageKey = "";
    if (["GET", "HEAD", "OPTIONS"].indexOf(method) === -1) {
      var headers = new Headers(options.headers || {});
      if (!headers.has("Idempotency-Key")) {
        storageKey = "procurement.idempotency." + method + "." + apiUrl(path);
        var key = "";
        try { key = window.sessionStorage.getItem(storageKey) || ""; } catch (ignore) {}
        if (!key) {
          key = window.crypto && typeof window.crypto.randomUUID === "function"
            ? window.crypto.randomUUID()
            : Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
          try { window.sessionStorage.setItem(storageKey, key); } catch (ignore) {}
        }
        headers.set("Idempotency-Key", key);
      }
      options.headers = headers;
    }
    return fetch(apiUrl(path), options).then(function (response) {
      if (storageKey && response.status < 500) {
        try { window.sessionStorage.removeItem(storageKey); } catch (ignore) {}
      }
      return response;
    });
  }

  function api(path, options) {
    return apiFetch(path, options).then(function (response) {
      var contentType = response.headers.get("content-type") || "";
      var body =
        contentType.indexOf("application/json") !== -1
          ? response.json()
          : response.text();
      return body.then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
    }).catch(function () {
      return { ok: false, status: 0, data: { detail: "The local service could not be reached. Check that the API is running." } };
    });
  }

  function apiError(data, fallback) {
    if (data && typeof data === "object" && data.detail) {
      if (typeof data.detail === "object" && data.detail.message) return String(data.detail.message);
      return String(data.detail);
    }
    if (data && data.error && data.error.message) return String(data.error.message);
    return fallback;
  }

  function apiDetails(data) {
    return data && data.error && data.error.details ? data.error.details : data || {};
  }

  function operationStorageKey(kind, scope) {
    return "procurement.operation." + kind + "." + (ctx.projectId || "unknown") + "." + (scope || "project");
  }

  function rememberOperation(kind, operationId, scope) {
    if (!operationId) return;
    try {
      window.sessionStorage.setItem(operationStorageKey(kind, scope), String(operationId));
    } catch (ignore) {}
  }

  function rememberedOperation(kind, scope) {
    try {
      return window.sessionStorage.getItem(operationStorageKey(kind, scope)) || "";
    } catch (ignore) {
      return "";
    }
  }

  function operationIdFrom(payload, kind, scope) {
    var value = payload && (
      payload.operation_id || payload.job_id || payload.operationId ||
      payload.evaluation_operation_id
    );
    return value ? String(value) : rememberedOperation(kind, scope);
  }

  function operationStatus(payload) {
    payload = payload || {};
    var status = String(payload.status || payload.state || "").toLowerCase();
    var result = payload.result && typeof payload.result === "object" ? payload.result : {};
    var resultStatus = String(result.status || result.state || "").toLowerCase();
    if ((status === "complete" || status === "succeeded") &&
        ["partial", "review_required", "completed_with_warnings"].indexOf(resultStatus) !== -1) {
      return "partial";
    }
    if (status === "complete") return "succeeded";
    if (status === "succeeded") return "succeeded";
    if (["partial", "review_required", "completed_with_warnings"].indexOf(status) !== -1) return "partial";
    if (["retry", "retrying", "retry_scheduled"].indexOf(status) !== -1) return "retrying";
    if (status === "queued" && (retryDelayMs(payload) || /retry|restart/i.test(String(payload.error_code || "")))) return "retrying";
    if (["queued", "running", "failed", "cancelled"].indexOf(status) !== -1) return status;
    return "not_started";
  }

  function operationIsActive(payload) {
    return ["queued", "running", "retrying"].indexOf(operationStatus(payload)) !== -1;
  }

  function operationIsReady(payload) {
    var status = operationStatus(payload);
    return status === "succeeded" || status === "partial";
  }

  function operationErrorText(payload, fallback) {
    payload = payload || {};
    var error = payload.safe_error || payload.error_detail || payload.error || payload.detail || payload.message;
    if (error && typeof error === "object") error = error.message || error.detail || error.title;
    var code = payload.error_code || payload.code || "";
    var detail = String(error || fallback || "The operation could not be completed.").trim();
    if (code && detail.toLowerCase().indexOf(String(code).toLowerCase()) === -1) {
      detail = String(code) + ": " + detail;
    }
    return detail.slice(0, 600);
  }

  function operationError(payload, fallback, operationId) {
    var error = new Error(operationErrorText(payload, fallback));
    error.code = payload && (payload.error_code || payload.code || "");
    error.operationId = operationId || "";
    error.payload = payload || {};
    return error;
  }

  function operationProgress(payload) {
    payload = payload || {};
    var result = payload.result && typeof payload.result === "object" ? payload.result : {};
    return payload.progress || result.progress || payload.last_checkpoint || result.last_checkpoint || {};
  }

  function retryDelayMs(payload) {
    payload = payload || {};
    var progress = operationProgress(payload);
    var raw = payload.retry_after_ms;
    if (raw == null) raw = payload.retry_after_seconds;
    if (raw == null) raw = payload.retry_after;
    if (raw == null) raw = progress.retry_after_ms;
    if (raw == null) raw = progress.retry_after_seconds;
    if (raw == null) raw = progress.retry_after;
    if (raw == null || raw === "") return 0;
    if (typeof raw === "string" && /[T:]/.test(raw)) {
      var parsedDate = Date.parse(raw);
      return Number.isFinite(parsedDate) ? Math.max(0, parsedDate - Date.now()) : 0;
    }
    var number = Number(raw);
    if (!Number.isFinite(number) || number < 0) return 0;
    if (String(raw).toLowerCase().indexOf("ms") !== -1) return Math.min(number, 86400000);
    return Math.min(number * 1000, 86400000);
  }

  function operationStatusText(payload, fallback) {
    var status = operationStatus(payload);
    var progress = operationProgress(payload);
    var stage = progress.current_stage || progress.stage || payload.current_stage || payload.stage;
    var startedAt = Date.parse(payload.started_at || payload.created_at || "");
    var elapsed = Number.isFinite(startedAt)
      ? Math.max(0, Math.floor(((payload.finished_at ? Date.parse(payload.finished_at) : Date.now()) - startedAt) / 1000))
      : 0;
    var elapsedText = elapsed >= 60
      ? Math.floor(elapsed / 60) + "m " + (elapsed % 60) + "s"
      : elapsed ? elapsed + "s" : "";
    function timed(text) { return text + (elapsedText ? " · " + elapsedText : ""); }
    var stageLabels = {
      native_text_complete: "Native text extracted",
      ocr_started: "Reading scanned pages",
      ocr_complete: "Scanned pages read",
      document_structure_started: "Organizing documents",
      llm_understanding_started: "Extracting tender fields",
      llm_understanding_complete: "Tender fields extracted",
      paddle_ocr: "Scanned pages read; understanding the bidder",
      vendor_profile: "Bidder understood; finding criterion evidence",
      evidence_retrieval: "Evidence found; verifying bidder documents",
      evidence_verification: "Evidence verified; judging each tender criterion",
      criterion_judging: "Criteria judged; finalizing the result",
      report_finalization: "Result finalized",
    };
    if (status === "retrying") {
      var delay = retryDelayMs(payload);
      return timed("Retrying safely" + (delay ? " in " + Math.ceil(delay / 1000) + "s" : "")) + "…";
    }
    if (status === "partial") return "Complete with review items. Successful work was preserved.";
    if (status === "failed") return operationErrorText(payload, fallback || "The operation failed.");
    if (status === "cancelled") return operationErrorText(payload, fallback || "The operation was cancelled.");
    if (status === "not_started") return fallback || "This operation has not started yet.";
    if (stageLabels[stage]) return timed(stageLabels[stage]) + "…";
    if (status === "queued") return timed("Queued; waiting for processing") + "…";
    if (status === "running") return timed(fallback || "Processing") + "…";
    return fallback || "Processing…";
  }

  function renderOperationActivities(hostId) {
    var host = byId(hostId);
    if (!host) return;
    var activities = Object.values(ctx.operationActivities[hostId] || {});
    host.replaceChildren();
    host.classList.toggle("operation-activity", Boolean(activities.length));
    host.setAttribute("aria-busy", activities.some(function (item) {
      return ["queued", "running", "retrying"].indexOf(item.status || operationStatus(item.payload)) !== -1;
    }) ? "true" : "false");
    activities.forEach(function (item) {
      var status = item.status || operationStatus(item.payload);
      var active = ["queued", "running", "retrying"].indexOf(status) !== -1;
      var icon = el("span", {
        className: active ? "operation-spinner" : "operation-icon",
        text: active ? "" : status === "failed" ? "!" : status === "partial" ? "!" : "✓",
        "aria-hidden": "true",
      });
      var copy = el("div", { className: "operation-copy" }, [
        el("strong", { text: item.title }),
        el("span", { text: item.detail || operationStatusText(item.payload, "Processing…") }),
      ]);
      if (item.next) copy.appendChild(el("small", { text: item.next }));
      var card = el("div", { className: "operation-progress is-" + status }, [icon, copy]);
      if (active) card.appendChild(el("span", { className: "operation-track", "aria-hidden": "true" }));
      host.appendChild(card);
    });
  }

  function showOperationActivity(hostId, key, item) {
    ctx.operationActivities[hostId] = ctx.operationActivities[hostId] || Object.create(null);
    ctx.operationActivities[hostId][key] = item;
    renderOperationActivities(hostId);
  }

  function setStatusText(hostId, text) {
    delete ctx.operationActivities[hostId];
    var host = byId(hostId);
    if (!host) return;
    host.classList.remove("operation-activity");
    host.setAttribute("aria-busy", "false");
    host.textContent = text || "";
  }

  function pollResource(key, path, options) {
    options = options || {};
    if (ctx.activePolls[key]) return ctx.activePolls[key];
    var startedAt = Date.now();
    var lastProgress = "";
    var stallShown = false;
    var failures = 0;
    var timer = null;
    var promise = new Promise(function (resolve, reject) {
      function cleanup() {
        if (timer) window.clearTimeout(timer);
        if (ctx.activePolls[key] === promise) delete ctx.activePolls[key];
      }
      function finish(callback, value) {
        cleanup();
        callback(value);
      }
      function schedule(delay) {
        timer = window.setTimeout(check, Math.max(250, delay));
      }
      function check() {
        api(path).then(function (res) {
          if (!res.ok) {
            failures += 1;
            if (failures >= ctx.POLL_FAILURE_LIMIT) {
              return finish(reject, operationError(
                res.data,
                "The operation status is temporarily unavailable. The backend job may still be running; it was not marked as lost.",
                options.operationId
              ));
            }
            if (options.onUpdate) options.onUpdate(res.data || {}, "status_unavailable");
            return schedule(Math.min(5000, 1000 * failures));
          }
          failures = 0;
          var payload = res.data || {};
          var status = operationStatus(payload);
          var progress = operationProgress(payload);
          var progressKey = JSON.stringify(progress);
          if (progressKey !== lastProgress) {
            lastProgress = progressKey;
            startedAt = Date.now();
          }
          if (options.onUpdate) options.onUpdate(payload, status);
          if (status === "failed" || status === "cancelled") {
            return finish(reject, operationError(payload, options.failureMessage, options.operationId));
          }
          if (status === "succeeded" || status === "partial") return finish(resolve, payload);
          if (!stallShown && Date.now() - startedAt >= ctx.POLL_STALL_NOTICE_MS) {
            stallShown = true;
            if (options.onStall) options.onStall(payload);
          }
          schedule(retryDelayMs(payload) || ctx.POLL_INTERVAL_MS);
        }).catch(function (errorValue) {
          failures += 1;
          if (failures >= ctx.POLL_FAILURE_LIMIT) return finish(reject, errorValue);
          schedule(Math.min(5000, 1000 * failures));
        });
      }
      check();
    });
    ctx.activePolls[key] = promise;
    promise.then(function () { if (ctx.activePolls[key] === promise) delete ctx.activePolls[key]; }, function () { if (ctx.activePolls[key] === promise) delete ctx.activePolls[key]; });
    return promise;
  }

  function accountMessage(text, kind) {
    renderMessage(byId("account-message"), text, kind);
  }

  function showRouteLoading(label) {
    byId("route-loading-title").textContent = label || "Loading workspace";
    byId("route-loading").hidden = false;
    byId("route-loading").setAttribute("aria-busy", "true");
  }

  function hideRouteLoading() {
    byId("route-loading").hidden = true;
    byId("route-loading").setAttribute("aria-busy", "false");
  }

  function showAuth(mode) {
    mode = mode === "register" ? "register" : "login";
    byId("account-shell").classList.remove("dashboard-mode");
    byId("account-shell").classList.add("auth-mode");
    byId("account-title").textContent = mode === "register" ? "Create your account" : "Welcome back";
    byId("account-subtitle").textContent = mode === "register"
      ? "Set up your profile to keep every tender and evaluation in one place."
      : "Sign in to continue a previous tender or start a new evaluation.";
    byId("account-shell").hidden = false;
    byId("auth-panel").hidden = false;
    byId("register-card").hidden = mode !== "register";
    byId("login-card").hidden = mode !== "login";
    byId("dashboard").hidden = true;
    byId("global-header").hidden = true;
    byId("app-footer").hidden = true;
    byId("workflow-header").hidden = true;
    byId("workflow-main").hidden = true;
    hideRouteLoading();
  }


  Object.assign(ctx, {
    byId: byId,
    el: el,
    renderMessage: renderMessage,
    showMessage: showMessage,
    reviewer: reviewer,
    syncReviewerIdentity: syncReviewerIdentity,
    requireReviewer: requireReviewer,
    nowIso: nowIso,
    apiUrl: apiUrl,
    apiFetch: apiFetch,
    api: api,
    apiError: apiError,
    apiDetails: apiDetails,
    operationStorageKey: operationStorageKey,
    rememberOperation: rememberOperation,
    rememberedOperation: rememberedOperation,
    operationIdFrom: operationIdFrom,
    operationStatus: operationStatus,
    operationIsActive: operationIsActive,
    operationIsReady: operationIsReady,
    operationErrorText: operationErrorText,
    operationError: operationError,
    operationProgress: operationProgress,
    retryDelayMs: retryDelayMs,
    operationStatusText: operationStatusText,
    renderOperationActivities: renderOperationActivities,
    showOperationActivity: showOperationActivity,
    setStatusText: setStatusText,
    pollResource: pollResource,
    accountMessage: accountMessage,
    showRouteLoading: showRouteLoading,
    hideRouteLoading: hideRouteLoading,
    showAuth: showAuth,
  });
}
