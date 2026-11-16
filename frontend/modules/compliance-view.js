"use strict";
/* Tender compliance rendering and finding review views. */

import {
  actionBullets,
  cleanedEvidence,
  complianceCounts,
  complianceFindings,
  complianceRunning,
  coverageList,
  findingMatchesComplianceFilter,
  isGenericLockText,
  isInternalEvidenceId,
  problemBullets,
  resultAlreadyOnFile,
} from "./compliance-model.js";

export function installComplianceView(ctx) {
  /* Cross-screen calls stay behind the shared context; no framework runtime is required. */
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var complianceInfo = function () { return ctx.complianceInfo.apply(ctx, arguments); };
  var criterionKind = function () { return ctx.criterionKind.apply(ctx, arguments); };
  var el = function () { return ctx.el.apply(ctx, arguments); };
  var ensureTenderDocuments = function () { return ctx.ensureTenderDocuments.apply(ctx, arguments); };
  var evidenceDetails = function () { return ctx.evidenceDetails.apply(ctx, arguments); };
  var pill = function () { return ctx.pill.apply(ctx, arguments); };
  var renderMessage = function () { return ctx.renderMessage.apply(ctx, arguments); };
  var resolveComplianceFinding = function () { return ctx.resolveComplianceFinding.apply(ctx, arguments); };
  var selectCompliancePolicy = function () { return ctx.selectCompliancePolicy.apply(ctx, arguments); };
  var setStatusText = function () { return ctx.setStatusText.apply(ctx, arguments); };
  var showOperationActivity = function () { return ctx.showOperationActivity.apply(ctx, arguments); };

  function renderCompliancePolicy(data) {
    var badge = byId("compliance-policy-badge");
    if (!badge) return;
    badge.replaceChildren();
    var policyName = (data && data.policy_name) || "BHEL Purchase Policy (2013) & Works Policy (2016)";
    badge.appendChild(el("div", { className: "compliance-policy-badge-inner" }, [
      el("strong", { text: policyName }),
    ]));
    if (!data || data.status !== "policy_selection_required") return;
    var policies = Array.isArray(data.available_policies) ? data.available_policies : [];
    var policySelect = el("select", { "aria-label": "Governing procurement policy" });
    policySelect.appendChild(el("option", { value: "", text: "Select policy" }));
    policies.forEach(function (policy) {
      policySelect.appendChild(el("option", {
        value: policy.policy_id,
        text: policy.name + (policy.version ? " · " + policy.version : ""),
      }));
    });
    var save = el("button", { className: "secondary", type: "button", text: "Use selected policy" });
    save.addEventListener("click", function () {
      if (!policySelect.value) {
        renderMessage(byId("compliance-errors"), "Select Purchase Policy or Works Policy first.", "error");
        policySelect.focus();
        return;
      }
      save.disabled = true;
      selectCompliancePolicy(policySelect.value).finally(function () { save.disabled = false; });
    });
    badge.appendChild(el("div", { className: "compliance-policy-select" }, [policySelect, save]));
  }

  function renderComplianceSummary(data) {
    var box = byId("compliance-summary");
    box.replaceChildren();
    if (data.status === "policy_selection_required") {
      box.appendChild(el("div", { className: "compliance-summary-state pending" }, [
        el("strong", { text: "Select the governing policy" }),
        el("span", { text: "Choose Purchase Policy or Works Policy before running the tender audit." }),
      ]));
      return;
    }
    if (["not_started", "queued", "running"].indexOf(data.status) !== -1) {
      box.appendChild(el("div", { className: "compliance-summary-state pending" }, [
        el("strong", { text: "Tender audit pending" }),
        el("span", { text: "Run the tender audit to verify clauses against governing procurement policies." }),
      ]));
      return;
    }
    var counts = complianceCounts(data);
    var incomplete = data.status === "incomplete";
    var hasIssues = incomplete || counts.blocking > 0 || counts.review > 0;
    if (!hasIssues) {
      var addressed = counts.audited > 0;
      box.appendChild(el("div", { className: "compliance-summary-state clear" }, [
        el("strong", { text: addressed ? "All policy findings addressed" : "No policy issues" }),
        el("span", { text: "This tender can proceed to the evaluation checklist." }),
      ]));
      return;
    }
    box.appendChild(el("div", {
      className: "compliance-summary-state attention",
    }, [
      el("strong", { text: incomplete ? "Tender audit incomplete" : counts.blocking > 0 ? "Tender has blocking issues" : "Tender review required" }),
      el("span", {
        text: incomplete
          ? "Review the recorded findings and coverage failure below, then run the audit again."
          : (counts.blocking ? counts.blocking + " blocking issue(s). " : "") +
            (counts.review ? counts.review + " item(s) need review before the checklist." : ""),
      }),
    ]));
    [
      { key: "blocking", label: "Blocking", count: counts.blocking, kind: "fail" },
      { key: "review", label: "Needs review", count: counts.review, kind: "review" },
    ].forEach(function (item) {
      if (!item.count) return;
      box.appendChild(el("div", { className: "compliance-summary-stat " + item.kind }, [
        el("span", { text: item.label }),
        el("strong", { text: String(item.count) }),
      ]));
    });
  }

  function renderComplianceCoverage(data) {
    var box = byId("compliance-coverage");
    box.replaceChildren();
    var coverage = (data && data.coverage) || {};
    var failedSections = coverageList(data, data.summary || {}, coverage, "failed_sections");
    if (!failedSections.length && Array.isArray(coverage.failures)) failedSections = coverage.failures;
    if (data.status === "policy_selection_required") return;
    if (data.status !== "incomplete" && coverage.complete !== false && !failedSections.length) return;
    box.appendChild(el("div", { className: "compliance-coverage-alert review" }, [
      pill("Audit could not finish", "review"),
      el("div", {}, [
        el("strong", { text: "The policy check did not complete" }),
        el("p", { text: "Re-run the tender audit. If it fails again, the findings below say what blocked it." }),
      ]),
    ]));
  }

  function renderComplianceFilters(data) {
    var box = byId("compliance-filters");
    box.replaceChildren();
    var counts = complianceCounts(data);
    [
      { key: "blocking", label: "Blocking", count: counts.blocking },
      { key: "review", label: "Needs review", count: counts.review },
      { key: "resolved", label: "Resolved", count: counts.resolved },
      { key: "audited", label: "All records", count: counts.audited },
    ].forEach(function (item) {
      var button = el("button", {
        className: "compliance-filter" + (ctx.complianceFilter === item.key ? " active" : ""),
        type: "button",
        text: item.label + " (" + item.count + ")",
        "aria-pressed": ctx.complianceFilter === item.key ? "true" : "false",
      });
      button.addEventListener("click", function () {
        ctx.complianceFilter = item.key;
        renderComplianceFilters(data);
        renderComplianceFindings(data);
      });
      box.appendChild(button);
    });
  }

  function renderBulletList(items) {
    return el("ul", { className: "compliance-bullets" }, items.map(function (item) {
      return el("li", { text: item });
    }));
  }

  function renderNextAsk(check) {
    if (!check) return null;
    var card = check.next_ask && typeof check.next_ask === "object" ? check.next_ask : {};
    var effect = card.tender_effect && typeof card.tender_effect === "object" ? card.tender_effect : null;
    var action = card.next_action && typeof card.next_action === "object" ? card.next_action : null;
    var clauses = Array.isArray(card.policy_clauses) ? card.policy_clauses : [];
    var governingClauses = Array.isArray(card.governing_policy_clauses)
      ? card.governing_policy_clauses
      : clauses.filter(function (clause) { return clause && (clause.role === "subject" || clause.role === "governing"); });
    var clarificationClauses = Array.isArray(card.clarification_clauses)
      ? card.clarification_clauses
      : clauses.filter(function (clause) { return clause && clause.role === "clarification"; });
    var deviationClauses = Array.isArray(card.deviation_clauses)
      ? card.deviation_clauses
      : clauses.filter(function (clause) { return clause && clause.role === "deviation_loading"; });
    var procedureClauses = Array.isArray(card.procedure_clauses)
      ? card.procedure_clauses
      : clauses.filter(function (clause) { return clause && (clause.role === "process" || clause.role === "bid_procedure"); });
    function clauseKey(clause) {
      return [clause && clause.policy_id, clause && clause.clause_id, clause && clause.role].join("|");
    }
    var representedProcedureClauses = new Set(clarificationClauses.concat(deviationClauses).map(clauseKey));
    var otherProcedureClauses = procedureClauses.filter(function (clause) {
      return !representedProcedureClauses.has(clauseKey(clause));
    });
    var approvals = Array.isArray(card.approval_requirements) ? card.approval_requirements : [];
    var problem = (Array.isArray(card.officer_problem) ? card.officer_problem : []).map(String).filter(Boolean);
    var actions = (Array.isArray(card.officer_actions) ? card.officer_actions : []).map(String).filter(Boolean);
    var missing = String(card.missing_paper || "").trim();
    var already = resultAlreadyOnFile(check);
    var doors = Array.isArray(card.doors) ? card.doors : [];
    if (!missing && criterionKind(check) !== "pass") {
      missing = String(check.required_proof || "").trim();
    }
    var section = el("section", { className: "result-detail-section result-next-ask" }, [
      el("span", { className: "decision-label", text: "Evaluation treatment" }),
    ]);
    if (effect || action) {
      if (problem.length) {
        section.appendChild(el("h4", { text: "Current finding" }));
        section.appendChild(el("p", { text: problem[0] }));
      }
      if (effect) {
        section.appendChild(el("div", { className: "next-ask-decision" }, [
          el("h4", { text: "Effect on this bid" }),
          el("strong", { text: String(effect.label || "Review required") }),
          effect.reason ? el("p", { text: String(effect.reason) }) : null,
        ].filter(Boolean)));
      }
      if (action) {
        section.appendChild(el("div", { className: "next-ask-action" }, [
          el("h4", { text: "Required next action" }),
          el("strong", { text: String(action.label || "Review this check") }),
          action.reason ? el("p", { text: String(action.reason) }) : null,
        ].filter(Boolean)));
      }
      if (already.length) {
        section.appendChild(el("h4", { text: "What is already confirmed" }));
        section.appendChild(renderBulletList(already));
      }
      function appendPolicyGroup(title, items) {
        if (!items.length) return;
        var policy = el("details", { className: "next-ask-policy" });
        policy.appendChild(el("summary", { text: title + " (" + items.length + ")" }));
        items.forEach(function (clause) {
          if (!clause || typeof clause !== "object") return;
          policy.appendChild(el("div", { className: "next-ask-clause" }, [
            el("strong", { text: String(clause.policy_name || "Policy") + " · Clause " + String(clause.clause_id || "") }),
            clause.title ? el("p", { text: String(clause.title) }) : null,
            clause.quote ? el("blockquote", { text: String(clause.quote) }) : null,
          ].filter(Boolean)));
        });
        section.appendChild(policy);
      }
      appendPolicyGroup("Governing BHEL policy", governingClauses);
      appendPolicyGroup("Clarification procedure", clarificationClauses);
      appendPolicyGroup("Deviation and loading", deviationClauses);
      appendPolicyGroup("Bid procedure", otherProcedureClauses);
      if (approvals.length) {
        var approvalItems = approvals.map(function (approval) {
          var who = String(approval && approval.authority_title || "").trim();
          var what = String(approval && approval.action || "").trim();
          return who + (who && what ? " — " : "") + what;
        }).filter(Boolean);
        if (approvalItems.length) {
          section.appendChild(el("h4", { text: "Required approval" }));
          section.appendChild(renderBulletList(approvalItems));
        }
      }
      if (card.communicate_to_all_qualified_bidders === true) {
        section.appendChild(el("p", {
          className: "next-ask-communication",
          text: "Any accepted additional deviation and its loading must be communicated to all qualified bidders before price opening.",
        }));
      }
      return section;
    }
    if (problem.length) {
      section.appendChild(el("h4", { text: "What this means" }));
      section.appendChild(renderBulletList(problem));
    }
    if (actions.length) {
      section.appendChild(el("h4", { text: "What to do" }));
      section.appendChild(renderBulletList(actions));
    }
    if (missing) {
      section.appendChild(el("h4", { text: "Missing paper" }));
      section.appendChild(el("p", { text: missing }));
    }
    if (already.length) {
      section.appendChild(el("h4", { text: "What is already confirmed" }));
      section.appendChild(renderBulletList(already));
    }
    doors.forEach(function (door) {
      if (!door || typeof door !== "object") return;
      var open = door.open === true;
      var title = (open ? "Door still open: " : "Door closed: ") + String(door.label || door.id || "Policy path");
      var line = el("p", { className: open ? "next-ask-door open" : "next-ask-door closed" }, [
        el("strong", { text: title }),
      ]);
      if (door.clause_id) {
        line.appendChild(document.createTextNode(" (Clause " + door.clause_id + ")"));
      }
      section.appendChild(line);
      if (door.reason) section.appendChild(el("p", { text: String(door.reason) }));
      (Array.isArray(door.approvals) ? door.approvals : []).forEach(function (approval) {
        if (!approval || typeof approval !== "object") return;
        var who = String(approval.authority_title || "").trim();
        var mayDo = String(approval.action || "").trim();
        if (!who) return;
        section.appendChild(el("p", { text: who + (mayDo ? " — " + mayDo : "") }));
      });
    });
    if (!problem.length && !actions.length && !missing && !already.length && !doors.length) {
      section.appendChild(el("p", { text: "No missing paper is recorded for this check." }));
    }
    return section;
  }

  function renderComplianceFinding(finding, expanded) {
    var resolution = finding.resolution || { status: "open" };
    var settled = resolution.status === "resolved" || resolution.status === "waived";
    var evidence = Array.isArray(finding.tender_evidence) ? finding.tender_evidence : [];
    var primaryEvidence = evidence[0] || {};
    var isViolation = finding.status === "violation";
    var isPackageFinding = String(finding.finding_id || "").indexOf("rule-package-") === 0;
    var issueTitle = finding.title || (isPackageFinding ? "Tender package is incomplete" : "Tender provision requires review");

    var pillLabel = settled
      ? (resolution.status === "resolved" ? "Resolved" : "Waived")
      : (isViolation ? "Blocking Issue" : "Needs Review");
    var pillKind = settled ? "pass" : (isViolation ? "fail" : "review");

    var head = el("summary", { className: "compliance-finding-head" }, [
      pill(pillLabel, pillKind),
      el("div", { className: "compliance-finding-title" }, [
        el("strong", { text: issueTitle }),
      ]),
      primaryEvidence.page ? el("span", { className: "compliance-finding-page", text: "Page " + primaryEvidence.page }) : null,
    ].filter(Boolean));

    var visibleEvidence = cleanedEvidence(finding);
    var tenderSection = el("section", { className: "compliance-finding-section tender" }, [
      el("h4", { text: "Tender Observation" }),
    ]);

    if (visibleEvidence.length) {
      tenderSection.appendChild(el("div", { className: "compliance-evidence" }, visibleEvidence.map(function (item) {
        var evidenceDocumentId = String(item.document_id || "").trim();
        var unitDocument = String(item.unit_id || "").match(/^(.*)-p\d+(?:-|$)/);
        if (!evidenceDocumentId && unitDocument && !isInternalEvidenceId(unitDocument[1])) {
          evidenceDocumentId = unitDocument[1];
        }
        var sourceLabel = el("strong", { text: item.label });
        if (evidenceDocumentId && !isInternalEvidenceId(evidenceDocumentId) && item.label === "Tender") {
          ensureTenderDocuments().then(function (documents) {
            var document = documents.find(function (candidate) { return candidate.document_id === evidenceDocumentId; });
            if (document) sourceLabel.textContent = document.file_name || "Tender";
          }).catch(function () {});
        }
        var sourceRow = el("div", { className: "compliance-source-row" }, [
          sourceLabel,
          item.page ? el("span", { className: "meta", text: "Page " + item.page }) : null,
        ].filter(Boolean));
        var article = el("article", {}, [sourceRow, el("blockquote", { text: item.quote })]);
        if (evidenceDocumentId && item.page && !isInternalEvidenceId(evidenceDocumentId)) {
          var previewHost = el("div", { className: "compliance-source-preview", "aria-live": "polite" });
          var openPage = el("button", { className: "secondary preview-btn", type: "button", text: "View page " + item.page });
          openPage.addEventListener("click", function () {
            openPage.disabled = true;
            ensureTenderDocuments().then(function (documents) {
              var document = documents.find(function (candidate) { return candidate.document_id === evidenceDocumentId; });
              if (!document || !document.page_url_template) throw new Error("source unavailable");
              var ref = Object.assign({}, item, {
                document_id: evidenceDocumentId,
                page_image_url: document.page_url_template.replace("{page}", String(item.page)),
              });
              previewHost.replaceChildren(evidenceDetails([ref]));
              previewHost.querySelector("details").open = true;
            }).catch(function () {
              previewHost.textContent = "The cited page could not be loaded.";
            }).then(function () { openPage.disabled = false; });
          });
          article.appendChild(openPage);
          article.appendChild(previewHost);
        }
        return article;
      })));
    } else {
      tenderSection.appendChild(el("p", { className: "compliance-missing-evidence", text: "No direct tender passage was cited for this finding." }));
    }

    var diagnosis = problemBullets(finding).filter(function (line) { return line && !isGenericLockText(line); });
    if (diagnosis.length) {
      tenderSection.appendChild(el("div", { className: "compliance-diagnosis" }, [
        el("strong", { text: "Why this is a problem" }),
        renderBulletList(diagnosis),
      ]));
    }

    var policyReference = finding.policy_reference || {};
    var clauseElements = [
      el("h4", { text: "Governing Policy Rule" }),
      el("div", { className: "compliance-policy-clause-tag" }, [
        el("strong", {
          text: finding.clause_id
            ? "Clause " + finding.clause_id + (finding.clause_title ? " · " + finding.clause_title : "")
            : "System Procurement Rule",
        }),
        policyReference.policy_name ? el("span", { className: "meta", text: " · " + policyReference.policy_name }) : null,
      ].filter(Boolean)),
    ];
    if (finding.clause_quote) {
      var clauseText = String(finding.clause_quote).replace(/\s+/g, " ").trim();
      if (clauseText.length > 420) clauseText = clauseText.slice(0, 417).replace(/\s+\S*$/, "") + "…";
      clauseElements.push(el("blockquote", { text: clauseText }));
    }
    var policySection = el("section", { className: "compliance-finding-section policy" }, clauseElements);

    var children = [
      head,
      el("div", { className: "compliance-finding-grid" }, [tenderSection, policySection]),
    ];

    var bullets = actionBullets(finding);
    if (bullets.length) {
      children.push(
        el("section", { className: "compliance-finding-section action" }, [
          el("h4", { text: "Required action" }),
          renderBulletList(bullets),
        ])
      );
    }

    var resolutionBox;
    if (settled) {
      var stamp = [resolution.resolved_by, resolution.resolved_at ? new Date(resolution.resolved_at).toLocaleString() : ""].filter(Boolean).join(" · ");
      var approval = resolution.approval || {};
      resolutionBox = el("div", { className: "compliance-resolution settled" }, [
        pill(resolution.status === "resolved" ? "Resolved" : "Waived", resolution.status === "resolved" ? "pass" : "review"),
        el("div", { className: "compliance-resolution-detail" }, [
          resolution.note ? el("p", { text: resolution.note }) : el("p", { className: "meta", text: "Authorized without extra note." }),
          approval.authority_title ? el("span", {
            className: "meta",
            text: "Approved by " + approval.authority_title + (approval.reference ? " · " + approval.reference : ""),
          }) : null,
          stamp ? el("span", { className: "meta", text: "By " + stamp }) : null,
        ].filter(Boolean)),
        el("button", {
          className: "secondary",
          type: "button",
          text: "Reopen issue",
          onclick: function () { resolveComplianceFinding(finding.finding_id, "reopen", ""); },
        }),
      ]);
    } else {
      var noteId = "compliance-note-" + String(finding.finding_id || "finding").replace(/[^a-zA-Z0-9_-]/g, "-");
      var note = el("textarea", { id: noteId, rows: "2", placeholder: "State justification note..." });
      var waiverApprovals = Array.isArray(finding.waiver_approvals) ? finding.waiver_approvals : [];
      var decisionAction = "resolve";
      var decisionPanel = el("div", { className: "compliance-decision-panel", hidden: "hidden" });
      var authorityId = noteId + "-authority";
      var authority = el("select", { id: authorityId });
      var referenceId = noteId + "-reference";
      var reference = el("input", { id: referenceId, type: "text", placeholder: "Approval note / file reference" });
      authority.appendChild(el("option", { value: "", text: "Select approving authority" }));
      waiverApprovals.forEach(function (approval) {
        authority.appendChild(el("option", {
          value: approval.authority_title,
          text: approval.authority_title + (approval.action ? " · " + approval.action : ""),
        }));
      });
      var authorityLabel = el("label", { "for": authorityId, text: "Approving authority" });
      var referenceLabel = el("label", { "for": referenceId, text: "Approval reference" });
      authorityLabel.hidden = authority.hidden = referenceLabel.hidden = reference.hidden = true;

      var saveDecision = el("button", { type: "button", text: "Save decision" });
      var cancelDecision = el("button", {
        className: "secondary",
        type: "button",
        text: "Cancel",
        onclick: function () { decisionPanel.hidden = true; },
      });

      saveDecision.addEventListener("click", function () {
        if (!note.value.trim()) {
          renderMessage(byId("compliance-errors"), "Enter the justification note before saving.", "error");
          note.focus();
          return;
        }
        if (decisionAction === "waive" && (!authority.value || !reference.value.trim())) {
          renderMessage(byId("compliance-errors"), "Select the approving authority and enter the approval reference.", "error");
          (authority.value ? reference : authority).focus();
          return;
        }
        resolveComplianceFinding(
          finding.finding_id,
          decisionAction,
          note.value.trim(),
          authority.value,
          reference.value.trim()
        );
      });

      decisionPanel.appendChild(authorityLabel);
      decisionPanel.appendChild(authority);
      decisionPanel.appendChild(referenceLabel);
      decisionPanel.appendChild(reference);
      decisionPanel.appendChild(el("label", { "for": noteId, text: "Decision note" }));
      decisionPanel.appendChild(note);
      decisionPanel.appendChild(el("div", { className: "row gap-sm" }, [saveDecision, cancelDecision]));

      var correctPackage = el("button", {
        className: "secondary",
        type: "button",
        text: "Upload corrected tender",
        onclick: function () {
          var upload = document.querySelector("details.compliance-reupload");
          if (upload) {
            upload.open = true;
            upload.scrollIntoView({ behavior: "smooth", block: "center" });
            byId("compliance-reupload-file").focus();
          }
        },
      });

      var exception = el("button", {
        className: "secondary",
        type: "button",
        text: "Record authorized exception",
        onclick: function () {
          decisionAction = "waive";
          authorityLabel.hidden = authority.hidden = referenceLabel.hidden = reference.hidden = false;
          note.placeholder = "Explain why the policy-authorized exception is being used.";
          decisionPanel.hidden = false;
          authority.focus();
        },
      });

      var resolve = el("button", {
        className: "secondary",
        type: "button",
        text: "Record resolution",
        onclick: function () {
          decisionAction = "resolve";
          authorityLabel.hidden = authority.hidden = referenceLabel.hidden = reference.hidden = true;
          authority.value = "";
          reference.value = "";
          note.placeholder = "State what was checked or corrected and why the finding is resolved.";
          decisionPanel.hidden = false;
          note.focus();
        },
      });

      resolutionBox = el("div", { className: "compliance-resolution" }, [
        el("div", { className: "compliance-resolution-actions" }, [
          resolve,
          waiverApprovals.length ? exception : null,
          correctPackage,
        ].filter(Boolean)),
        waiverApprovals.length ? null : el("p", {
          className: "meta",
          text: "The cited policy does not provide an authorized waiver path for this finding.",
        }),
        decisionPanel,
      ].filter(Boolean));
    }

    children.push(resolutionBox);
    var card = el("details", {
      className: "compliance-finding" + (settled ? " settled" : "") + (isViolation ? " violation" : " review"),
    }, children);
    if (!settled || expanded) card.open = true;
    return card;
  }

  function renderComplianceFindings(data) {
    var box = byId("compliance-findings");
    box.replaceChildren();
    var findings = complianceFindings(data);
    var visible = findings.filter(function (finding) {
      return findingMatchesComplianceFilter(finding, ctx.complianceFilter);
    });
    var findingsHead = document.querySelector(".compliance-findings-head");
    var clear = data.status === "completed" && !findings.length;
    if (findingsHead) findingsHead.hidden = !findings.length;
    byId("compliance-filter-summary").textContent = clear
      ? ""
      : "Showing " + visible.length + " of " + findings.length + " reported finding(s).";
    if (!visible.length) {
      if (findings.length) {
        box.appendChild(el("p", { className: "compliance-empty", text: "No findings match this filter." }));
      } else if (data.status !== "completed") {
        box.appendChild(el("p", { className: "compliance-empty", text: "Run the tender audit to check this tender against policy." }));
      } else {
        box.appendChild(el("p", {
          className: "compliance-empty compliance-clear",
          text: "No issues. You can proceed to the evaluation checklist.",
        }));
      }
      return;
    }
    visible.forEach(function (finding, index) {
      box.appendChild(renderComplianceFinding(finding, index === 0));
    });
  }

  function renderCompliance(data) {
    var info = complianceInfo();
    var enabled = Boolean(info && info.enabled);
    byId("compliance-disabled-note").hidden = enabled;
    byId("compliance-body").hidden = !enabled;
    byId("run-compliance-btn").hidden = !enabled;
    byId("reload-compliance-btn").hidden = !enabled;
    if (!enabled) return;
    var running = complianceRunning(data, info);
    var requiresPolicy = data && data.status === "policy_selection_required";
    byId("run-compliance-btn").disabled = running || requiresPolicy;
    byId("run-compliance-btn").textContent = running
      ? "Tender audit running…"
      : requiresPolicy ? "Select policy first"
        : data && data.status !== "not_started" ? "Re-run tender audit" : "Run tender audit";
    if (!data) {
      setStatusText("compliance-status", "");
      return;
    }
    renderMessage(byId("compliance-errors"), "");
    renderCompliancePolicy(data);
    renderComplianceSummary(data);
    renderComplianceCoverage(data);
    var findingCount = complianceFindings(data).length;
    var clear = data.status === "completed" && !findingCount;
    var intro = document.querySelector(".compliance-review-head p");
    if (intro) {
      intro.textContent = clear
        ? "The tender was checked against the procurement policy."
        : "Review each issue below before approving the checklist.";
    }
    byId("compliance-filters").hidden = !findingCount;
    if (findingCount) renderComplianceFilters(data);
    else byId("compliance-filters").replaceChildren();
    renderComplianceFindings(data);
    if (running) {
      showOperationActivity("compliance-status", "compliance", {
        title: "Reviewing tender compliance",
        payload: data,
        next: "You can leave this page. Findings will appear here automatically.",
      });
    } else {
      setStatusText("compliance-status", "");
    }
  }

  return {
    renderCompliancePolicy: renderCompliancePolicy,
    renderComplianceSummary: renderComplianceSummary,
    renderComplianceCoverage: renderComplianceCoverage,
    renderComplianceFilters: renderComplianceFilters,
    renderBulletList: renderBulletList,
    renderNextAsk: renderNextAsk,
    renderComplianceFinding: renderComplianceFinding,
    renderComplianceFindings: renderComplianceFindings,
    renderCompliance: renderCompliance,
  };
}
