"use strict";
/* Evaluation result DOM views. */

import {
  criterionKind,
  criterionLabel,
  resultChecks,
  resultCheckKey,
  resultCounts,
  resultExplanation,
  resultKind,
  resultLabel,
  shortId,
} from "./results-model.js";

export function installResultsView(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var CHECKLIST_GROUPS = ctx.CHECKLIST_GROUPS;
  var CHECKLIST_GROUP_ORDER = ctx.CHECKLIST_GROUP_ORDER;
  var api = function () { return ctx.api.apply(ctx, arguments); };
  var apiError = function () { return ctx.apiError.apply(ctx, arguments); };
  var apiFetch = function () { return ctx.apiFetch.apply(ctx, arguments); };
  var bidderMetadata = function () { return ctx.bidderMetadata.apply(ctx, arguments); };
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var criterionDisplayParts = function () { return ctx.criterionDisplayParts.apply(ctx, arguments); };
  var criterionGroup = function () { return ctx.criterionGroup.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var friendlyKey = function () { return ctx.friendlyKey.apply(ctx, arguments); };
  var operationErrorText = function () { return ctx.operationErrorText.apply(ctx, arguments); };
  var operationStatus = function () { return ctx.operationStatus.apply(ctx, arguments); };
  var pageUrl = function () { return ctx.pageUrl.apply(ctx, arguments); };
  var pill = function () { return ctx.pill.apply(ctx, arguments); };
  var renderHumanReview = function () { return ctx.renderHumanReview.apply(ctx, arguments); };
  var renderNextAsk = function () { return ctx.renderNextAsk.apply(ctx, arguments); };
  var resultPolicyLine = function () { return ctx.resultPolicyLine.apply(ctx, arguments); };
  var runEvaluate = function () { return ctx.runEvaluate.apply(ctx, arguments); };
  var showMessage = function () { return ctx.showMessage.apply(ctx, arguments); };

  function renderClaim(claim) {
    var box = el("details", { className: "claim-block" });
    box.appendChild(
      el("summary", { text: friendlyKey(claim.fact_name) + ": " + String(claim.value) })
    );
    var body = el("div", { className: "claim-body" });
    claim.source_refs.forEach(function (ref) {
      var locator = (ref.file_name || ref.document_id) + " p." + (ref.page == null ? "?" : ref.page);
      body.appendChild(el("blockquote", { text: "“" + ref.quote + "” — " + locator }));
    });
    var technical = el("details", { className: "result-technical" });
    technical.appendChild(el("summary", { text: "Technical trace" }));
    technical.appendChild(el("p", { text: "Extraction: " + (claim.extraction_method || "unknown") }));
    technical.appendChild(el("p", { text: "Record: " + (claim.role_id || "not assigned") }));
    body.appendChild(technical);
    box.appendChild(body);
    return box;
  }

  function renderClaimPages(claims) {
    var browser = el("div", { className: "evidence-browser" });
    var seen = {};
    var refs = [];
    claims.forEach(function (claim) {
      claim.source_refs.forEach(function (ref) {
        if (!ref.page_image_url || seen[ref.page_image_url]) return;
        seen[ref.page_image_url] = true;
        refs.push(ref);
      });
    });
    if (!refs.length) {
      browser.appendChild(el("p", { text: "No page image is available; use the cited bidder text above." }));
      return browser;
    }
    var toolbar = el("div", { className: "evidence-page-tabs" });
    var caption = el("strong", { className: "evidence-page-caption" });
    var image = el("img", { className: "claim-page-image", loading: "lazy" });
    var openPdf = el("button", { className: "inline-pdf-button", type: "button", text: "Open full PDF here" });
    var selected = refs[0];

    function selectPage(ref) {
      selected = ref;
      caption.textContent = (ref.file_name || ref.document_id) + " · page " + ref.page;
      image.src = ref.page_image_url;
      image.alt = "Bid evidence page " + ref.page + " from " + (ref.file_name || ref.document_id);
      toolbar.querySelectorAll("button").forEach(function (button) {
        button.classList.toggle("active", button.getAttribute("data-page-url") === ref.page_image_url);
      });
    }
    refs.forEach(function (ref) {
      var button = el("button", {
        type: "button",
        text: (ref.file_name || ref.document_id) + " p." + ref.page,
        "data-page-url": ref.page_image_url,
      });
      button.addEventListener("click", function () { selectPage(ref); });
      toolbar.appendChild(button);
    });
    openPdf.addEventListener("click", function () {
      var pane = openPdf.closest(".bidder-review-pane");
      if (!pane || !selected.document_url) return;
      var shell = pane.querySelector(".inline-pdf-shell");
      if (!shell) {
        shell = el("div", { className: "inline-pdf-shell" }, [
          el("p", { className: "inline-pdf-status" }),
          el("iframe", { className: "inline-bid-pdf", title: "Bidder source PDF" }),
        ]);
        browser.appendChild(shell);
      }
      var status = shell.querySelector(".inline-pdf-status");
      var frame = shell.querySelector("iframe");
      status.textContent = "Loading " + (selected.file_name || selected.document_id) + "...";
      apiFetch(selected.document_url).then(function (response) {
        if (!response.ok || (response.headers.get("content-type") || "").indexOf("application/pdf") !== 0) throw new Error("unavailable");
        return response.blob();
      }).then(function (blob) {
        var previous = frame.getAttribute("data-object-url");
        if (previous) URL.revokeObjectURL(previous);
        var objectUrl = URL.createObjectURL(blob);
        frame.src = objectUrl + "#page=" + selected.page;
        frame.setAttribute("data-object-url", objectUrl);
        status.textContent = (selected.file_name || selected.document_id) + " · page " + selected.page + " · click inside the PDF, then use Ctrl+F";
      }).catch(function () { status.textContent = "This PDF could not be opened."; });
    });
    browser.appendChild(toolbar);
    browser.appendChild(el("div", { className: "evidence-page-header" }, [caption, openPdf]));
    browser.appendChild(image);
    selectPage(selected);
    return browser;
  }

  function decisionStats(summary) {
    var stats = el("div", { className: "results-summary" });
    [
      ["Rules evaluated", summary.total || 0],
      ["Passed", summary.passed || 0],
      ["Failed / missing", summary.failed || 0],
      ["Needs review", summary.review || 0],
      ["Not evaluated", summary.not_evaluated || 0],
    ].forEach(function (item) {
      stats.appendChild(el("div", { className: "result-stat" }, [
        el("span", { text: item[0] }),
        el("strong", { text: String(item[1]) }),
      ]));
    });
    return stats;
  }

  function renderFindingExplanation(check) {
    var explanation = check.finding_explanation && typeof check.finding_explanation === "object"
      ? check.finding_explanation
      : null;
    if (!explanation) return null;
    if (["pass", "passed", "meets_requirement", "not_applicable"].indexOf(String(check.status || "").toLowerCase()) !== -1) return null;
    var section = el("section", { className: "result-detail-section result-next-ask" }, [
      el("span", { className: "decision-label", text: "Evaluation finding" }),
    ]);
    if (explanation.requirement) {
      section.appendChild(el("h4", { text: "What the tender requires" }));
      section.appendChild(el("p", { text: String(explanation.requirement) }));
    }
    if (explanation.issue) {
      section.appendChild(el("div", { className: "next-ask-decision" }, [
        el("h4", { text: "What is wrong or unresolved" }),
        el("strong", { text: String(explanation.issue) }),
      ]));
    }
    var observed = Array.isArray(explanation.observed) ? explanation.observed.filter(Boolean) : [];
    if (observed.length) {
      section.appendChild(el("h4", { text: "What was found" }));
      var list = el("ul", { className: "compliance-bullets" });
      observed.forEach(function (item) { list.appendChild(el("li", { text: String(item) })); });
      section.appendChild(list);
    }
    if (explanation.reviewer_action) {
      section.appendChild(el("div", { className: "next-ask-action" }, [
        el("h4", { text: "Reviewer action" }),
        el("strong", { text: String(explanation.reviewer_action) }),
      ]));
    }
    return section;
  }

  function renderResultEvidence(vendor, check, displayName) {
    var panel = el("div", { className: "result-detail-content" });
    var explanation = renderFindingExplanation(check);
    if (explanation) panel.appendChild(explanation);
    panel.appendChild(renderNextAsk(check));

    var tenderEvidence = el("details", { className: "result-detail-section result-secondary-section" });
    tenderEvidence.appendChild(el("summary", {}, [
      el("span", { className: "decision-label", text: "Tender evidence" }),
    ]));
    if (!(check.tender_evidence || []).length) {
      tenderEvidence.appendChild(el("p", { text: "No tender-page citation is linked to this check." }));
    }
    (check.tender_evidence || []).forEach(function (ref) {
      var source = el("details", { className: "tender-source" });
      source.appendChild(el("summary", {
        text: (ref.file_name || "Tender source") + (ref.page ? " · page " + ref.page : ""),
      }));
      if (ref.page_image_url) {
        source.appendChild(el("img", {
          className: "review-page-image",
          src: ref.page_image_url,
          alt: "Tender evidence page " + ref.page,
          loading: "lazy",
        }));
      }
      if (ref.quote) source.appendChild(el("blockquote", { text: ref.quote }));
      tenderEvidence.appendChild(source);
    });
    panel.appendChild(tenderEvidence);

    var bidderEvidence = el("details", { className: "result-detail-section result-secondary-section" });
    bidderEvidence.appendChild(el("summary", {}, [
      el("span", { className: "decision-label", text: displayName + " evidence" }),
    ]));
    if (!(check.claims || []).length) {
      bidderEvidence.appendChild(el("p", { text: "No usable bidder evidence was linked to this check." }));
    } else {
      (check.claims || []).forEach(function (claim) { bidderEvidence.appendChild(renderClaim(claim)); });
      bidderEvidence.appendChild(renderClaimPages(check.claims));
    }
    panel.appendChild(bidderEvidence);

    var review = el("details", { className: "result-detail-section result-human-review result-secondary-section" });
    review.appendChild(el("summary", {}, [
      el("span", { className: "decision-label", text: "Human review" }),
    ]));
    var reviewPanel = renderHumanReview(vendor, check);
    reviewPanel.hidden = false;
    review.appendChild(reviewPanel);
    panel.appendChild(review);
    return panel;
  }

  function renderResultWorkspace(vendors, vendorIds, metadata) {
    var shell = el("section", { className: "result-review-shell" });

    function render() {
      shell.textContent = "";
      if (!ctx.selectedResultVendor || vendorIds.indexOf(ctx.selectedResultVendor) === -1) {
        ctx.selectedResultVendor = vendorIds[0];
      }
      var vendor = ctx.selectedResultVendor;
      var data = vendors[vendor];
      var displayName = data.vendor_name || (metadata[vendor] || {}).displayName || vendor;
      var allChecks = resultChecks(data);
      var families = CHECKLIST_GROUP_ORDER.filter(function (family) {
        return allChecks.some(function (check) { return criterionGroup(check) === family; });
      });
      if (ctx.selectedResultFamily !== "all" && families.indexOf(ctx.selectedResultFamily) === -1) ctx.selectedResultFamily = "all";

      var bidderSelect = el("select", { "aria-label": "Select bidder result" });
      vendorIds.forEach(function (vendorId) {
        var option = el("option", {
          value: vendorId,
          text: vendors[vendorId].vendor_name || (metadata[vendorId] || {}).displayName || vendorId,
        });
        if (vendorId === vendor) option.selected = true;
        bidderSelect.appendChild(option);
      });
      bidderSelect.addEventListener("change", function () {
        ctx.selectedResultVendor = bidderSelect.value;
        ctx.selectedResultFamily = "all";
        ctx.selectedResultKind = "all";
        ctx.selectedResultCheckKey = null;
        ctx.resultSearch = "";
        render();
      });

      var search = el("input", {
        type: "search",
        value: ctx.resultSearch,
        placeholder: "Search requirement, proof, finding, or evidence",
        "aria-label": "Search evaluated checks",
      });
      search.value = ctx.resultSearch;
      search.addEventListener("input", function () {
        ctx.resultSearch = search.value;
        ctx.selectedResultCheckKey = null;
        render();
        var replacement = shell.querySelector('.result-search input');
        if (replacement) {
          replacement.focus();
          replacement.setSelectionRange(ctx.resultSearch.length, ctx.resultSearch.length);
        }
      });

      var statusFilter = el("select", { "aria-label": "Filter result status" });
      [["all", "All decisions"], ["fail", "Failed or missing"], ["review", "Needs review"], ["not_evaluated", "Processing incomplete"], ["pass", "Passed"]].forEach(function (choice) {
        var option = el("option", { value: choice[0], text: choice[1] });
        if (choice[0] === ctx.selectedResultKind) option.selected = true;
        statusFilter.appendChild(option);
      });
      statusFilter.addEventListener("change", function () {
        ctx.selectedResultKind = statusFilter.value;
        ctx.selectedResultCheckKey = null;
        render();
      });

      var summary = data.decision_summary || {};
      var outcomeKind = resultKind(data);
      shell.appendChild(el("div", { className: "result-bidder-head" }, [
        el("div", {}, [
          el("span", { className: "decision-label", text: "Bidder result " + (vendorIds.indexOf(vendor) + 1) + " of " + vendorIds.length }),
          el("div", { className: "result-bidder-title" }, [
            el("h3", { text: displayName }),
            pill(summary.label || resultLabel(outcomeKind), outcomeKind),
          ]),
          el("p", { text: summary.answer || resultExplanation(resultKind(data)) }),
          el("div", { className: "result-bidder-metrics" }, [
            pill(String(summary.passed || 0) + " passed", "pass"),
            pill(String(summary.failed || 0) + " failed or missing", Number(summary.failed || 0) ? "fail" : "pass"),
            pill(String(summary.review || 0) + " need review", Number(summary.review || 0) ? "review" : "pass"),
            pill(String(summary.not_evaluated || 0) + " processing incomplete", Number(summary.not_evaluated || 0) ? "not_evaluated" : "pass"),
          ]),
        ]),
        el("div", { className: "result-bidder-control field" }, [
          el("label", { text: "Bidder" }),
          bidderSelect,
        ]),
      ]));
      shell.appendChild(el("div", { className: "result-toolbar" }, [
        el("div", { className: "result-search" }, [search]),
        statusFilter,
      ]));

      var categoryPane = el("nav", { className: "result-category-pane", "aria-label": "Evaluation result categories" });
      categoryPane.appendChild(el("h3", { text: "Categories" }));
      [{ key: "all", label: "All checks" }].concat(families.map(function (family) {
        return { key: family, label: CHECKLIST_GROUPS[family].label };
      })).forEach(function (choice) {
        var categoryChecks = choice.key === "all" ? allChecks : allChecks.filter(function (check) { return criterionGroup(check) === choice.key; });
        var counts = resultCounts(categoryChecks);
        var button = el("button", {
          className: "result-category-button" + (choice.key === ctx.selectedResultFamily ? " active" : ""),
          type: "button",
          "aria-pressed": choice.key === ctx.selectedResultFamily ? "true" : "false",
        }, [
          el("strong", { text: choice.label }),
          el("span", { text: counts.pass + " passed · " + counts.fail + " failed · " + counts.review + " review · " + counts.not_evaluated + " incomplete" }),
          el("b", { text: String(counts.total) }),
        ]);
        button.addEventListener("click", function () {
          ctx.selectedResultFamily = choice.key;
          ctx.selectedResultCheckKey = null;
          render();
        });
        categoryPane.appendChild(button);
      });

      var query = ctx.resultSearch.trim().toLowerCase();
      var visible = allChecks.filter(function (check) {
        if (ctx.selectedResultFamily !== "all" && criterionGroup(check) !== ctx.selectedResultFamily) return false;
        if (ctx.selectedResultKind !== "all" && criterionKind(check) !== ctx.selectedResultKind) return false;
        if (!query) return true;
        var nextAsk = check.next_ask || {};
        return [check.title, check.requirement, check.required_proof, check.reason, nextAsk.missing_paper]
          .concat(nextAsk.officer_problem || [])
          .concat(nextAsk.officer_actions || [])
          .concat(nextAsk.already_have || [])
          .concat((check.claims || []).map(function (claim) { return claim.fact_name + " " + claim.value; }))
          .join(" ").toLowerCase().indexOf(query) !== -1;
      });
      var selected = visible.find(function (check, index) { return resultCheckKey(check, index) === ctx.selectedResultCheckKey; });
      if (!selected) {
        selected = visible.find(function (check) { return criterionKind(check) === "fail"; }) ||
          visible.find(function (check) { return criterionKind(check) === "review"; }) || visible[0] || null;
        ctx.selectedResultCheckKey = selected ? resultCheckKey(selected, visible.indexOf(selected)) : null;
      }

      var listPane = el("section", { className: "result-list-pane" });
      listPane.appendChild(el("div", { className: "result-list-head" }, [
        el("div", {}, [
          el("span", { className: "decision-label", text: "Evaluation checks" }),
          el("h3", { text: ctx.selectedResultFamily === "all" ? "All evaluated checks" : CHECKLIST_GROUPS[ctx.selectedResultFamily].label }),
        ]),
        el("span", { text: visible.length + " of " + allChecks.length }),
      ]));
      if (!visible.length) {
        listPane.appendChild(el("div", { className: "checklist-empty" }, [
          el("strong", { text: "No results match these filters." }),
          el("p", { text: "Clear the search or choose another category or decision." }),
        ]));
      }
      visible.forEach(function (check, index) {
        var key = resultCheckKey(check, index);
        var kind = criterionKind(check);
        var display = criterionDisplayParts(check);
        var policyLine = resultPolicyLine(check);
        var row = el("button", {
          className: "result-check-row " + kind + (key === ctx.selectedResultCheckKey ? " selected" : ""),
          type: "button",
          "aria-pressed": key === ctx.selectedResultCheckKey ? "true" : "false",
        }, [
          el("span", { className: "result-check-copy" }, [
            el("strong", { text: display.requirement }),
            el("small", { text: policyLine || display.topic }),
          ]),
          el("span", { className: "result-check-proof", text: (check.claims || []).length + " evidence item" + ((check.claims || []).length === 1 ? "" : "s") }),
          el("span", { className: "result-check-status " + kind, text: criterionLabel(kind, check) }),
          el("span", { className: "result-check-arrow", text: "›", "aria-hidden": "true" }),
        ]);
        row.addEventListener("click", function () {
          ctx.selectedResultCheckKey = key;
          render();
        });
        listPane.appendChild(row);
      });

      var detailPane = el("aside", { className: "result-detail-pane" });
      if (!selected) {
        detailPane.appendChild(el("div", { className: "checklist-detail-empty" }, [
          el("strong", { text: "Select an evaluated check" }),
          el("p", { text: "The policy next step for that check will appear here." }),
        ]));
      } else {
        var selectedDisplay = criterionDisplayParts(selected);
        var selectedKind = criterionKind(selected);
        detailPane.appendChild(el("div", { className: "result-detail-head" }, [
          el("div", {}, [
            el("span", { className: "eyebrow", text: CHECKLIST_GROUPS[criterionGroup(selected)].label }),
            el("h3", { text: selectedDisplay.requirement }),
          ]),
          pill(criterionLabel(selectedKind, selected), selectedKind),
        ]));
        detailPane.appendChild(renderResultEvidence(vendor, selected, displayName));
      }

      shell.appendChild(el("div", { className: "result-workspace" }, [categoryPane, listPane, detailPane]));
    }

    render();
    return shell;
  }

  function renderAwardSuggestion(award) {
    if (!award || typeof award !== "object") return null;
    var preferredName = String(award.preferred_vendor_name || "").trim();
    var preferredKey = String(award.preferred_vendor_key || "").trim();
    var awardable = Boolean(award.awardable_now);
    var preferredBid = (Array.isArray(award.bidders) ? award.bidders : []).find(function (bidder) {
      return (preferredKey && bidder.vendor_key === preferredKey) ||
        (preferredName && String(bidder.vendor_name || "").toLowerCase() === preferredName.toLowerCase());
    }) || {};
    var hardWalls = Array.isArray(preferredBid.hard_walls) ? preferredBid.hard_walls.filter(Boolean) : [];
    var askOnce = Array.isArray(preferredBid.ask_once) ? preferredBid.ask_once.filter(Boolean) : [];
    var decisiveGap = awardable
      ? "No unresolved technical qualification blocker"
      : String(hardWalls[0] || askOnce[0] || "No technically responsive bidder");
    var card = el("section", { className: "award-suggestion" + (award.awardable_now ? " awardable" : " blocked") });
    card.appendChild(el("div", { className: "award-suggestion-head" }, [
      el("div", { className: "award-ai-identity" }, [
        el("span", { className: "award-ai-mark", text: "✦", "aria-hidden": "true" }),
        el("span", { className: "award-ai-copy" }, [
          el("span", { className: "decision-label", text: "AI recommendation" }),
          el("small", { text: "Decision support · Human approval required" }),
        ]),
      ]),
      el("span", {
        className: "award-status " + (awardable ? "awardable" : "blocked"),
        text: awardable ? "Ready for approval" : "Award blocked",
      }),
    ]));

    var headline = el("h3");
    if (preferredName) {
      headline.appendChild(document.createTextNode(awardable ? "Proceed with " : "Do not award yet. Review "));
      headline.appendChild(el("span", { className: "award-preferred-bidder", text: preferredName }));
      headline.appendChild(document.createTextNode(awardable ? ", subject to approval." : " first."));
    } else {
      headline.textContent = "Do not award. No bidder is technically responsive.";
    }
    var supportingText = awardable && preferredName
      ? "The technical evaluation supports moving " + preferredName + " to the controlled commercial stage."
      : preferredName
        ? "No bidder is technically qualified. " + preferredName + " has the shortest path to a responsive bid."
        : "No bidder currently satisfies the frozen technical checklist.";
    card.appendChild(headline);
    card.appendChild(el("p", { className: "award-summary", text: supportingText }));
    card.appendChild(el("div", { className: "award-guidance" }, [
      el("div", { className: "award-guidance-item" }, [
        el("span", { text: "Decision" }),
        el("strong", { text: awardable ? "Proceed subject to committee approval" : "Keep price bids closed" }),
      ]),
      el("div", { className: "award-guidance-item" }, [
        el("span", { text: "Decisive gap" }),
        el("strong", { text: decisiveGap }),
      ]),
      el("div", { className: "award-guidance-item" }, [
        el("span", { text: "Next action" }),
        el("strong", {
          text: awardable && preferredName
            ? "Record committee approval for " + preferredName
            : preferredName
              ? "Issue one ask-once clarification to " + preferredName
              : "Review unresolved qualification gaps",
        }),
      ]),
    ]));

    var footerChildren = [el("p", { text: "Committee approval is required before price opening." })];
    if (preferredKey && preferredName) {
      var viewGaps = el("button", { type: "button", className: "award-gap-action", text: "View " + preferredName + " gaps  ›" });
      viewGaps.addEventListener("click", function () {
        window.location.href = pageUrl("results") + "?vendor=" + encodeURIComponent(preferredKey);
      });
      footerChildren.push(viewGaps);
    }
    card.appendChild(el("div", { className: "award-suggestion-footer" }, footerChildren));
    return card;
  }

  function renderResults(vendors, unevaluatedRequirements, comparison, complete) {
    var host = byId("results");
    host.textContent = "";
    var vendorIds = Object.keys(vendors);
    var requestedVendor = new URLSearchParams(window.location.search).get("vendor");
    if (requestedVendor && vendors[requestedVendor]) {
      ctx.selectedResultVendor = requestedVendor;
    }
    if (!vendorIds.length) {
      host.appendChild(el("div", { className: "result-empty", text: "No bidder results are available yet." }));
      return;
    }
    vendorIds.forEach(function (vendor) {
      var evaluationStatus = operationStatus(vendors[vendor]);
      if (evaluationStatus !== "succeeded" && evaluationStatus !== "partial") {
        var pendingLabel = evaluationStatus === "running" ? "Evaluation running" :
          evaluationStatus === "retrying" ? "Evaluation retrying" :
          evaluationStatus === "queued" ? "Evaluation queued" :
          evaluationStatus === "failed" ? "Evaluation failed" :
          evaluationStatus === "cancelled" ? "Evaluation cancelled" : "Evaluation not started";
        var pendingMessage = evaluationStatus === "running" || evaluationStatus === "queued"
          ? "This bidder is still being processed. Refresh results to see the completed decision."
          : evaluationStatus === "retrying"
          ? "The backend is retrying this bidder safely. The job has not been marked as lost."
          : evaluationStatus === "failed"
          ? operationErrorText(vendors[vendor], "This bidder evaluation failed. Review the error and retry when ready.")
          : evaluationStatus === "cancelled"
          ? operationErrorText(vendors[vendor], "This bidder evaluation was cancelled.")
          : "This bidder has no completed evaluation yet. Its classification can be approved and evaluated independently.";
        host.appendChild(el("div", { className: "results-alert" }, [
          el("strong", { text: (vendors[vendor].vendor_name || vendor) + ": " + pendingLabel }),
          el("p", { text: pendingMessage }),
        ]));
        return;
      }
      if (evaluationStatus === "partial") {
        var item = vendors[vendor].decision_summary || {};
        host.appendChild(el("div", { className: "results-alert" }, [
          el("strong", { text: (vendors[vendor].vendor_name || vendor) + ": Evaluation complete with review items" }),
          el("p", { text: item.answer || "Successful evaluation work was preserved, but some items require human review." }),
        ]));
      }
    });
    if (unevaluatedRequirements.length) {
      var incomplete = el("div", {
        className: "results-alert",
        text: "Not evaluated: " + unevaluatedRequirements.map(function (item) { return item.title; }).join(", ") +
          ". The automated result is incomplete; keep every bidder in human review until these frozen tender requirements are evaluated.",
      });
      var evaluateMissing = el("button", { type: "button", text: "Evaluate missing requirements" });
      evaluateMissing.addEventListener("click", runEvaluate);
      incomplete.appendChild(evaluateMissing);
      host.appendChild(incomplete);
    }
    var metadata = bidderMetadata();
    host.appendChild(renderResultWorkspace(vendors, vendorIds, metadata));
    var effectiveKinds = vendorIds.map(function (vendor) { return resultKind(vendors[vendor]); });
    if (!unevaluatedRequirements.length && effectiveKinds.every(function (kind) { return kind === "pass"; })) {
      host.appendChild(el("div", {
        className: "results-alert results-ready",
        text: "Qualification review complete. All displayed requirements are confirmed. Committee approval is required before opening commercial bids.",
      }));
    } else if (!unevaluatedRequirements.length) {
      host.appendChild(el("div", {
        className: "results-alert",
        text: "Commercial bids must remain closed while any bidder has a failed or unconfirmed requirement.",
      }));
    }
    var reportAction = el("button", { type: "button", text: "Continue to report" });
    reportAction.addEventListener("click", function () {
      api("/workbench/projects/" + encodeURIComponent(ctx.projectId) + "/report/generate", { method: "POST" }).then(function (res) {
        if (!res.ok) return showMessage(apiError(res.data, "The report is not ready yet."), "error");
        window.location.assign(pageUrl("report"));
      });
    });
    host.appendChild(el("div", { className: "row" }, [reportAction]));
  }

  function renderReviewLog(data) {
    var wrap = byId("review-log-wrap");
    var preview = byId("review-log-preview");
    var details = byId("review-log-details");
    wrap.textContent = "";
    preview.textContent = "";
    var records = data.records || [];
    if (!records.length) {
      preview.appendChild(
        el("p", { text: "No gate records yet — records appear when the contract is frozen, bids are intaken, and the evaluation is reviewed." })
      );
      details.hidden = true;
      return;
    }
    details.hidden = false;
    byId("review-log-summary").textContent = "View complete audit trail (" + records.length + " entries)";
    records.slice(-4).reverse().forEach(function (record) {
      preview.appendChild(el("div", { className: "audit-preview-item" }, [
        el("div", {}, [pill(record.action, record.action), el("small", { text: " #" + record.sequence })]),
        el("div", {}, [el("strong", { text: record.actor_id }), el("small", { text: record.recorded_at })]),
        el("div", {}, [el("strong", { text: record.reason_code }), el("small", { text: record.reason_text })]),
        el("small", { text: record.subject_artifact_name }),
      ]));
    });
    var table = el("table");
    table.appendChild(
      el("caption", {
        text:
          "Namespace " + data.ledger_namespace + " · " + data.record_count +
          " chained record(s) · head " + shortId(data.head_record_id || ""),
      })
    );
    var head = el("tr");
    ["#", "Action", "Reason", "Actor", "Subject artifact", "Recorded at", "Chain"].forEach(function (label) {
      head.appendChild(el("th", { text: label }));
    });
    table.appendChild(head);
    records.forEach(function (record) {
      var row = el("tr");
      row.appendChild(el("td", { text: String(record.sequence) }));
      var actionCell = el("td");
      actionCell.appendChild(pill(record.action, record.action));
      row.appendChild(actionCell);
      var reasonCell = el("td");
      reasonCell.appendChild(pill(record.reason_code, "review"));
      reasonCell.appendChild(el("div", { text: record.reason_text }));
      row.appendChild(reasonCell);
      row.appendChild(el("td", { text: record.actor_id + " (" + record.actor_role + ")" }));
      row.appendChild(el("td", { text: record.subject_artifact_name }));
      row.appendChild(el("td", { text: record.recorded_at }));
      var chainCell = el("td");
      chainCell.appendChild(el("div", { text: "id " + shortId(record.record_id) }));
      chainCell.appendChild(el("div", { text: "prior " + shortId(record.prior_record_id) }));
      row.appendChild(chainCell);
      table.appendChild(row);
    });
    wrap.appendChild(table);
  }

  return {
    renderClaim: renderClaim,
    renderClaimPages: renderClaimPages,
    decisionStats: decisionStats,
    renderResultEvidence: renderResultEvidence,
    renderResultWorkspace: renderResultWorkspace,
    renderAwardSuggestion: renderAwardSuggestion,
    renderResults: renderResults,
    renderReviewLog: renderReviewLog,
  };
}
