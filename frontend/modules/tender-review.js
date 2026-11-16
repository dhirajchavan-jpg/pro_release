"use strict";
/* tender finding review. */

export function installTenderReview(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var api = function () { return ctx.api.apply(ctx, arguments); };
  var apiError = function () { return ctx.apiError.apply(ctx, arguments); };
  var apiFetch = function () { return ctx.apiFetch.apply(ctx, arguments); };
  var blockingQuestions = function () { return ctx.blockingQuestions.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var friendlyStatus = function () { return ctx.friendlyStatus.apply(ctx, arguments); };
  var ledgerValueToText = function () { return ctx.ledgerValueToText.apply(ctx, arguments); };
  var nowIso = function () { return ctx.nowIso.apply(ctx, arguments); };
  var pill = function () { return ctx.pill.apply(ctx, arguments); };
  var renderBlockingQuestions = function () { return ctx.renderBlockingQuestions.apply(ctx, arguments); };
  var renderDraft = function () { return ctx.renderDraft.apply(ctx, arguments); };
  var requireReviewer = function () { return ctx.requireReviewer.apply(ctx, arguments); };
  var reviewer = function () { return ctx.reviewer.apply(ctx, arguments); };
  var showMessage = function () { return ctx.showMessage.apply(ctx, arguments); };
  var updateStepper = function () { return ctx.updateStepper.apply(ctx, arguments); };
  // ---------------------------------------------------------------- Step 2

  function loadLedger() {
    if (!ctx.projectId) return Promise.resolve();
    return api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/ledger").then(
      function (res) {
        if (!res.ok) return;
        renderLedger(res.data);
      }
    );
  }

  function evidenceDetails(refs) {
    var ref = refs[0];
    var details = el("details", { className: "evidence-detail" });
    var location = "Page " + ref.page + " · original source document";
    details.appendChild(el("summary", { text: location }));
    details.title = ref.document_id;
    refs
      .map(function (item) { return item.quote; })
      .filter(function (quote, index, quotes) { return quote && quotes.indexOf(quote) === index; })
      .forEach(function (quote) {
        details.appendChild(el("blockquote", { text: "“" + quote + "”" }));
      });
    if (!ref.page_image_url) {
      details.appendChild(el("p", { text: "Page preview unavailable for this package." }));
      return details;
    }
    var status = el("p", { text: "Open to load the cited page image." });
    var image = el("img", {
      className: "evidence-preview",
      alt: location + " evidence preview",
      tabindex: "0",
      title: "Click to open the full-screen viewer",
    });
    var fullscreenButton = el("button", {
      className: "evidence-fullscreen",
      type: "button",
      text: "Enlarge source page",
    });
    var viewer = el("dialog", {
      className: "evidence-viewer",
      "aria-label": location + " full-screen viewer",
    });
    var viewerClose = el("button", { type: "button", text: "Close" });
    var viewerHint = el("p", { text: "Click the page to zoom to 200%." });
    var viewerImage = el("img", { alt: location + " full-screen evidence" });
    var viewerStage = el("div", { className: "evidence-viewer-stage" }, [viewerImage]);
    viewer.appendChild(el("div", { className: "evidence-viewer-bar" }, [viewerHint, viewerClose]));
    viewer.appendChild(viewerStage);
    fullscreenButton.hidden = true;
    var requestNumber = 0;

    function openViewer() {
      if (!image.src) return;
      viewerImage.src = image.src;
      viewerImage.classList.remove("zoomed");
      viewerHint.textContent = "Click the page to zoom to 200%.";
      viewer.showModal();
    }

    image.addEventListener("click", openViewer);
    image.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openViewer();
      }
    });
    fullscreenButton.addEventListener("click", openViewer);
    viewerClose.addEventListener("click", function () { viewer.close(); });
    viewerImage.addEventListener("click", function () {
      var zoomed = viewerImage.classList.toggle("zoomed");
      viewerHint.textContent = zoomed ? "Click the page to fit it to the screen." : "Click the page to zoom to 200%.";
      if (!zoomed) viewerStage.scrollTo(0, 0);
    });
    viewer.addEventListener("close", function () {
      viewerImage.classList.remove("zoomed");
      viewerImage.removeAttribute("src");
    });
    details.appendChild(status);
    details.appendChild(image);
    details.appendChild(fullscreenButton);
    details.appendChild(viewer);
    details.addEventListener("toggle", function () {
      requestNumber += 1;
      var currentRequest = requestNumber;
      if (!details.open) {
        if (viewer.open) viewer.close();
        if (image.src) URL.revokeObjectURL(image.src);
        image.removeAttribute("src");
        fullscreenButton.hidden = true;
        status.textContent = "Open to load the cited page image.";
        return;
      }
      status.textContent = "Loading cited page…";
      apiFetch(ref.page_image_url).then(function (response) {
        if (!response.ok || (response.headers.get("content-type") || "").indexOf("image/png") !== 0) {
          throw new Error("preview unavailable");
        }
        return response.blob();
      }).then(function (blob) {
        var objectUrl = URL.createObjectURL(blob);
        if (!details.open || currentRequest !== requestNumber) {
          URL.revokeObjectURL(objectUrl);
          return;
        }
        image.src = objectUrl;
        fullscreenButton.hidden = false;
        status.textContent = "Exact page from the uploaded tender document.";
      }).catch(function () {
        if (details.open && currentRequest === requestNumber) {
          status.textContent = "Page preview unavailable.";
        }
      });
    });
    return details;
  }

  function evidenceGroup(refs) {
    var details = el("details", { className: "evidence-group" });
    var pageGroups = [];
    var pageGroupByKey = {};
    refs.forEach(function (ref) {
      var pageKey = ref.document_id + "|" + ref.page;
      if (!pageGroupByKey[pageKey]) {
        pageGroupByKey[pageKey] = [];
        pageGroups.push(pageGroupByKey[pageKey]);
      }
      pageGroupByKey[pageKey].push(ref);
    });
    details.appendChild(el("summary", {
      text:
        pageGroups.length + (pageGroups.length === 1 ? " page" : " pages") +
        " · " + refs.length + (refs.length === 1 ? " citation" : " citations")
    }));
    pageGroups.forEach(function (pageRefs) {
      details.appendChild(evidenceDetails(pageRefs));
    });
    return details;
  }

  function correctionValueToText(value) {
    if (value == null) return "";
    return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
  }

  var BOOLEAN_TENDER_FIELDS = {
    joint_venture_allowed: true,
    integrity_pact_required: true,
    local_content_declaration: true,
    mse_startup_relaxation: true,
    msme_preference_applicable: true,
    non_blacklisting_affidavit: true,
  };
  var NUMBER_TENDER_FIELDS = {
    emd_amount_inr: true,
    min_turnover_req_inr: true,
    tender_estimated_cost_inr: true,
    tender_fee_inr: true,
  };

  function parseCorrectionValue(raw, original, fieldKey) {
    var text = raw.trim();
    if (BOOLEAN_TENDER_FIELDS[fieldKey]) {
      if (text.toLowerCase() !== "true" && text.toLowerCase() !== "false") throw new Error("not a boolean");
      return text.toLowerCase() === "true";
    }
    if (NUMBER_TENDER_FIELDS[fieldKey]) {
      var fieldNumber = Number(text.replace(/,/g, ""));
      if (!isFinite(fieldNumber)) throw new Error("not a number");
      return fieldNumber;
    }
    if (original !== null && typeof original === "object") return JSON.parse(text);
    if (typeof original === "boolean") return text === "true";
    if (typeof original === "number") {
      var number = Number(text);
      if (!isFinite(number)) throw new Error("not a number");
      return number;
    }
    if (text[0] === "[" || text[0] === "{") return JSON.parse(text);
    return text;
  }

  function correctionList(raw) {
    return raw
      .split("\n")
      .map(function (item) { return item.trim(); })
      .filter(function (item, index, items) { return item && items.indexOf(item) === index; });
  }

  function documentReviewWorkspace(entry, editor) {
    var workspace = el("div", { className: "ledger-workspace" });
    var library = el("aside", { className: "document-library" });
    library.appendChild(el("h3", { text: "Uploaded documents" }));
    var documentList = el("div", { className: "document-list" });
    library.appendChild(documentList);
    var viewer = el("div", { className: "document-viewer" });
    var viewerStatus = el("p", {
      className: "document-viewer-status",
      text: "Choose a document. Click inside the PDF, then use Ctrl+F to find text in it.",
    });
    var frame = el("iframe", {
      title: "Original tender document",
      className: "document-frame",
    });
    viewer.appendChild(viewerStatus);
    viewer.appendChild(frame);
    workspace.appendChild(library);
    workspace.appendChild(viewer);
    workspace.appendChild(editor);
    var currentDocument = null;

    function openDocument(document) {
      currentDocument = document;
      viewerStatus.textContent = "Loading " + document.file_name + "…";
      apiFetch(document.url).then(function (response) {
        if (!response.ok || (response.headers.get("content-type") || "").indexOf("application/pdf") !== 0) {
          throw new Error("document unavailable");
        }
        return response.blob();
      }).then(function (blob) {
        var previous = frame.getAttribute("data-object-url");
        if (previous) URL.revokeObjectURL(previous);
        var objectUrl = URL.createObjectURL(blob);
        frame.src = objectUrl;
        frame.setAttribute("data-object-url", objectUrl);
        frame.title = document.file_name;
        viewerStatus.textContent = document.document_kind + " · " + document.file_name +
          " · click the PDF, then use Ctrl+F";
      }).catch(function () {
        viewerStatus.textContent = "This PDF could not be opened.";
      });
    }

    function renderDocuments(documents) {
      documentList.textContent = "";
      if (!documents.length) {
        documentList.appendChild(el("p", { text: "No viewable PDF was found in this package." }));
        return;
      }
      documents.forEach(function (document) {
        var button = el("button", { className: "document-link", type: "button" });
        button.appendChild(el("strong", { text: document.document_kind }));
        button.appendChild(el("span", { text: document.file_name }));
        if (document.page_count) button.appendChild(el("small", { text: document.page_count + " pages" }));
        button.addEventListener("click", function () { openDocument(document); });
        documentList.appendChild(button);
      });
      var citedDocumentId = entry.evidence.length ? entry.evidence[0].document_id : "";
      var selected = documents.find(function (document) { return document.document_id === citedDocumentId; });
      openDocument(selected || documents[0]);
    }

    return {
      node: workspace,
      frame: frame,
      open: function () {
        if (documentList.children.length) return;
        documentList.appendChild(el("p", { text: "Loading uploaded documents…" }));
        if (ctx.tenderDocuments) {
          renderDocuments(ctx.tenderDocuments);
          return;
        }
        api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/documents").then(function (res) {
          if (!res.ok) {
            documentList.textContent = "";
            documentList.appendChild(el("p", { text: "Uploaded documents are unavailable." }));
            return;
          }
          ctx.tenderDocuments = res.data.documents;
          renderDocuments(ctx.tenderDocuments);
        });
      },
    };
  }

  function correctionEditor(entry, ledger, workspaceRow, workspaceCell) {
    var host = el("div", { className: "ledger-review" });
    if (entry.review_source === "human_corrected") {
      host.appendChild(pill("human corrected", "review"));
      host.appendChild(el("p", {
        text: "Initial analysis: " + friendlyStatus(entry.ai_status) +
          (entry.ai_value == null ? "" : " · " + correctionValueToText(entry.ai_value)),
      }));
      host.appendChild(el("p", {
        text: entry.reviewed_by + ": " + entry.correction_reason,
      }));
    }
    if (!ledger.editable) return host;

    var editButton = el("button", {
      className: "secondary",
      type: "button",
      text: entry.review_source === "human_corrected" ? "Edit again" : "Edit finding",
    });
    var editor = el("div", { className: "ledger-correction" });
    var status = el("select");
    ["found", "not_found", "not_applicable", "unresolved"].forEach(function (value) {
      var option = el("option", { value: value, text: friendlyStatus(value) });
      if (value === entry.status) option.setAttribute("selected", "selected");
      status.appendChild(option);
    });
    var value;
    if (BOOLEAN_TENDER_FIELDS[entry.key]) {
      value = el("select");
      [
        { value: "", text: "Select Yes or No" },
        { value: "true", text: "Yes — explicitly stated" },
        { value: "false", text: "No — explicitly prohibited or not required" },
      ].forEach(function (item) {
        var option = el("option", { value: item.value, text: item.text });
        if (entry.value === true && item.value === "true") option.setAttribute("selected", "selected");
        if (entry.value === false && item.value === "false") option.setAttribute("selected", "selected");
        value.appendChild(option);
      });
    } else {
      value = el("textarea", { rows: "3", placeholder: "Enter the correct value" });
      value.value = correctionValueToText(entry.value);
    }
    var conditions = el("textarea", { rows: "2", placeholder: "One condition per line" });
    conditions.value = entry.conditions.join("\n");
    var exceptions = el("textarea", { rows: "2", placeholder: "One exception per line" });
    exceptions.value = entry.exceptions.join("\n");
    var documents = el("textarea", { rows: "2", placeholder: "One required document per line" });
    documents.value = entry.required_documents.join("\n");
    var reason = el("textarea", {
      rows: "3",
      placeholder: "Required: what was wrong, the correct interpretation, and why",
    });
    var save = el("button", { type: "button", text: "Save human correction" });
    var correctionStatus = el("p", { className: "status-line" });

    function labelled(label, control) {
      var field = el("div", { className: "field" });
      field.appendChild(el("label", { text: label }));
      field.appendChild(control);
      return field;
    }

    editor.appendChild(labelled("Correct status", status));
    editor.appendChild(labelled("Correct value", value));
    if (BOOLEAN_TENDER_FIELDS[entry.key]) editor.appendChild(el("small", {
      text: "If the tender does not mention this requirement, choose Not found above — do not choose No.",
    }));
    var extra = el("details", { className: "correction-extra" });
    extra.appendChild(el("summary", { text: "Conditions, exceptions, and required documents" }));
    extra.appendChild(labelled("Conditions", conditions));
    extra.appendChild(labelled("Exceptions", exceptions));
    extra.appendChild(labelled("Required bidder documents", documents));
    editor.appendChild(extra);
    editor.appendChild(labelled("Reason for correction (required)", reason));
    editor.appendChild(save);
    editor.appendChild(correctionStatus);
    var documentWorkspace = documentReviewWorkspace(entry, editor);
    workspaceCell.appendChild(documentWorkspace.node);
    editButton.addEventListener("click", function () {
      var opening = workspaceRow.hidden;
      document.querySelectorAll(".ledger-workspace-row").forEach(function (row) { row.hidden = true; });
      workspaceRow.hidden = !opening;
      editButton.textContent = opening ? "Close review" :
        (entry.review_source === "human_corrected" ? "Edit again" : "Edit finding");
      if (opening) {
        ctx.activeDocumentFrame = documentWorkspace.frame;
        documentWorkspace.open();
        workspaceRow.scrollIntoView({ behavior: "smooth", block: "start" });
        reason.focus();
      } else if (ctx.activeDocumentFrame === documentWorkspace.frame) {
        ctx.activeDocumentFrame = null;
      }
    });
    status.addEventListener("change", function () {
      value.disabled = status.value !== "found";
      value.placeholder = value.disabled ? "Select Found above to enter the correct value" : "Enter the correct value";
    });
    value.disabled = status.value !== "found";
    if (value.disabled) value.placeholder = "Select Found above to enter the correct value";
    save.addEventListener("click", function () {
      if (!requireReviewer()) return;
      if (reason.value.trim().length < 10) {
        correctionStatus.textContent = "Explain the correction in at least 10 characters.";
        return;
      }
      var correctedValue = null;
      try {
        if (status.value === "found") correctedValue = parseCorrectionValue(value.value, entry.value, entry.key);
      } catch (_error) {
        correctionStatus.textContent = "The corrected value is not valid. Use valid JSON for lists or objects.";
        return;
      }
      save.disabled = true;
      correctionStatus.textContent = "Saving correction and rebuilding bidder checks…";
      api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/ledger/corrections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          field_key: entry.key,
          status: status.value,
          value: correctedValue,
          conditions: correctionList(conditions.value),
          exceptions: correctionList(exceptions.value),
          required_documents: correctionList(documents.value),
          reason: reason.value.trim(),
          reviewed_by: reviewer(),
          reviewed_at: nowIso(),
        }),
      }).then(function (res) {
        if (!res.ok) {
          save.disabled = false;
          correctionStatus.textContent = apiError(res.data, "The correction could not be saved.");
          return;
        }
        ctx.draft = res.data.draft;
        ctx.state = res.data.state;
        renderLedger(res.data.ledger);
        renderDraft();
        updateStepper();
        showMessage("Human correction saved. Bidder checks were rebuilt from the reviewed ledger.", "ok");
      });
    });
    host.appendChild(editButton);
    return host;
  }

  function tenderFindingNeedsAttention(entry) {
    if (entry.requirement_level === "reference") return false;
    if (entry.blocking_grounding_issues && entry.blocking_grounding_issues.length) return true;
    if (entry.requirement_level === "always_required") return entry.status !== "found";
    return ["unresolved", "need_more_evidence", "conflict", "unreadable_or_not_checked"].indexOf(entry.status) !== -1;
  }

  function requirementLevelLabel(level) {
    return {
      always_required: "Must confirm",
      conditional: "Required if applicable",
      reference: "Reference only",
    }[level] || "Required if applicable";
  }

  function requirementLevelKind(level) {
    return level === "always_required" ? "fail" : level === "reference" ? "review_note" : "review";
  }

  function renderLedger(ledger) {
    var wrap = byId("ledger-wrap");
    wrap.querySelectorAll("iframe[data-object-url]").forEach(function (frame) {
      URL.revokeObjectURL(frame.getAttribute("data-object-url"));
    });
    wrap.textContent = "";
    var summary = el("section", { className: "tender-summary" });
    summary.appendChild(el("h3", { text: "Tender summary" }));
    summary.appendChild(el("p", { text: ledger.tender_summary.text }));
    var facts = el("div", { className: "summary-facts" });
    ledger.tender_summary.key_facts.forEach(function (fact) {
      var item = el("span", { className: "summary-fact" });
      item.appendChild(el("strong", { text: fact.label + ": " }));
      item.appendChild(document.createTextNode(fact.value));
      facts.appendChild(item);
    });
    summary.appendChild(facts);
    summary.appendChild(el("small", {
      text: ledger.tender_summary.source === "ai_extracted"
        ? "Extracted tender summary; verify important facts below."
        : "Summary assembled from the tender findings below.",
    }));
    wrap.appendChild(summary);
    var entries = ledger.entries.filter(function (entry) { return entry.key !== "tender_summary"; });
    ctx.ledgerEntriesByKey = {};
    entries.forEach(function (entry) { ctx.ledgerEntriesByKey[entry.key] = entry; });
    var blockingFieldKeys = {};
    if (ctx.draft) blockingQuestions().forEach(function (question) {
      if (question.parts && question.parts.kind === "field") blockingFieldKeys[question.parts.key] = true;
    });
    entries.sort(function (left, right) {
      return Number(tenderFindingNeedsAttention(right) || blockingFieldKeys[right.key]) -
        Number(tenderFindingNeedsAttention(left) || blockingFieldKeys[left.key]);
    });
    var attentionCount = entries.filter(function (entry) {
      return tenderFindingNeedsAttention(entry) || blockingFieldKeys[entry.key];
    }).length;
    wrap.appendChild(el("h3", {
      text: attentionCount
        ? attentionCount + " finding" + (attentionCount === 1 ? " needs" : "s need") + " your attention"
        : "No tender findings need correction",
    }));
    wrap.appendChild(el("p", {
      text: attentionCount
        ? "Correct a finding only when it is wrong. You may also mark a requirement Not applicable. The other findings are reference information."
        : "Not found and Not applicable are valid outcomes; you do not need to fill every tender field.",
    }));
    wrap.appendChild(el("div", { className: "summary-facts" }, [
      pill("Must confirm — needed for every evaluation", "fail"),
      pill("Required if applicable — needed only when the tender contains the clause", "review"),
      pill("Reference only — never blocks evaluation", "review_note"),
    ]));
    var table = el("table");
    table.appendChild(
      el("caption", {
        text:
          "Tender review status: " + friendlyStatus(ledger.ledger_status),
      })
    );
    var head = el("tr");
    ["Tender finding", "Status", "Value", "Evidence", "Correct if needed"].forEach(function (label) {
      head.appendChild(el("th", { text: label }));
    });
    table.appendChild(head);
    entries.forEach(function (entry) {
      var row = el("tr");
      if (!tenderFindingNeedsAttention(entry) && !blockingFieldKeys[entry.key]) {
        row.hidden = true;
        row.setAttribute("data-secondary-finding", "true");
      }
      row.id = "ledger-field-" + entry.key;
      var findingCell = el("td");
      findingCell.appendChild(el("strong", { text: entry.label || entry.key }));
      findingCell.appendChild(el("div", {}, [
        pill(requirementLevelLabel(entry.requirement_level), requirementLevelKind(entry.requirement_level)),
      ]));
      row.appendChild(findingCell);
      var statusCell = el("td");
      statusCell.appendChild(pill(
        blockingFieldKeys[entry.key] ? "Needs review" : friendlyStatus(entry.status),
        blockingFieldKeys[entry.key] ? "conflict" : entry.status
      ));
      if (entry.verified) {
        statusCell.appendChild(pill(entry.status === "found" ? "evidence verified" : "search completed", "pass"));
      }
      row.appendChild(statusCell);
      row.appendChild(
        el("td", {
          className: "ledger-value",
          text: entry.value == null || entry.value === "" ? "—" : ledgerValueToText(entry.value),
        })
      );
      var evidenceCell = el("td", { className: "ledger-evidence" });
      if (!entry.evidence.length) evidenceCell.textContent = "—";
      else evidenceCell.appendChild(evidenceGroup(entry.evidence));
      row.appendChild(evidenceCell);
      var workspaceRow = el("tr", { className: "ledger-workspace-row" });
      workspaceRow.hidden = true;
      var workspaceCell = el("td", { colspan: "5" });
      workspaceRow.appendChild(workspaceCell);
      var reviewCell = el("td");
      reviewCell.appendChild(correctionEditor(entry, ledger, workspaceRow, workspaceCell));
      row.appendChild(reviewCell);
      table.appendChild(row);
      if (ledger.editable) table.appendChild(workspaceRow);
    });
    wrap.appendChild(table);
    var hiddenCount = entries.length - attentionCount;
    if (hiddenCount) {
      var showAll = el("button", { className: "secondary", type: "button", text: "View the other " + hiddenCount + " tender findings" });
      showAll.addEventListener("click", function () {
        var opening = showAll.getAttribute("data-open") !== "true";
        table.querySelectorAll("tr[data-secondary-finding]").forEach(function (row) { row.hidden = !opening; });
        showAll.setAttribute("data-open", opening ? "true" : "false");
        showAll.textContent = opening ? "Hide the other tender findings" : "View the other " + hiddenCount + " tender findings";
      });
      wrap.appendChild(showAll);
    }
    if (ctx.draft) renderBlockingQuestions();
  }


  Object.assign(ctx, {
    loadLedger: loadLedger,
    evidenceDetails: evidenceDetails,
    evidenceGroup: evidenceGroup,
    correctionValueToText: correctionValueToText,
    parseCorrectionValue: parseCorrectionValue,
    correctionList: correctionList,
    documentReviewWorkspace: documentReviewWorkspace,
    correctionEditor: correctionEditor,
    tenderFindingNeedsAttention: tenderFindingNeedsAttention,
    requirementLevelLabel: requirementLevelLabel,
    requirementLevelKind: requirementLevelKind,
    renderLedger: renderLedger,
  });
}

