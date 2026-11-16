import assert from "node:assert/strict";
import { evaluationFailureIsCurrent } from "./bid-classification-actions.js";

const ready = ({ status }) => status === "succeeded" || status === "partial";

assert.equal(evaluationFailureIsCurrent("ogger", [
  { vendor_id: "ogger", evaluation_status: "succeeded" },
], ready), false);
assert.equal(evaluationFailureIsCurrent("ogger", [
  { vendor_id: "ogger", evaluation_status: "failed" },
], ready), true);
