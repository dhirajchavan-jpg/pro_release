import assert from "node:assert/strict";
import test from "node:test";
import { projectPage, projectStageLabel } from "./project.js";

test("maps stages to resume screens", () => {
  assert.equal(projectPage({ stage: "tender_uploaded" }), "tender");
  assert.equal(projectPage({ stage: "rfp_understood", compliance: { enabled: true } }), "compliance");
  assert.equal(projectPage({ stage: "rfp_understood", compliance: { enabled: false } }), "review");
  assert.equal(projectPage({ stage: "report_generated" }), "report");
});
test("formats stage labels", () => {
  assert.equal(projectStageLabel("contract_frozen"), "Tender reviewed");
  assert.equal(projectStageLabel("custom_stage"), "custom stage");
});
