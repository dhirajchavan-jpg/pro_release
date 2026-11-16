"use strict";
/* Bidder classification PDF preview and workspace rendering. */

import {
  BUSINESS_DOCUMENT_CATEGORIES,
  broadRouteForCategory,
  businessCategoryForType,
  businessDocumentName,
  businessReviewState,
  classificationChanged,
  classificationRow,
  containsLabel,
  normalizedBroadRoute,
  projectedBroadRoute,
  validateClassification,
} from "./bid-classification-model.js";

export function installBidClassificationView(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var apiFetch = function () { return ctx.apiFetch.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var pill = function () { return ctx.pill.apply(ctx, arguments); };
  var updateClassificationContinue = function () { return ctx.updateClassificationContinue.apply(ctx, arguments); };

  function openManualDocument(document, page) {
    var requestNumber = ++ctx.manualDocumentRequest;
    var frame = byId("manual-pdf-frame");
    var status = byId("manual-pdf-status");
    if (!document.url) {
      status.textContent = "This saved run has no source-file preview. The extracted text remains available in the evaluation evidence.";
      return;
    }
    status.textContent = "Loading " + document.file_name + "…";
    apiFetch(document.url).then(function (response) {
      if (!response.ok) throw new Error("unavailable");
      var contentType = (response.headers.get("content-type") || "").toLowerCase();
      if (contentType.indexOf("application/pdf") !== 0 && contentType.indexOf("text/plain") !== 0) {
        throw new Error("unsupported");
      }
      return response.blob();
    }).then(function (blob) {
      if (requestNumber !== ctx.manualDocumentRequest) return;
      var previous = frame.getAttribute("data-object-url");
      if (previous) URL.revokeObjectURL(previous);
      var objectUrl = URL.createObjectURL(blob);
      var isPdf = blob.type.toLowerCase().indexOf("application/pdf") === 0;
      frame.src = objectUrl + (isPdf && page ? "#page=" + encodeURIComponent(page) : "");
      frame.setAttribute("data-object-url", objectUrl);
      frame.title = "Bidder source: " + document.file_name;
      ctx.activeDocumentFrame = frame;
      status.textContent = document.file_name + (isPdf ? " · click inside the PDF, then use Ctrl+F" : " · source text");
    }).catch(function (error) {
      status.textContent = error.message === "unsupported"
        ? "This file type cannot be previewed here. "
        : "This source file could not be opened. ";
      status.appendChild(el("a", {
        href: document.url,
        text: "Download source file",
        target: "_blank",
        rel: "noopener",
      }));
    });
  }

  function updateClassificationState() {
    var changed = classificationChanged(ctx);
    byId("classification-reason-wrap").hidden = !changed;
    var error = validateClassification(ctx, byId);
    var unresolvedRows = ctx.activeClassification.rows.filter(function (row) {
      return normalizedBroadRoute(row.broad_route || projectedBroadRoute([row])) === "needs_review";
    });
    var unresolved = unresolvedRows.length;
    var status = byId("manual-review-status");
    status.textContent = error || (unresolved
      ? "Classify " + unresolved + " remaining document section" + (unresolved === 1 ? "" : "s") + " before approval."
      : changed
      ? "You have unsaved changes."
      : "Changes are saved. Continue reviewing or approve the classification.");
    if (unresolved) {
      var actions = el("div", { className: "classification-blocker-actions" });
      unresolvedRows.forEach(function (row) {
        var review = el("button", {
          className: "secondary",
          type: "button",
          text: "Review " + (row.title || "unclassified section") +
            " (pages " + row.start_page + "–" + row.end_page + ")",
        });
        review.addEventListener("click", function () {
          var field = byId("classification-" + row.logical_document_id.replace(/[^A-Za-z0-9_-]/g, "-") + "-route");
          if (!field) return;
          field.scrollIntoView({ behavior: "smooth", block: "center" });
          window.setTimeout(function () { field.focus(); }, 350);
        });
        actions.appendChild(review);
      });
      status.appendChild(actions);
    }
    status.classList.toggle("classification-error", Boolean(error || unresolved));
    byId("classification-save-state").textContent = changed ? "Unsaved changes" : "All changes saved";
    byId("save-classification-btn").disabled = !changed ||
      ctx.activeClassification.status === "approved";
    var approve = byId("approve-classification-btn");
    approve.disabled = changed || Boolean(error) || unresolved > 0 ||
      ctx.activeClassification.status === "approved";
    approve.title = unresolved
      ? "Approval is blocked until the listed file classifications are resolved."
      : changed ? "Save your changes before approval." : "";
    updateClassificationContinue();
  }

  function renderClassificationWorkspace(payload, review) {
    var classification = payload.classification || {};
    var rows = (classification.logical_documents || []).map(classificationRow);
    rows.forEach(function (row) {
      if (!row.broad_route) {
        var category = businessCategoryForType(row.document_type);
        row.broad_route = category ? broadRouteForCategory(category) : "needs_review";
        if (row.broad_route === "needs_review" && !row.route_reason) {
          row.route_reason = "This logical document needs a document category before approval.";
        }
      }
    });
    var rawProposal = classification.ai_proposal || [];
    var proposal = rawProposal.map(classificationRow);
    var proposalConfidence = {};
    rawProposal.forEach(function (row) {
      proposalConfidence[String(row.logical_document_id || "")] = Number(row.confidence);
    });
    var physicalRoutes = {};
    (classification.physical_routes || []).forEach(function (route) {
      physicalRoutes[String(route.document_id || "")] = Object.assign({}, route, {
        broad_route: normalizedBroadRoute(route.broad_route),
      });
    });
    var documents = review.documents || [];
    documents.forEach(function (document) {
      if (!rows.some(function (row) { return row.source_document_id === document.document_id; })) {
        rows.push({
          logical_document_id: document.document_id + "-section-1",
          title: document.file_name || "Bid document",
          document_type: "other",
          source_document_id: document.document_id,
          start_page: 1,
          end_page: Number(document.page_count || 1),
        });
      }
      if (!physicalRoutes[document.document_id]) {
        var documentRows = rows.filter(function (row) { return row.source_document_id === document.document_id; });
        physicalRoutes[document.document_id] = {
          document_id: document.document_id,
          broad_route: projectedBroadRoute(documentRows),
          reason: projectedBroadRoute(documentRows) === "needs_review" ? "Needs human review." : "",
        };
      }
    });

    function syncPhysicalRoute(documentId) {
      var documentRows = rows.filter(function (row) { return row.source_document_id === documentId; });
      var rowRoutes = documentRows.map(function (row) {
        return normalizedBroadRoute(row.broad_route || projectedBroadRoute([row]));
      });
      var known = rowRoutes.filter(function (route) {
        return route !== "needs_review" && route !== "mixed_submission";
      });
      var broadRoute = !known.length
        ? "needs_review"
        : rowRoutes.indexOf("needs_review") !== -1 || new Set(known).size > 1
        ? "mixed_submission"
        : known[0];
      var route = physicalRoutes[documentId] || { document_id: documentId };
      route.broad_route = broadRoute;
      route.secondary_routes = Array.from(new Set(known.filter(function (value) { return value !== broadRoute; })));
      route.reason = broadRoute === "needs_review"
        ? "This file contains no recognized procurement category yet; classify its document section(s)."
        : broadRoute === "mixed_submission"
        ? "This uploaded file contains multiple logical document categories; review the sections below."
        : "";
      physicalRoutes[documentId] = route;
      return route;
    }
    ctx.activeClassification = {
      vendor: payload.vendor_id || review.vendor_id,
      status: payload.status || "draft",
      rows: rows,
      savedRows: rows.map(classificationRow),
      aiProposal: proposal,
      proposalConfidence: proposalConfidence,
      physicalRoutes: physicalRoutes,
      savedPhysicalRoutes: JSON.parse(JSON.stringify(physicalRoutes)),
      documents: documents,
    };
    byId("manual-bidder-name").textContent = review.bidder_name || review.vendor_id || "Bidder submission";
    byId("classification-reason").value = "";
    byId("classification-reason-wrap").hidden = true;
    byId("manual-readiness").hidden = false;
    var tenderMatch = review.tender_match || {};
    var mismatch = tenderMatch.status === "mismatch";
    byId("tender-mismatch").hidden = !mismatch;
    if (mismatch) {
      byId("tender-mismatch-message").textContent =
        "You can classify these files, but qualification is blocked because they reference tender " +
        (tenderMatch.observed_tender_ids || []).join(", ") + " instead of the selected tender.";
    }
    var coverageHost = byId("manual-review-coverage");
    coverageHost.textContent = "";
    coverageHost.appendChild(pill(payload.status === "approved" ? "Classification approved" : "Awaiting approval",
      payload.status === "approved" ? "complete" : "review"));
    coverageHost.appendChild(el("span", { className: "summary-fact", text: documents.length + " submitted file(s)" }));
    coverageHost.appendChild(el("span", { className: "summary-fact", text: rows.length + " recognized document(s)" }));
    var categoryGuide = el("div", { className: "classification-category-guide" });
    BUSINESS_DOCUMENT_CATEGORIES.forEach(function (category) {
      categoryGuide.appendChild(el("span", { text: category.label }));
    });
    coverageHost.appendChild(categoryGuide);
    var host = byId("manual-document-list");
    host.textContent = "";
    documents.forEach(function (document) {
      var card = el("article", { className: "classification-file" });
      var head = el("div", { className: "classification-file-head" });
      var heading = el("div");
      heading.appendChild(el("small", { text: "Original file" }));
      heading.appendChild(el("strong", { text: document.file_name || "Unnamed file" }));
      heading.appendChild(el("small", { text: (document.page_count || 0) + " page(s)" }));
      head.appendChild(heading);
      var view = el("button", { className: "secondary", type: "button", text: "View source" });
      view.disabled = !document.url;
      view.addEventListener("click", function () {
        openManualDocument(document, 1);
        Array.prototype.forEach.call(host.children, function (item) { item.classList.remove("active"); });
        card.classList.add("active");
      });
      head.appendChild(view);
      card.appendChild(head);
      var summary = el("div", { className: "classification-business-summary" });
      var summaryHead = el("div", { className: "criterion-head" });
      var businessName = el("h3");
      var statePill = pill("Ready", "complete");
      summaryHead.appendChild(businessName);
      summaryHead.appendChild(statePill);
      summary.appendChild(summaryHead);
      var contains = el("div", { className: "classification-contains" });
      summary.appendChild(contains);
      var routeControls = el("div", { className: "classification-route-controls" });
      summary.appendChild(routeControls);
      card.appendChild(summary);
      var sections = el("div", { className: "classification-sections" });
      function documentRows() {
        return ctx.activeClassification.rows.filter(function (row) {
          return row.source_document_id === document.document_id;
        }).sort(function (a, b) { return a.start_page - b.start_page; });
      }
      function refreshSummary() {
        var currentRows = documentRows();
        businessName.textContent = businessDocumentName(currentRows, document);
        var reviewState = businessReviewState(currentRows, ctx.activeClassification.proposalConfidence,
          ctx.activeClassification.physicalRoutes[document.document_id]);
        statePill.textContent = reviewState;
        statePill.className = "pill " + (reviewState === "Ready" ? "complete" :
          reviewState === "Reference documents" ? "review_note" : "review");
        contains.textContent = "";
        contains.appendChild(el("strong", { text: "Contains" }));
        Array.from(new Set(currentRows.map(containsLabel))).forEach(function (label) {
          contains.appendChild(el("span", { className: "classification-tag", text: label }));
        });
      }
      function renderRouteControls() {
        routeControls.textContent = "";
        var route = ctx.activeClassification.physicalRoutes[document.document_id];
        var routeField = el("div", { className: "field" });
        var routeId = "classification-route-" + document.document_id;
        routeField.appendChild(el("label", { text: "Classification", for: routeId }));
        var routeSelect = el("select", { id: routeId });
        BUSINESS_DOCUMENT_CATEGORIES.forEach(function (category) {
          routeSelect.appendChild(el("option", {
            value: broadRouteForCategory(category.value), text: category.label,
          }));
        });
        routeSelect.appendChild(el("option", { value: "reference_document", text: "Reference documents" }));
        routeSelect.appendChild(el("option", { value: "mixed_submission", text: "Multiple document categories" }));
        routeSelect.appendChild(el("option", { value: "needs_review", text: "Please check — decide later" }));
        routeSelect.value = route.broad_route;
        routeSelect.disabled = route.broad_route === "mixed_submission";
        routeSelect.addEventListener("change", function () {
          route.broad_route = routeSelect.value;
          route.secondary_routes = (route.secondary_routes || []).filter(function (value) {
            return value !== route.broad_route;
          });
          route.reason = "";
          renderRouteControls();
          refreshSummary();
          updateClassificationState();
        });
        routeField.appendChild(routeSelect);
        routeControls.appendChild(routeField);
        if (route.broad_route !== "reference_document" && route.broad_route !== "needs_review") {
          var secondary = el("details", { className: "classification-secondary" });
          var secondaryLabels = (route.secondary_routes || []).map(function (value) {
            var category = BUSINESS_DOCUMENT_CATEGORIES.find(function (item) {
              return broadRouteForCategory(item.value) === value;
            });
            return category ? category.label : value;
          });
          secondary.appendChild(el("summary", {
            text: secondaryLabels.length
              ? "Also relevant to: " + secondaryLabels.join(", ")
              : "Add another relevant category",
          }));
          BUSINESS_DOCUMENT_CATEGORIES.forEach(function (category) {
            var value = broadRouteForCategory(category.value);
            if (value === route.broad_route || value === "commercial_price_restricted") return;
            var checkbox = el("input", { type: "checkbox", value: value });
            checkbox.checked = (route.secondary_routes || []).indexOf(value) !== -1;
            checkbox.addEventListener("change", function () {
              route.secondary_routes = route.secondary_routes || [];
              if (checkbox.checked && route.secondary_routes.indexOf(value) === -1) route.secondary_routes.push(value);
              if (!checkbox.checked) route.secondary_routes = route.secondary_routes.filter(function (item) { return item !== value; });
              renderRouteControls();
              updateClassificationState();
            });
            secondary.appendChild(el("label", { className: "classification-secondary-option" }, [
              checkbox,
              el("span", { text: category.label }),
            ]));
          });
          routeControls.appendChild(secondary);
        }
        if ((route.evidence_purposes || []).length) {
          var purposes = el("div", { className: "classification-purpose-list" }, [
            el("strong", { text: "Evaluation uses" }),
          ]);
          route.evidence_purposes.forEach(function (purpose) {
            purposes.appendChild(el("span", { className: "classification-tag", text: purpose }));
          });
          routeControls.appendChild(purposes);
        }
        if (route.broad_route === "needs_review") {
          var noteField = el("div", { className: "field" });
          var noteId = routeId + "-note";
          noteField.appendChild(el("label", { text: "What remains unclear?", for: noteId }));
          var note = el("input", {
            id: noteId, type: "text", value: route.reason || "",
            placeholder: "Briefly describe it for the next reviewer",
          });
          note.addEventListener("input", function () {
            route.reason = note.value;
            updateClassificationState();
          });
          noteField.appendChild(note);
          routeControls.appendChild(noteField);
        }
      }
      function renderSections() {
        sections.textContent = "";
        var currentRows = documentRows();
        var showPageRanges = currentRows.length > 1;
        currentRows.forEach(function (row) {
          var editor = el("div", { className: "classification-section" });
          var controlPrefix = "classification-" + row.logical_document_id.replace(/[^A-Za-z0-9_-]/g, "-");
          var titleField = el("div", { className: "field classification-wide" });
          titleField.appendChild(el("label", { text: "Business document name", for: controlPrefix + "-title" }));
          var title = el("input", { id: controlPrefix + "-title", type: "text", value: row.title });
          title.addEventListener("input", function () {
            row.title = title.value;
            refreshSummary();
            updateClassificationState();
          });
          titleField.appendChild(title);
          editor.appendChild(titleField);
          if (showPageRanges) {
            [["From page", "start_page"], ["To page", "end_page"]].forEach(function (item) {
              var field = el("div", { className: "field" });
              var inputId = controlPrefix + "-" + item[1];
              field.appendChild(el("label", { text: item[0], for: inputId }));
              var input = el("input", { id: inputId, type: "number", min: "1", max: String(document.page_count), value: String(row[item[1]]) });
              input.addEventListener("change", function () { row[item[1]] = Number(input.value); updateClassificationState(); });
              field.appendChild(input);
              editor.appendChild(field);
            });
          }
          var routeField = el("div", { className: "field classification-wide" });
          var routeId = controlPrefix + "-route";
          routeField.appendChild(el("label", { text: "Document classification", for: routeId }));
          var routeSelect = el("select", { id: routeId });
          BUSINESS_DOCUMENT_CATEGORIES.forEach(function (category) {
            routeSelect.appendChild(el("option", {
              value: broadRouteForCategory(category.value), text: category.label,
            }));
          });
          routeSelect.appendChild(el("option", { value: "reference_document", text: "Reference documents" }));
          routeSelect.appendChild(el("option", { value: "needs_review", text: "Please check — decide later" }));
          var rowRoute = normalizedBroadRoute(row.broad_route || projectedBroadRoute([row]));
          routeSelect.value = rowRoute;
          routeSelect.addEventListener("change", function () {
            row.broad_route = routeSelect.value;
            row.secondary_routes = [];
            row.route_reason = "";
            if (row.broad_route === "needs_review") {
              row.route_reason = "This logical document needs a document category before approval.";
            }
            syncPhysicalRoute(document.document_id);
            renderRouteControls();
            renderSections();
            updateClassificationState();
          });
          routeField.appendChild(routeSelect);
          editor.appendChild(routeField);
          if (rowRoute === "needs_review") {
            var rowNote = el("div", { className: "field classification-wide" });
            var rowNoteId = routeId + "-note";
            rowNote.appendChild(el("label", { text: "What remains unclear?", for: rowNoteId }));
            var rowNoteInput = el("input", {
              id: rowNoteId, type: "text", value: row.route_reason || "",
              placeholder: "Briefly describe it for the next reviewer",
            });
            rowNoteInput.addEventListener("input", function () {
              row.route_reason = rowNoteInput.value;
              updateClassificationState();
            });
            rowNote.appendChild(rowNoteInput);
            editor.appendChild(rowNote);
          }
          var actions = el("div", { className: "classification-actions" });
          var inspect = el("button", { className: "secondary", type: "button", text: "Open source" });
          inspect.disabled = !document.url;
          inspect.addEventListener("click", function () { openManualDocument(document, row.start_page); });
          actions.appendChild(inspect);
          var split = el("button", { className: "secondary", type: "button", text: "Split PDF" });
          split.hidden = document.content_type !== "application/pdf";
          split.disabled = row.start_page >= row.end_page || payload.status === "approved";
          split.addEventListener("click", function () {
            var oldEnd = row.end_page;
            var splitAfter = Math.floor((row.start_page + row.end_page) / 2);
            row.end_page = splitAfter;
            ctx.activeClassification.rows.push({
              logical_document_id: row.logical_document_id + "-split-" + (ctx.activeClassification.rows.length + 1),
              title: row.title + " (continued)",
              document_type: row.document_type,
              source_document_id: row.source_document_id,
              start_page: splitAfter + 1,
              end_page: oldEnd,
              custom_document_type: row.custom_document_type,
            });
            renderSections();
            updateClassificationState();
          });
          actions.appendChild(split);
          var remove = el("button", { className: "secondary", type: "button", text: "Merge with previous" });
          var sameFile = documentRows();
          remove.disabled = sameFile.length < 2 || payload.status === "approved";
          remove.title = "Merge these pages into the adjacent document";
          remove.addEventListener("click", function () {
            var ordered = documentRows();
            var index = ordered.indexOf(row);
            var neighbor = index > 0 ? ordered[index - 1] : ordered[1];
            neighbor.start_page = Math.min(neighbor.start_page, row.start_page);
            neighbor.end_page = Math.max(neighbor.end_page, row.end_page);
            ctx.activeClassification.rows.splice(ctx.activeClassification.rows.indexOf(row), 1);
            renderSections();
            updateClassificationState();
          });
          actions.appendChild(remove);
          editor.appendChild(actions);
          sections.appendChild(editor);
        });
        refreshSummary();
      }
      renderRouteControls();
      renderSections();
      card.appendChild(sections);
      host.appendChild(card);
    });
    if (documents.length) {
      var firstPreview = documents.find(function (document) { return document.url; });
      if (firstPreview) openManualDocument(firstPreview, 1);
      if (host.children[0]) host.children[0].classList.add("active");
    }
    if (!documents.length) host.appendChild(el("div", { className: "vendor-empty", text: "No bidder documents were found." }));
    byId("approve-classification-btn").textContent = payload.status === "approved" ? "Classification approved" : "Approve classification";
    byId("propose-classification-btn").hidden = payload.status === "approved";
    Array.prototype.forEach.call(host.querySelectorAll("input, select"), function (control) {
      control.disabled = payload.status === "approved" || control.disabled;
    });
    updateClassificationState();
  }

  return {
    openManualDocument: openManualDocument,
    updateClassificationState: updateClassificationState,
    renderClassificationWorkspace: renderClassificationWorkspace,
  };
}
