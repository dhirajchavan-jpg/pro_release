export const stages = ["tender_uploaded", "rfp_understood", "contract_drafted", "contract_frozen", "bids_intaken", "evaluated", "report_generated"];
export function projectStageLabel(stage) {
  return { tender_uploaded: "Tender uploaded", rfp_understood: "Tender ready for review", contract_drafted: "Review in progress", contract_frozen: "Tender reviewed", bids_intaken: "Ready to evaluate", evaluated: "Results ready", report_generated: "Report ready" }[stage] || String(stage || "Not started").replaceAll("_", " ");
}
export function projectPage(project) {
  const pages = ["tender", "review", "checklist", "bids", "evaluate", "results", "report"];
  const index = Math.max(0, Math.min(stages.indexOf(project.stage), pages.length - 1));
  if (project.stage === "rfp_understood" && project.compliance?.enabled) return "compliance";
  return pages[index];
}
