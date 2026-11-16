import { useEffect, useMemo, useState } from "react";

import { getCompliance, resolveComplianceFinding, runCompliance, selectCompliancePolicy } from "../../api/compliance.js";
import { getJob, getProject } from "../../api/projects.js";

const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));
const statusClass = (status) => status === "compliant" || status === "pass" ? "status-pass" : status === "violation" ? "status-fail" : "status-review";

function Finding({ finding, projectId, onReload, onMessage }) {
  const resolution = finding.resolution || { status: "open" };
  const [action, setAction] = useState(resolution.status === "open" ? "resolve" : "reopen");
  const [note, setNote] = useState("");
  const [authority, setAuthority] = useState("");
  const [reference, setReference] = useState("");
  const [busy, setBusy] = useState(false);
  const approvals = finding.waiver_approvals || [];
  const submit = async () => {
    if (action === "waive" && (!note.trim() || !authority || !reference.trim())) return onMessage("A justification, approval authority, and approval reference are required for a waiver.", "error");
    setBusy(true);
    try { await resolveComplianceFinding(projectId, finding.finding_id, { action, note: note.trim(), approval_authority: authority, approval_reference: reference.trim() }); await onReload(); onMessage("Compliance finding updated.", "success"); }
    catch (error) { onMessage(error.message || "The finding could not be updated.", "error"); }
    finally { setBusy(false); }
  };
  return <article className="compliance-finding">
    <div className="compliance-finding-head"><div><span className="eyebrow">{finding.clause_id || "Policy check"}</span><h4>{finding.title || finding.clause_title || "Tender compliance finding"}</h4></div><span className={`status-pill ${statusClass(finding.status)}`}>{finding.status || "needs review"}</span></div>
    <p>{finding.detail || finding.required_action}</p>
    {finding.clause_quote && <blockquote>{finding.clause_quote}</blockquote>}
    {finding.required_action && <p><strong>Required action:</strong> {finding.required_action}</p>}
    {finding.consequence && <p><strong>Why this matters:</strong> {finding.consequence}</p>}
    <div className="finding-resolution"><span className={`status-pill ${resolution.status === "resolved" || resolution.status === "waived" ? "status-pass" : "status-review"}`}>{resolution.status}</span><label className="field-label">Action<select value={action} onChange={(event) => setAction(event.target.value)}>{resolution.status === "open" ? <><option value="resolve">Mark resolved</option>{approvals.length > 0 && <option value="waive">Record approved waiver</option>}</> : <option value="reopen">Reopen finding</option>}</select></label><label className="field-label">Review note<textarea rows="2" value={note} onChange={(event) => setNote(event.target.value)} /></label>{action === "waive" && <><label className="field-label">Approval authority<select value={authority} onChange={(event) => setAuthority(event.target.value)}><option value="">Select authority</option>{approvals.map((item) => <option key={item.authority_title} value={item.authority_title}>{item.authority_title}</option>)}</select></label><label className="field-label">Approval reference<input value={reference} onChange={(event) => setReference(event.target.value)} /></label></>}<button type="button" disabled={busy} onClick={submit}>{busy ? "Saving…" : "Save finding review"}</button></div>
  </article>;
}

export default function ComplianceStep({ project, onProjectChange, onNavigate, onMessage }) {
  const [data, setData] = useState(null); const [filter, setFilter] = useState("all"); const [busy, setBusy] = useState("");
  const load = async () => { try { setData(await getCompliance(project.project_id)); } catch (error) { onMessage(error.message || "Tender compliance could not be loaded.", "error"); } };
  useEffect(() => { load(); }, [project.project_id]);
  const run = async () => { setBusy("run"); try { const operation = await runCompliance(project.project_id); for (;;) { const job = await getJob(operation.job_id); if (["complete", "succeeded"].includes(job.status)) break; if (["failed", "cancelled"].includes(job.status)) throw new Error(job.error || "Tender compliance review failed."); await wait(2000); } await load(); onProjectChange(await getProject(project.project_id)); onMessage("Tender compliance review completed.", "success"); } catch (error) { onMessage(error.message || "Tender compliance review could not run.", "error"); } finally { setBusy(""); } };
  const changePolicy = async (event) => { const policyId = event.target.value; if (!policyId || policyId === data?.policy_override) return; setBusy("policy"); try { await selectCompliancePolicy(project.project_id, policyId); await load(); onMessage("Governing procurement policy updated. Run the tender audit again.", "success"); } catch (error) { onMessage(error.message || "The policy could not be changed.", "error"); } finally { setBusy(""); } };
  const findings = useMemo(() => (data?.findings || []).filter((finding) => filter === "all" || (finding.resolution?.status || "open") === filter), [data, filter]);
  const canProceed = data?.status === "completed" && Number(data?.open || 0) === 0 && (data?.coverage?.complete !== false);
  return <section className="workflow-panel"><div className="step-head"><h2><span className="step-num">Step 3</span> · Review tender compliance</h2><div className="flex flex-wrap gap-2"><button className="secondary" type="button" onClick={() => onNavigate("review")}>Back to tender review</button><button className="secondary" type="button" onClick={load}>Reload review</button><button type="button" disabled={busy === "run" || !data?.enabled} onClick={run}>{busy === "run" ? "Review running…" : "Re-run tender audit"}</button><button type="button" disabled={!canProceed} onClick={() => onNavigate("checklist")}>Proceed to evaluation checklist</button></div></div><p>Review the tender against the governing procurement policy before finalizing the evaluation checklist.</p>
    {!data ? <p className="mt-4">Loading tender assurance…</p> : !data.enabled ? <div className="compliance-empty"><strong>Policy compliance review is not enabled for this tender.</strong></div> : <><div className="compliance-overview"><div><span>Tender assurance</span><strong>{data.status.replaceAll("_", " ")}</strong></div><div><span>Is this tender ready to issue?</span><strong>{canProceed ? "Yes" : "Review required"}</strong></div><div><span>Open findings</span><strong>{data.open || 0}</strong></div><div><span>Coverage</span><strong>{data.coverage?.complete === false ? "Incomplete" : data.status === "completed" ? "Complete" : "Pending"}</strong></div></div><div className="policy-context"><label className="field-label">Governing procurement policy<select disabled={busy === "policy"} value={data.policy_override || data.policy_id || ""} onChange={changePolicy}>{(data.available_policies || []).map((policy) => <option key={policy.policy_id} value={policy.policy_id}>{policy.name || policy.policy_name || policy.policy_id}</option>)}</select></label><p>{data.policy_name || "The governing policy will be detected from the tender."}</p></div><div className="compliance-filter"><strong>Findings</strong><select aria-label="Filter compliance findings" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">All findings</option><option value="open">Open</option><option value="resolved">Resolved</option><option value="waived">Waived</option></select></div><div className="compliance-list">{findings.length ? findings.map((finding) => <Finding key={finding.finding_id} finding={finding} projectId={project.project_id} onReload={load} onMessage={onMessage} />) : <p className="vendor-empty">No findings match this filter.</p>}</div></>}
  </section>;
}
