"use strict";
/* Tender compliance compatibility facade. */

import {
  actionBullets,
  cleanTenderQuote,
  cleanedEvidence,
  complianceCounts,
  complianceFindings,
  complianceRunning,
  coverageList,
  evidenceSourceLabel,
  findingMatchesComplianceFilter,
  findingResolutionStatus,
  isGenericLockText,
  isInternalEvidenceId,
  isNoisyTenderQuote,
  problemBullets,
  resultAlreadyOnFile,
  resultPolicyLine,
} from "./compliance-model.js";
import { installComplianceActions } from "./compliance-actions.js";
import { installComplianceView } from "./compliance-view.js";

export function installCompliance(ctx) {
  var view = installComplianceView(ctx);
  var actions = installComplianceActions(ctx);
  var complianceInfo = function () { return ctx.complianceInfo.apply(ctx, arguments); };
  var criterionKind = function () { return ctx.criterionKind.apply(ctx, arguments); };
  Object.assign(ctx, {
    complianceRunning: function (data) { return complianceRunning(data, complianceInfo()); },
    loadCompliance: actions.loadCompliance,
    complianceFindings: complianceFindings,
    findingResolutionStatus: findingResolutionStatus,
    complianceCounts: complianceCounts,
    findingMatchesComplianceFilter: function (finding) {
      return findingMatchesComplianceFilter(finding, ctx.complianceFilter);
    },
    renderCompliancePolicy: view.renderCompliancePolicy,
    renderComplianceSummary: view.renderComplianceSummary,
    coverageList: coverageList,
    renderComplianceCoverage: view.renderComplianceCoverage,
    renderComplianceFilters: view.renderComplianceFilters,
    isInternalEvidenceId: isInternalEvidenceId,
    isNoisyTenderQuote: isNoisyTenderQuote,
    cleanTenderQuote: cleanTenderQuote,
    evidenceSourceLabel: evidenceSourceLabel,
    cleanedEvidence: cleanedEvidence,
    problemBullets: problemBullets,
    actionBullets: actionBullets,
    isGenericLockText: isGenericLockText,
    renderBulletList: view.renderBulletList,
    resultAlreadyOnFile: resultAlreadyOnFile,
    resultPolicyLine: function (check) {
      return resultPolicyLine(check, criterionKind);
    },
    renderNextAsk: view.renderNextAsk,
    renderComplianceFinding: view.renderComplianceFinding,
    renderComplianceFindings: view.renderComplianceFindings,
    renderCompliance: view.renderCompliance,
    runCompliance: actions.runCompliance,
    selectCompliancePolicy: actions.selectCompliancePolicy,
    resolveComplianceFinding: actions.resolveComplianceFinding,
    reuploadTender: actions.reuploadTender,
  });
}
