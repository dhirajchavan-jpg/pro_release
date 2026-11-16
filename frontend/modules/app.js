"use strict";

import { createContext } from "./context.js";
import { installRuntime } from "./runtime.js";
import { installAccount } from "./account.js?v=20260923-assets-2";
import { installNavigation } from "./navigation.js";
import { installTenderSetup } from "./tender-setup.js";
import { installTenderReview } from "./tender-review.js";
import { installCompliance } from "./compliance.js";
import { installChecklist } from "./checklist.js";
import { installBidIntake } from "./bid-intake.js";
import { installBidClassification } from "./bid-classification.js";
import { installResults } from "./results.js";
import { installReport } from "./report.js";
import { installWiring } from "./wiring.js";

export function start() {
  const context = createContext();
  installRuntime(context);
  installAccount(context);
  installNavigation(context);
  installTenderSetup(context);
  installTenderReview(context);
  installCompliance(context);
  installChecklist(context);
  installBidIntake(context);
  installBidClassification(context);
  installResults(context);
  installReport(context);
  installWiring(context);
}
