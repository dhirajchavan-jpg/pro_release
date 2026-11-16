"use strict";
/* Bidder classification compatibility facade. */

import {
  broadRouteForCategory,
  businessCategoryForType,
  businessDocumentName,
  businessReviewState,
  classificationChanged,
  classificationRow,
  comparableClassification,
  comparablePhysicalRoutes,
  containsLabel,
  isReferenceDocument,
  manualDocumentLabel,
  normalizedBroadRoute,
  projectedBroadRoute,
  validateClassification,
} from "./bid-classification-model.js";
import { installBidClassificationActions } from "./bid-classification-actions.js";
import { installBidClassificationView } from "./bid-classification-view.js";

export function installBidClassification(ctx) {
  var byId = function () { return ctx.byId.apply(ctx, arguments); };
  var view = installBidClassificationView(ctx);
  var actions = installBidClassificationActions(ctx);
  Object.assign(ctx, {
    manualReviewVendor: actions.manualReviewVendor,
    prepareManualReview: actions.prepareManualReview,
    openManualDocument: view.openManualDocument,
    manualDocumentLabel: manualDocumentLabel,
    classificationRow: classificationRow,
    comparableClassification: comparableClassification,
    classificationChanged: function () { return classificationChanged(ctx); },
    comparablePhysicalRoutes: comparablePhysicalRoutes,
    businessCategoryForType: businessCategoryForType,
    normalizedBroadRoute: normalizedBroadRoute,
    broadRouteForCategory: broadRouteForCategory,
    projectedBroadRoute: projectedBroadRoute,
    isReferenceDocument: isReferenceDocument,
    containsLabel: containsLabel,
    businessDocumentName: businessDocumentName,
    businessReviewState: businessReviewState,
    validateClassification: function () { return validateClassification(ctx, byId); },
    updateClassificationState: view.updateClassificationState,
    renderClassificationWorkspace: view.renderClassificationWorkspace,
    approveClassification: actions.approveClassification,
    saveClassification: actions.saveClassification,
    improveClassification: actions.improveClassification,
    loadManualBidReview: actions.loadManualBidReview,
    runEvaluate: actions.runEvaluate,
    handleEvaluationAction: actions.handleEvaluationAction,
  });
}
