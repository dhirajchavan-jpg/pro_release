"use strict";
/* tender upload and understanding setup. */

export function installTenderSetup(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var api = function () { return ctx.api.apply(ctx, arguments); };
  var apiError = function () { return ctx.apiError.apply(ctx, arguments); };
  var applyPageMode = function () { return ctx.applyPageMode.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var hideRouteLoading = function () { return ctx.hideRouteLoading.apply(ctx, arguments); };
  var loadActivePage = function () { return ctx.loadActivePage.apply(ctx, arguments); };
  var loadLedger = function () { return ctx.loadLedger.apply(ctx, arguments); };
  var nowIso = function () { return ctx.nowIso.apply(ctx, arguments); };
  var operationErrorText = function () { return ctx.operationErrorText.apply(ctx, arguments); };
  var operationIdFrom = function () { return ctx.operationIdFrom.apply(ctx, arguments); };
  var operationIsActive = function () { return ctx.operationIsActive.apply(ctx, arguments); };
  var operationStatus = function () { return ctx.operationStatus.apply(ctx, arguments); };
  var pageUrl = function () { return ctx.pageUrl.apply(ctx, arguments); };
  var pollResource = function () { return ctx.pollResource.apply(ctx, arguments); };
  var recommendedPage = function () { return ctx.recommendedPage.apply(ctx, arguments); };
  var recordStepView = function () { return ctx.recordStepView.apply(ctx, arguments); };
  var refreshState = function () { return ctx.refreshState.apply(ctx, arguments); };
  var rememberOperation = function () { return ctx.rememberOperation.apply(ctx, arguments); };
  var requireReviewer = function () { return ctx.requireReviewer.apply(ctx, arguments); };
  var reviewer = function () { return ctx.reviewer.apply(ctx, arguments); };
  var routeProject = function () { return ctx.routeProject.apply(ctx, arguments); };
  var setStatusText = function () { return ctx.setStatusText.apply(ctx, arguments); };
  var showMessage = function () { return ctx.showMessage.apply(ctx, arguments); };
  var showOperationActivity = function () { return ctx.showOperationActivity.apply(ctx, arguments); };
  var showProjectContext = function () { return ctx.showProjectContext.apply(ctx, arguments); };
  var updateStepper = function () { return ctx.updateStepper.apply(ctx, arguments); };
  // ---------------------------------------------------------------- Step 1

  function showSetupTab(name) {
    var schemaActive = name === "schema";
    byId("schema-setup-panel").hidden = !schemaActive;
    byId("schema-setup-tab").setAttribute("aria-expanded", schemaActive ? "true" : "false");
    byId("schema-setup-tab").textContent = schemaActive ? "Hide custom fields" : "Use custom fields";
  }

  function schemaSummary(schema) {
    if (!schema || schema.schema_version !== "rfp-field-schema.v1" || !Array.isArray(schema.sections) || !schema.sections.length) {
      throw new Error("Use an rfp-field-schema.v1 JSON object with at least one section.");
    }
    var count = schema.sections.reduce(function (total, section) {
      if (!section || !Array.isArray(section.fields) || !section.fields.length) {
        throw new Error("Every schema section must contain at least one field.");
      }
      return total + section.fields.length;
    }, 0);
    return { schema_id: schema.schema_id || "custom-schema", field_count: count };
  }

  function displaySchema(info, custom) {
    byId("schema-mode-label").textContent = custom ? "Custom tender fields" : "Standard tender fields";
    byId("schema-count").textContent = info.field_count + " fields";
    byId("schema-selection").textContent = (info.schema_id || "Field schema") + " · " + info.field_count + " fields";
    byId("selected-schema-name").textContent = (info.schema_id || "Field schema") + " · " + info.field_count + " fields";
    byId("schema-status").textContent = custom
      ? "Client schema selected. It will be validated again and pinned when the project is created."
      : "The standard organization schema will be used unless you provide a client JSON.";
  }

  function useSchemaText(text) {
    try {
      var parsed = JSON.parse(text);
      var info = schemaSummary(parsed);
      ctx.selectedSchemaJson = JSON.stringify(parsed);
      byId("field-schema-json").value = JSON.stringify(parsed, null, 2);
      displaySchema(info, true);
    } catch (error) {
      ctx.selectedSchemaJson = null;
      byId("schema-status").textContent = error.message || "The field schema JSON is invalid.";
    }
  }

  function useDefaultSchema() {
    ctx.selectedSchemaJson = null;
    byId("field-schema-file").value = "";
    byId("field-schema-json").value = "";
    if (ctx.defaultSchemaInfo) displaySchema(ctx.defaultSchemaInfo, false);
  }

  function loadDefaultSchemaInfo() {
    api("/workbench/field-schema/default").then(function (res) {
      if (!res.ok) return;
      ctx.defaultSchemaInfo = res.data;
      if (!ctx.selectedSchemaJson) displaySchema(ctx.defaultSchemaInfo, false);
    });
  }

  function loadProjectSchema() {
    if (!ctx.projectId) return;
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/field-schema").then(function (res) {
      if (res.ok) displaySchema(res.data, res.data.schema_id !== (ctx.defaultSchemaInfo && ctx.defaultSchemaInfo.schema_id));
    });
  }

  function createProject() {
    var file = byId("tender-file").files[0];
    if (!file) return showMessage("Choose a tender PDF or ZIP.", "error");
    var button = byId("create-project-btn");
    var fileName = file.name;
    var form = new FormData();
    form.append("file", file);
    if (ctx.selectedSchemaJson) form.append("schema_json", ctx.selectedSchemaJson);
    form.append("policy_audit", byId("policy-audit-toggle").checked ? "1" : "0");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.classList.add("button-loading");
    button.textContent = "Uploading tender…";
    showOperationActivity("understand-status", "understanding", {
      title: "Uploading tender package",
      status: "running",
      detail: "Creating the tender workspace…",
      next: "Analysis will start automatically after the upload completes.",
    });
    api("/workbench/projects", { method: "POST", body: form }).then(function (res) {
      if (!res.ok) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.classList.remove("button-loading");
        button.textContent = "Create and analyze tender";
        showOperationActivity("understand-status", "understanding", {
          title: "Tender upload failed",
          status: "failed",
          detail: apiError(res.data, "Could not create the project."),
          next: "Review the error, then try again.",
        });
        return showMessage(
          apiError(res.data, "Could not create the project."),
          "error"
        );
      }
      ctx.projectId = res.data.project_id;
      byId("project-id").value = ctx.projectId;
      showProjectContext({ project_id: ctx.projectId });
      ctx.tenderDocuments = null;
      ctx.state = res.data.state || res.data;
      byId("tender-file").value = "";
      byId("selected-tender-name").textContent = fileName;
      if (res.data.field_schema) displaySchema(res.data.field_schema, Boolean(ctx.selectedSchemaJson));
      window.history.replaceState({}, "", pageUrl("tender"));
      applyPageMode("tender");
      updateStepper();
      recordStepView("tender");
      button.hidden = true;
      button.removeAttribute("aria-busy");
      button.classList.remove("button-loading");
      byId("run-understanding-btn").hidden = true;
      byId("run-understanding-btn").disabled = false;
      runUnderstanding();
    });
  }

  function resumeProject() {
    var id = byId("project-id").value.trim();
    if (!id) return showMessage("Enter the project ID to resume.", "error");
    showMessage("");
    api("/workbench/projects/" + encodeURIComponent(id)).then(function (res) {
      if (!res.ok) {
        hideRouteLoading();
        return showMessage(apiError(res.data, "Project not found."), "error");
      }
      ctx.projectId = id;
      ctx.tenderDocuments = null;
      ctx.state = res.data;
      var requestedRoute = routeProject();
      if (
        requestedRoute && requestedRoute.page === "compliance" &&
        !(ctx.state.compliance && ctx.state.compliance.enabled)
      ) {
        window.history.replaceState({}, "", pageUrl("ledger"));
        applyPageMode("ledger");
      }
      showProjectContext(ctx.state);
      if (!routeProject()) {
        window.history.replaceState({}, "", pageUrl(recommendedPage()));
        applyPageMode(recommendedPage());
      }
      updateStepper();
      loadProjectSchema();
      var understandingJob = ctx.state.understanding_job;
      var understandingId = operationIdFrom(understandingJob, "understanding", "project");
      var understandingActive = understandingJob && operationIsActive(understandingJob);
      byId("create-project-btn").hidden = true;
      byId("run-understanding-btn").hidden = ctx.state.stage !== "tender_uploaded" || understandingActive;
      byId("run-understanding-btn").disabled = ctx.state.stage !== "tender_uploaded" || understandingActive;
      if (understandingActive) {
        showOperationActivity("understand-status", "understanding", {
          title: "Analyzing tender",
          payload: understandingJob,
          next: "You can leave this page. Progress is saved and updates automatically.",
        });
        if (understandingId) pollJob(understandingId);
      } else if (
        ctx.state.stage === "tender_uploaded" && understandingJob &&
        ["failed", "cancelled"].indexOf(operationStatus(understandingJob)) !== -1
      ) {
        showOperationActivity("understand-status", "understanding", {
          title: "Tender analysis failed",
          payload: understandingJob,
          next: "Review the error, then run the analysis again.",
        });
        byId("run-understanding-btn").disabled = false;
        byId("run-understanding-btn").hidden = false;
        showMessage(operationErrorText(understandingJob, "Tender analysis failed."), "error");
      } else if (!understandingJob) {
        setStatusText("understand-status", "Tender analysis has not started yet.");
      }
      var intakeJob = ctx.state.intake_job;
      var intakeId = operationIdFrom(intakeJob, "intake", "project");
      var intakeActive = intakeJob && operationIsActive(intakeJob);
      if (intakeActive) {
        var intakeVendor = (ctx.state.bidders || []).find(function (bidder) {
          return !bidder.documents_prepared;
        });
        showOperationActivity("intake-status", "intake", {
          title: "Preparing bidder documents",
          payload: intakeJob,
          next: "You can continue reviewing this tender. Classification will appear when ready.",
        });
        waitForWorkbenchJob(intakeId, {
          scope: "project",
          kind: "intake",
          statusElement: byId("intake-status"),
          onUpdate: function (payload) {
            showOperationActivity("intake-status", "intake", {
              title: "Preparing bidder documents",
              payload: payload,
              next: "You can continue reviewing this tender. Classification will appear when ready.",
            });
          },
        }).then(function () {
          return refreshState();
        }).then(function () {
          updateStepper();
          setStatusText("intake-status", "Documents prepared. Review and approve the classification.");
          window.location.href = pageUrl("evaluate") +
            (intakeVendor ? "?vendor=" + encodeURIComponent(intakeVendor.vendor_id) : "");
        }).catch(function (errorValue) {
          showOperationActivity("intake-status", "intake", {
            title: "Bidder document preparation failed",
            status: "failed",
            detail: errorValue.message || "Bidder document preparation failed.",
            next: "Review the error, then process the documents again.",
          });
          showMessage(errorValue.message || "Bidder document status could not be restored.", "error");
        });
      } else if (intakeJob && ["failed", "cancelled"].indexOf(operationStatus(intakeJob)) !== -1) {
        showOperationActivity("intake-status", "intake", {
          title: "Bidder document preparation failed",
          payload: intakeJob,
          next: "Review the error, then process the documents again.",
        });
        showMessage(operationErrorText(intakeJob, "Bidder document preparation failed."), "error");
      } else if (!intakeJob && ctx.state.bidders && ctx.state.bidders.length) {
        setStatusText("intake-status", "Bidder document preparation has not started yet.");
      }
      resumeKnownClassificationOperations();
      resumeKnownEvaluationOperations();
      var activeRoute = routeProject();
      recordStepView(activeRoute ? activeRoute.page : ctx.activePage);
      return Promise.resolve(loadActivePage(activeRoute ? activeRoute.page : ctx.activePage)).finally(hideRouteLoading);
    }).catch(function (errorValue) {
      console.error("Project resume failed.", errorValue);
      hideRouteLoading();
      showMessage("The project could not be loaded. Please try again.", "error");
    });
  }

  function runUnderstanding() {
    if (byId("tender-file").files[0]) {
      return showMessage("Create the new project first so the selected tender file is uploaded.", "error");
    }
    if (!ctx.projectId) return showMessage("Create the project first.", "error");
    if (!requireReviewer()) return;
    showMessage("");
    var form = new FormData();
    form.append("reviewed_by", reviewer());
    form.append("reviewed_at", nowIso());
    byId("run-understanding-btn").disabled = true;
    showOperationActivity("understand-status", "understanding", {
      title: "Analyzing tender",
      status: "queued",
      detail: "Starting tender analysis…",
      next: "You can leave this page. Progress is saved and updates automatically.",
    });
    api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/understand", {
      method: "POST",
      body: form,
    }).then(function (res) {
      if (!res.ok) {
        byId("run-understanding-btn").disabled = false;
        byId("run-understanding-btn").hidden = false;
        showOperationActivity("understand-status", "understanding", {
          title: "Tender analysis could not start",
          status: "failed",
          detail: apiError(res.data, "Could not start the run."),
          next: "Review the error, then try again.",
        });
        return showMessage(apiError(res.data, "Could not start the run."), "error");
      }
      rememberOperation("understanding", res.data.job_id, "project");
      pollJob(res.data.job_id);
    });
  }

  function pollJob(jobId) {
    if (!jobId) return Promise.reject(new Error("Tender analysis did not return a job."));
    rememberOperation("understanding", jobId, "project");
    return pollResource(
      "understanding:" + ctx.projectId + ":" + jobId,
      "/workbench/jobs/" + encodeURIComponent(jobId),
      {
        operationId: jobId,
        failureMessage: "Tender analysis failed.",
        onUpdate: function (job, status) {
          if (status === "status_unavailable") {
            showOperationActivity("understand-status", "understanding", {
              title: "Analyzing tender",
              status: "running",
              detail: "Status is temporarily unavailable; the backend job may still be running.",
              next: "No work has been marked as lost. This page will keep checking.",
            });
          } else {
            showOperationActivity("understand-status", "understanding", {
              title: "Analyzing tender",
              payload: job,
              next: "You can leave this page. Progress is saved and updates automatically.",
            });
          }
        },
        onStall: function () {
          showOperationActivity("understand-status", "understanding", {
            title: "Analyzing tender",
            status: "running",
            detail: "This is taking longer than usual, but the job is still running.",
            next: "No work has been marked as lost. You can leave this page safely.",
          });
        },
      }
    ).then(function (job) {
      setStatusText("understand-status", operationStatus(job) === "partial"
        ? "Tender analysis complete with review items. Review the extracted requirements."
        : "Tender analysis complete. Review the extracted requirements.");
      return refreshState().then(function () {
        window.history.replaceState({}, "", pageUrl("ledger"));
        applyPageMode("ledger");
        loadLedger();
        return job;
      });
    }).catch(function (errorValue) {
      showOperationActivity("understand-status", "understanding", {
        title: "Tender analysis failed",
        status: "failed",
        detail: errorValue.message || "Tender analysis failed.",
        next: "Review the error, then run the analysis again.",
      });
      byId("run-understanding-btn").disabled = false;
      byId("run-understanding-btn").hidden = false;
      showMessage(errorValue.message || "Tender analysis failed.", "error");
      return null;
    });
  }

  function waitForWorkbenchJob(jobId, options) {
    options = options || {};
    if (!jobId) return Promise.reject(new Error("Processing did not return a job."));
    rememberOperation(options.kind || "workbench", jobId, options.scope || "project");
    return pollResource(
      (options.kind || "workbench") + ":" + ctx.projectId + ":" + jobId,
      "/workbench/jobs/" + encodeURIComponent(jobId),
      {
        operationId: jobId,
        failureMessage: options.failureMessage || "Document processing failed.",
        onUpdate: options.onUpdate,
        onStall: options.onStall || function () {
          if (options.onUpdate) options.onUpdate({}, "stalled");
          else if (options.statusElement) options.statusElement.textContent = "This is taking longer than usual; the job is still running and has not been marked as lost.";
        },
      }
    );
  }

  function waitForOperation(operationId, options) {
    options = options || {};
    if (!operationId) return Promise.reject(new Error("Evaluation did not return an operation."));
    rememberOperation("evaluation", operationId, options.vendorId || "project");
    return pollResource(
      "evaluation:" + ctx.projectId + ":" + operationId,
      "/v1/operations/" + encodeURIComponent(operationId),
      {
        operationId: operationId,
        failureMessage: "Evaluation failed.",
        onUpdate: function (payload, status) {
          if (options.onUpdate) options.onUpdate(payload, status);
          var name = options.vendorName || options.vendorId || "Bidder";
          if (status === "status_unavailable") {
            showOperationActivity("evaluation-status", "evaluation:" + (options.vendorId || operationId), {
              title: "Evaluating " + name,
              status: "running",
              detail: "Status is temporarily unavailable; the backend job may still be running.",
              next: "No result has been marked as lost. Other bidders remain available.",
            });
          } else {
            showOperationActivity("evaluation-status", "evaluation:" + (options.vendorId || operationId), {
              title: "Evaluating " + name,
              payload: payload,
              next: operationStatus(payload) === "failed"
                ? "Review the error, then run this bidder again. Other results were preserved."
                : "You can review another bidder while this finishes. Results update automatically.",
            });
          }
        },
        onStall: function () {
          showOperationActivity("evaluation-status", "evaluation:" + (options.vendorId || operationId), {
            title: "Evaluating " + (options.vendorName || options.vendorId || "bidder"),
            status: "running",
            detail: "This is taking longer than usual, but evaluation is still running.",
            next: "No result has been marked as lost. Other bidders remain available.",
          });
        },
      }
    );
  }

  function resumeKnownClassificationOperations() {
    (ctx.state && ctx.state.bidders || []).forEach(function (bidder) {
      var job = bidder.classification_operation || {};
      var jobId = operationIdFrom(job, "classification", bidder.vendor_id);
      if (!jobId || !operationIsActive(job)) return;
      waitForWorkbenchJob(jobId, {
        kind: "classification",
        scope: bidder.vendor_id,
        onUpdate: function (payload, status) {
          showOperationActivity("evaluation-status", "classification:" + bidder.vendor_id, {
            title: "Classifying " + (bidder.bidder_name || bidder.vendor_id),
            payload: payload,
            status: status === "status_unavailable" || status === "stalled" ? "running" : "",
            detail: status === "status_unavailable"
              ? "Status is temporarily unavailable; classification may still be running."
              : status === "stalled"
              ? "This is taking longer than usual, but classification is still running."
              : "",
            next: "You can review another bidder while this finishes. The page updates automatically.",
          });
        },
      }).then(function () {
        return refreshState();
      }).catch(function (errorValue) {
        showOperationActivity("evaluation-status", "classification:" + bidder.vendor_id, {
          title: "Classification failed for " + (bidder.bidder_name || bidder.vendor_id),
          status: "failed",
          detail: errorValue.message || "Classification failed.",
          next: "Review the error, then re-analyze this bidder.",
        });
        showMessage(errorValue.message || "Classification failed.", "error");
      });
    });
  }

  function resumeKnownEvaluationOperations() {
    (ctx.state && ctx.state.bidders || []).forEach(function (bidder) {
      var operationId = bidder.evaluation_operation_id || bidder.operation_id || bidder.job_id ||
        operationIdFrom(null, "evaluation", bidder.vendor_id);
      if (!operationId || !operationIsActive({ status: bidder.evaluation_status })) return;
      waitForOperation(operationId, {
        vendorId: bidder.vendor_id,
        vendorName: bidder.bidder_name || bidder.vendor_id,
      }).then(function () {
        return refreshState();
      }).catch(function (errorValue) {
        showOperationActivity("evaluation-status", "evaluation:" + bidder.vendor_id, {
          title: "Evaluation failed for " + (bidder.bidder_name || bidder.vendor_id),
          status: "failed",
          detail: errorValue.message || "Evaluation failed.",
          next: "Review the error, then run this bidder again. Other results were preserved.",
        });
        showMessage(errorValue.message || "Evaluation failed.", "error");
      });
    });
  }


  Object.assign(ctx, {
    showSetupTab: showSetupTab,
    schemaSummary: schemaSummary,
    displaySchema: displaySchema,
    useSchemaText: useSchemaText,
    useDefaultSchema: useDefaultSchema,
    loadDefaultSchemaInfo: loadDefaultSchemaInfo,
    loadProjectSchema: loadProjectSchema,
    createProject: createProject,
    resumeProject: resumeProject,
    runUnderstanding: runUnderstanding,
    pollJob: pollJob,
    waitForWorkbenchJob: waitForWorkbenchJob,
    waitForOperation: waitForOperation,
    resumeKnownClassificationOperations: resumeKnownClassificationOperations,
    resumeKnownEvaluationOperations: resumeKnownEvaluationOperations,
  });
}
