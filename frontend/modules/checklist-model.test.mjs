import assert from "node:assert/strict";
import { coerceRuleValue } from "./checklist-model.js";

assert.deepEqual(coerceRuleValue("Signed offer, No-deviation certificate", []), [
  "Signed offer",
  "No-deviation certificate",
]);
assert.deepEqual(coerceRuleValue("1, 2", [0]), [1, 2]);
