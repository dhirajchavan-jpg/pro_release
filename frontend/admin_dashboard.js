"use strict";

(function () {
  var account = null;
  var selectedProject = new URLSearchParams(window.location.search).get("project_id") || "";

  function byId(id) { return document.getElementById(id); }
  function node(tag, text, className) {
    var item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  }
  function api(path, options) {
    return fetch(path, Object.assign({ credentials: "include" }, options || {})).then(function (response) {
      if (response.status === 401) { window.location.href = "/workbench/login"; throw new Error("Sign in required"); }
      return response.json().then(function (data) {
        if (!response.ok) throw new Error((data.error && data.error.message) || data.detail || "Request failed");
        return data;
      });
    });
  }
  function number(value) { return Number(value || 0).toLocaleString("en-IN"); }
  function percentage(value) {
    var numeric = Number(value);
    return value !== null && value !== undefined && value !== "" && Number.isFinite(numeric)
      ? Math.max(0, numeric).toFixed(1) + "%"
      : "—";
  }
  function budgetShare(value, budget) {
    var limit = Number(budget && budget.limit_inr);
    var amount = Number(value);
    return budget && budget.enabled && value !== null && value !== undefined && value !== "" &&
      Number.isFinite(amount) && Number.isFinite(limit) && limit > 0
      ? percentage(amount / limit * 100)
      : "—";
  }
  function duration(ms) {
    var seconds = Math.round(Number(ms || 0) / 1000);
    if (seconds < 60) return seconds + "s";
    return Math.floor(seconds / 60) + "m " + (seconds % 60) + "s";
  }
  function date(value) {
    return value ? new Date(value).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "—";
  }
  function compact(value) {
    if (value === null || value === undefined || value === "") return "Not set";
    var text = typeof value === "string" ? value : JSON.stringify(value);
    return text.length > 220 ? text.slice(0, 217) + "…" : text;
  }
  function words(value) {
    return String(value || "Activity").replace(/^workbench\./, "").replace(/[._-]+/g, " ").replace(/\b\w/g, function (letter) { return letter.toUpperCase(); });
  }
  function changedFields(before, after) {
    before = before && typeof before === "object" ? before : {};
    after = after && typeof after === "object" ? after : {};
    return Array.from(new Set(Object.keys(before).concat(Object.keys(after)))).filter(function (key) {
      return JSON.stringify(before[key]) !== JSON.stringify(after[key]);
    }).map(function (key) {
      return { key: words(key), before: before[key], after: after[key] };
    });
  }
  function setMessage(message) {
    byId("message").hidden = !message;
    byId("message").textContent = message || "";
  }

  function renderProjects(projects) {
    var select = byId("project");
    select.replaceChildren(new Option("All tenders", ""));
    var body = byId("projects");
    body.replaceChildren();
    projects.forEach(function (project) {
      select.add(new Option(project.title || project.tender_number || project.project_id, project.project_id));
      var row = document.createElement("tr");
      var title = node("td");
      title.append(node("strong", project.title || "Untitled tender"), node("small", project.tender_number || project.project_id));
      var bidders = project.portfolio_summary ? project.portfolio_summary.bidder_count : (project.bidders || []).length;
      [title, node("td", words(project.stage)), node("td", number(bidders)), node("td", date(project.updated_at))].forEach(function (cell) { row.appendChild(cell); });
      row.addEventListener("click", function () { select.value = project.project_id; select.dispatchEvent(new Event("change")); });
      body.appendChild(row);
    });
    if (selectedProject && projects.some(function (project) { return project.project_id === selectedProject; })) select.value = selectedProject;
    else selectedProject = "";
  }

  function renderUsage(data) {
    var summary = data.summary || {};
    var budget = data.budget || {};
    var used = Number(budget.percent_used);
    var hasBudgetPercent = budget.enabled && budget.percent_used !== null && budget.percent_used !== undefined &&
      budget.percent_used !== "" && Number.isFinite(used);
    byId("spent").textContent = hasBudgetPercent ? percentage(used) : "—";
    byId("budget-used").textContent = budget.enabled ? "of account budget" : "Budget tracking disabled";
    byId("remaining").textContent = hasBudgetPercent ? percentage(100 - Math.max(0, used)) : "—";
    byId("budget-limit").textContent = budget.enabled ? "available share" : "Budget tracking disabled";
    byId("tokens").textContent = number(summary.total_tokens);
    byId("token-split").textContent = number(summary.input_tokens) + " in · " + number(summary.output_tokens) + " out";
    byId("calls").textContent = number(summary.call_count);
    byId("cache-hits").textContent = number(summary.cache_hits) + " cache hits";
    byId("failures").textContent = number(summary.failed_calls + summary.budget_blocked_calls);
    byId("retries").textContent = number(summary.retry_count) + " retries";

    var stages = byId("stages");
    stages.replaceChildren();
    (data.stages || []).forEach(function (stage) {
      var units = stage.total_tokens ? number(stage.total_tokens) + " tokens" : number(stage.input_units) + " " + (stage.input_units === 1 ? "unit" : "units");
      var row = document.createElement("tr");
      [words(stage.stage), number(stage.call_count), units, budgetShare(stage.estimated_cost_inr, budget)].forEach(function (value) { row.appendChild(node("td", value)); });
      stages.appendChild(row);
    });
    if (!(data.stages || []).length) stages.appendChild(node("tr")).appendChild(node("td", "No provider usage recorded."));

    var calls = byId("recent-calls");
    calls.replaceChildren();
    (data.recent_calls || []).forEach(function (call) {
      var row = document.createElement("tr");
      var provider = node("td");
      provider.append(node("strong", call.provider || "—"), node("small", call.model || "—"));
      var statusCell = node("td");
      statusCell.appendChild(node("span", words(call.status), "status " + String(call.status || "")));
      var units = call.total_tokens ? number(call.total_tokens) + " tokens" : number(call.input_units) + " " + (call.unit_name || "units");
      [node("td", date(call.started_at)), node("td", words(call.stage)), node("td", words(call.capability)), provider, statusCell, node("td", units), node("td", duration(call.duration_ms)), node("td", call.priced ? budgetShare(call.estimated_cost_inr, budget) : "—")].forEach(function (cell) { row.appendChild(cell); });
      calls.appendChild(row);
    });
    byId("call-count").textContent = number((data.recent_calls || []).length) + " shown";
  }

  function renderAuditIntegrity(data) {
    var status = byId("audit-status");
    status.textContent = data.valid ? "Verified" : "Check failed";
    status.className = data.valid ? "good" : "bad";
    byId("audit-events").textContent = number(data.event_count) + " protected events";
  }

  function renderActivity(data) {
    var records = (data && data.records) || [];
    var list = byId("activity");
    list.replaceChildren();
    byId("activity-count").textContent = number(records.length) + " events";
    byId("activity-empty").hidden = records.length !== 0;
    byId("activity-empty").textContent = selectedProject ? "No activity has been recorded for this tender." : "Select a tender to view its activity.";
    records.slice().reverse().forEach(function (record) {
      var item = node("li");
      item.appendChild(node("time", date(record.recorded_at)));
      var copy = node("div", null, "event-copy");
      copy.appendChild(node("strong", words(record.reason_code || record.action)));
      if (record.reason) copy.appendChild(node("p", record.reason));
      var changes = changedFields(record.before, record.after);
      changes.slice(0, 4).forEach(function (change) {
        var line = node("div", null, "change");
        line.append(node("b", change.key), node("span", compact(change.before) + " → " + compact(change.after)));
        copy.appendChild(line);
      });
      if (!changes.length && record.after) copy.appendChild(node("p", compact(record.after)));
      copy.appendChild(node("span", (record.actor_role === "system" ? "System" : "User") + " · " + words(record.entity_type || record.subject_artifact_name), "event-meta"));
      item.appendChild(copy);
      list.appendChild(item);
    });
  }

  function load() {
    setMessage("");
    byId("refresh").disabled = true;
    var usagePath = "/v1/workbench/usage?limit=100" + (selectedProject ? "&project_id=" + encodeURIComponent(selectedProject) : "");
    var activity = selectedProject ? api("/v1/workbench/projects/" + encodeURIComponent(selectedProject) + "/review-log") : Promise.resolve({ records: [] });
    return Promise.all([api("/workbench/account"), api(usagePath), api("/v1/workbench/audit/verify"), activity]).then(function (results) {
      account = results[0];
      renderProjects(account.projects || []);
      renderUsage(results[1]);
      renderAuditIntegrity(results[2]);
      renderActivity(results[3]);
      byId("updated").textContent = "Updated " + new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    }).catch(function (error) {
      setMessage(error.message || "Dashboard data could not be loaded.");
    }).finally(function () { byId("refresh").disabled = false; });
  }

  byId("project").addEventListener("change", function () {
    selectedProject = this.value;
    var url = new URL(window.location.href);
    if (selectedProject) url.searchParams.set("project_id", selectedProject); else url.searchParams.delete("project_id");
    window.history.replaceState({}, "", url);
    load();
  });
  byId("refresh").addEventListener("click", load);
  byId("logout").addEventListener("click", function () {
    fetch("/workbench/account/logout", { method: "POST", credentials: "include" }).finally(function () { window.location.href = "/workbench/login"; });
  });
  load();
})();
