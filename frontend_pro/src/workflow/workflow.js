export const workflowSteps = [
  { page: "tender", label: "Tender", requirement: 0 },
  { page: "review", label: "Review", requirement: 1 },
  { page: "compliance", label: "Compliance", requirement: 1 },
  { page: "checklist", label: "Checklist", requirement: 1 },
  { page: "bids", label: "Bids", requirement: 3 },
  { page: "evaluate", label: "Evaluate", requirement: 4 },
  { page: "results", label: "Results", requirement: 5 },
  { page: "report", label: "Report", requirement: 6 },
];

export const stageOrder = ["tender_uploaded", "rfp_understood", "contract_drafted", "contract_frozen", "bids_intaken", "evaluated", "report_generated"];
export const stageIndex = (stage) => stageOrder.indexOf(stage);

export function pageForProject(project) {
  const index = Math.max(0, stageIndex(project?.stage));
  if (index === 0) return "tender";
  if (index === 1) return project?.compliance?.enabled ? "compliance" : "review";
  return ["tender", "review", "checklist", "bids", "evaluate", "results", "report"][Math.min(index, 6)];
}
