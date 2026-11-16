import { useEffect, useMemo, useState } from "react";

import { getProject } from "../api/projects.js";
import Notice from "../components/Notice.jsx";
import ChecklistStep from "./steps/ChecklistStep.jsx";
import BidsStep from "./steps/BidsStep.jsx";
import ClassificationStep from "./steps/ClassificationStep.jsx";
import ComplianceStep from "./steps/ComplianceStep.jsx";
import ReviewStep from "./steps/ReviewStep.jsx";
import ResultsStep from "./steps/ResultsStep.jsx";
import ReportStep from "./steps/ReportStep.jsx";
import TenderStep from "./steps/TenderStep.jsx";
import { stageIndex, workflowSteps } from "./workflow.js";

export default function WorkflowPage({ account, projectId, page, onBack, onNavigate, onProjectChange }) {
  const accountProject = useMemo(() => account.projects.find((item) => item.project_id === projectId), [account.projects, projectId]);
  const [project, setProject] = useState(accountProject || null);
  const [loading, setLoading] = useState(Boolean(projectId));
  const [notice, setNotice] = useState({ text: "", kind: "info" });

  const replaceProject = (next) => {
    setProject(next);
    onProjectChange(next);
  };

  useEffect(() => {
    if (!projectId) { setProject(null); setLoading(false); return; }
    setLoading(true);
    getProject(projectId).then(replaceProject).catch((error) => setNotice({ text: error.message, kind: "error" })).finally(() => setLoading(false));
  }, [projectId]);

  const currentStage = stageIndex(project?.stage);
  const complianceEnabled = Boolean(project?.compliance?.enabled);
  const navigate = (nextPage, targetProjectId) => onNavigate(targetProjectId || project?.project_id || "new", nextPage);

  if (loading) return <main className="page-width py-12 text-center">Loading tender…</main>;
  return <>
    <header className="workflow-header">
      <div className="tender-context-bar"><div className="page-width flex min-h-16 items-center gap-4"><button className="link-button" type="button" onClick={onBack}>← All tenders</button><span className="h-7.5 w-px bg-slate-300" /><div className="grid min-w-0 gap-0.5"><span className="text-[11px] text-slate-500">Tenders <span className="px-1 text-slate-400">/</span> {project?.tender_number || project?.project_id || "Not yet created"}</span><strong className="truncate text-[15px]">{project?.title || (project ? "Untitled tender" : "New tender")}</strong></div></div></div>
      <nav className="workflow-stepper" aria-label="Workflow progress">{workflowSteps.map((step, index) => {
        const hidden = step.page === "compliance" && project && !complianceEnabled;
        const classificationReady = step.page === "evaluate" && project?.bidders?.some((item) => item.documents_prepared);
        const unlocked = !project ? index === 0 : index === 0 || currentStage >= step.requirement || classificationReady;
        const done = project && (index === 0 ? currentStage >= 1 : currentStage > step.requirement);
        return <button key={step.page} hidden={hidden} className={`workflow-pill ${done ? "done" : unlocked ? "available" : ""} ${page === step.page ? "current" : ""}`} disabled={!unlocked} onClick={() => navigate(step.page)}><span className="step-icon">{index + 1}</span><span>{step.label}</span></button>;
      })}</nav>
      <div className="page-width py-3"><Notice kind={notice.kind} onDismiss={() => setNotice({ text: "", kind: "info" })}>{notice.text}</Notice></div>
    </header>
    <main className="page-width pb-12">
      {page === "tender" && <TenderStep account={account} project={project} onProjectChange={replaceProject} onNavigate={navigate} onMessage={(text, kind = "info") => setNotice({ text, kind })} />}
      {page === "review" && project && <ReviewStep account={account} project={project} onProjectChange={replaceProject} onNavigate={navigate} onMessage={(text, kind = "info") => setNotice({ text, kind })} />}
      {page === "compliance" && project && <ComplianceStep project={project} onProjectChange={replaceProject} onNavigate={navigate} onMessage={(text, kind = "info") => setNotice({ text, kind })} />}
      {page === "checklist" && project && <ChecklistStep account={account} project={project} onProjectChange={replaceProject} onNavigate={navigate} onMessage={(text, kind = "info") => setNotice({ text, kind })} />}
      {page === "bids" && project && <BidsStep project={project} onProjectChange={replaceProject} onNavigate={navigate} onMessage={(text, kind = "info") => setNotice({ text, kind })} />}
      {page === "evaluate" && project && <ClassificationStep account={account} project={project} onProjectChange={replaceProject} onNavigate={navigate} onMessage={(text, kind = "info") => setNotice({ text, kind })} />}
      {page === "results" && project && <ResultsStep account={account} project={project} onNavigate={navigate} onMessage={(text, kind = "info") => setNotice({ text, kind })} />}
      {page === "report" && project && <ReportStep project={project} onProjectChange={replaceProject} onNavigate={navigate} onMessage={(text, kind = "info") => setNotice({ text, kind })} />}
    </main>
  </>;
}
