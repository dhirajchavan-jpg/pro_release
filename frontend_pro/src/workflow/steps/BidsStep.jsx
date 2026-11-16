import { useRef, useState } from "react";

import { addBid, deleteBid, replaceBid, runIntake } from "../../api/bidders.js";
import { getJob, getProject } from "../../api/projects.js";

function bidderRows(project) {
  return Array.isArray(project.bidders) ? project.bidders : [];
}

function BidderRow({ project, vendorId, onProjectChange, onNavigate, onMessage }) {
  const inputRef = useRef(null);
  const record = bidderRows(project).find((item) => item.vendor_id === vendorId) || {};
  const approved = record.classification_status === "approved";
  const prepared = Boolean(record.documents_prepared || record.classification_status && record.classification_status !== "pending");
  const status = approved ? "Classification approved" : prepared ? "Classification needs approval" : "Ready to prepare documents";

  const replace = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const reason = window.prompt(`Reason for replacing ${(record.bidder_name || vendorId)}'s bidder package:`, "Corrected bidder submission");
    if (!reason?.trim()) { event.target.value = ""; return onMessage("A replacement reason is required.", "error"); }
    try { const result = await replaceBid(project.project_id, vendorId, file, reason.trim()); onProjectChange(result.state); onMessage("Bidder package replaced. Process the corrected documents before evaluation.", "success"); }
    catch (error) { onMessage(error.message || "Could not replace the bidder package.", "error"); }
    finally { event.target.value = ""; }
  };
  const remove = async () => {
    if (!window.confirm(`Delete ${record.bidder_name || vendorId} and its uploaded bidder package? This cannot be undone.`)) return;
    try { const result = await deleteBid(project.project_id, vendorId); onProjectChange(result.state); onMessage(`Bidder deleted: ${record.bidder_name || vendorId}.`, "success"); }
    catch (error) { onMessage(error.message || "Could not delete the bidder.", "error"); }
  };

  return <div className="vendor-row"><div><strong>{record.bidder_name || vendorId}</strong><small>Submission ID: {vendorId}</small></div><div><strong>{record.original_filename || "Package uploaded"}</strong><small>{record.package_bytes ? `${(record.package_bytes / 1048576).toFixed(1)} MB · ` : ""}one isolated bidder submission</small></div><span className={`status-pill ${approved ? "status-pass" : "status-review"}`}>{status}</span>{prepared && <button className="secondary" type="button" onClick={() => onNavigate("evaluate")}>{approved ? "View classification" : "Review classification"}</button>}<button className="secondary" type="button" onClick={() => inputRef.current?.click()}>Replace package</button><input ref={inputRef} className="sr-only" type="file" accept=".pdf,.zip" onChange={replace} /><button className="secondary" type="button" disabled={approved || Boolean(record.evaluation_id)} onClick={remove}>Delete bidder</button></div>;
}

export default function BidsStep({ project, onProjectChange, onNavigate, onMessage }) {
  const [name, setName] = useState(""); const [file, setFile] = useState(null); const [busy, setBusy] = useState(""); const [status, setStatus] = useState("");
  const vendors = project.vendors || bidderRows(project).map((item) => item.vendor_id);
  const add = async () => {
    if (!name.trim()) return onMessage("Enter the bidder name.", "error");
    if (!file) return onMessage("Choose one PDF or ZIP file.", "error");
    setBusy("add");
    try { const result = await addBid(project.project_id, name.trim(), file); onProjectChange(result.state); onMessage(`Bidder added: ${name.trim()}.`, "success"); setName(""); setFile(null); }
    catch (error) { onMessage(error.message || "Could not add the bid.", "error"); }
    finally { setBusy(""); }
  };
  const process = async () => {
    if (!vendors.length) return onMessage("Add at least one bidder package first.", "error");
    setBusy("intake"); setStatus("Starting document preparation…");
    try {
      const operation = await runIntake(project.project_id);
      for (;;) {
        const job = await getJob(operation.job_id); setStatus(`Preparing bidder documents: ${job.status}`);
        if (["complete", "succeeded"].includes(job.status)) break;
        if (["failed", "cancelled"].includes(job.status)) throw new Error(job.error || "Bidder document preparation failed.");
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
      }
      const updated = await getProject(project.project_id); onProjectChange(updated); setStatus("Documents prepared. Review and approve the classification."); onNavigate("evaluate");
    } catch (error) { setStatus(""); onMessage(error.message || "Bidder document preparation failed.", "error"); }
    finally { setBusy(""); }
  };
  const pending = bidderRows(project).some((item) => !item.documents_prepared);
  return <section className="workflow-panel"><div className="step-head"><h2><span className="step-num">Step 5</span> · Bidder submissions</h2><button type="button" disabled={!vendors.length || busy === "intake" || !pending} onClick={process}>{busy === "intake" ? "Processing documents…" : "Process documents"}</button></div><p>Add one bidder at a time. Enter the bidder name and upload one PDF or ZIP containing that bidder&apos;s complete submission. Price material is quarantined automatically before technical extraction.</p><div className="row"><label className="field-label">Bidder name<input value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Pacific Engineers" /></label><label className="field-label">Bid submission (.pdf / .zip)<input key={file?.name || "empty"} type="file" accept=".pdf,.zip,application/pdf,application/zip" onChange={(event) => setFile(event.target.files?.[0] || null)} /></label><button type="button" disabled={busy === "add"} onClick={add}>{busy === "add" ? "Adding bidder…" : "Add bidder"}</button></div><div className="vendor-list">{vendors.length ? vendors.map((vendor) => <BidderRow key={vendor} project={project} vendorId={vendor} onProjectChange={onProjectChange} onNavigate={onNavigate} onMessage={onMessage} />) : <div className="vendor-empty">No bidders added yet.</div>}</div>{status && <div className="operation-progress"><span className="operation-spinner" /><div><strong>Preparing bidder documents</strong><p>{status}</p><small>You can continue reviewing this tender. Classification will appear when ready.</small></div></div>}</section>;
}
