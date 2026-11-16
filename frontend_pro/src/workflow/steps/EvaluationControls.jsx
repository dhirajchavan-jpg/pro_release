import { useMemo, useState } from "react";

import { runEvaluation } from "../../api/evaluation.js";
import { getJob, getProject } from "../../api/projects.js";

const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

export default function EvaluationControls({ project, vendor, onProjectChange, onNavigate, onMessage }) {
  const [busy, setBusy] = useState("");
  const bidders = project.bidders || [];
  const approved = useMemo(() => bidders.filter((bidder) => bidder.classification_status === "approved"), [bidders]);
  const selected = bidders.find((bidder) => bidder.vendor_id === vendor);
  const execute = async (vendorId = null) => {
    setBusy(vendorId || "all");
    try {
      const queued = await runEvaluation(project.project_id, vendorId);
      const operations = queued.operations || [];
      if (!operations.length) { onMessage("The approved bidder evaluation is already queued or complete.", "success"); return; }
      await Promise.all(operations.map(async (operation) => { for (;;) { const job = await getJob(operation.operation_id); if (["complete", "succeeded"].includes(job.status)) return; if (["failed", "cancelled"].includes(job.status)) throw new Error(job.error || `Evaluation failed for ${operation.vendor_id}.`); await wait(2000); } }));
      onProjectChange(await getProject(project.project_id));
      onMessage("Bidder evaluation completed. Review the qualification results.", "success");
      onNavigate("results");
    } catch (error) { onMessage(error.message || "The evaluation could not be completed.", "error"); }
    finally { setBusy(""); }
  };
  return <div className="evaluation-run-panel"><div><h3>Run the approved evaluation</h3><p>Only approved bidder document classifications are evaluated against the frozen checklist.</p></div><div className="flex flex-wrap gap-2"><button type="button" disabled={!selected || selected.classification_status !== "approved" || Boolean(busy)} onClick={() => execute(vendor)}>{busy === vendor ? "Evaluation running…" : "Run evaluation"}</button><button className="secondary" type="button" disabled={!approved.length || Boolean(busy)} onClick={() => execute()}>{busy === "all" ? "Evaluations running…" : "Run all approved"}</button><button className="secondary" type="button" onClick={() => onNavigate("results")}>View results</button></div></div>;
}
