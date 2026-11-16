import { useEffect, useMemo, useState } from "react";

import { approveClassification, correctClassification, getClassification, getClassificationReview, proposeClassification } from "../../api/bidders.js";
import { getJob, getProject } from "../../api/projects.js";
import EvaluationControls from "./EvaluationControls.jsx";

const categories = [
  ["company_statutory_eligibility", "Bidder Eligibility & Statutory Status"],
  ["emd_mse_exemptions", "EMD, MSE & Exemptions"],
  ["pqr_source_approval", "PQR & Source Approval"],
  ["technical_quality_scrutiny", "Technical & QAX Scrutiny"],
  ["financial_qualification", "Financial Qualification"],
  ["declarations_tender_acceptance", "Declarations & Tender Acceptance"],
  ["techno_commercial_unpriced", "Techno-Commercial Terms (Unpriced)"],
  ["commercial_price_restricted", "Price Bid (Restricted)"],
  ["reference_document", "Reference documents"],
  ["needs_review", "Please check — decide later"],
];

const clone = (value) => JSON.parse(JSON.stringify(value));
const recordFor = (project, vendor) => (project.bidders || []).find((item) => item.vendor_id === vendor) || {};

export default function ClassificationStep({ account, project, onProjectChange, onNavigate, onMessage }) {
  const preparedVendors = (project.vendors || []).filter((vendor) => recordFor(project, vendor).documents_prepared);
  const [vendor, setVendor] = useState(preparedVendors[0] || ""); const [payload, setPayload] = useState(null); const [review, setReview] = useState(null); const [rows, setRows] = useState([]); const [savedRows, setSavedRows] = useState([]); const [reason, setReason] = useState(""); const [busy, setBusy] = useState(""); const [activeDocument, setActiveDocument] = useState(null);
  const dirty = useMemo(() => JSON.stringify(rows) !== JSON.stringify(savedRows), [rows, savedRows]);
  const load = async (target = vendor) => {
    if (!target) return;
    setBusy("load");
    try { const [classification, reviewData] = await Promise.all([getClassification(project.project_id, target), getClassificationReview(project.project_id, target)]); const logical = clone(classification.classification?.logical_documents || []); setPayload(classification); setReview(reviewData); setRows(logical); setSavedRows(clone(logical)); setActiveDocument(reviewData.documents?.[0] || null); setReason(""); }
    catch (error) { onMessage(error.message || "The bidder documents could not be loaded.", "error"); }
    finally { setBusy(""); }
  };
  useEffect(() => { if (vendor) load(vendor); }, [vendor, project.project_id]);
  const updateRow = (id, changes) => setRows((current) => current.map((item) => item.logical_document_id === id ? { ...item, ...changes } : item));
  const save = async () => {
    if (!dirty) return onMessage("There are no unsaved changes.", "success");
    if (reason.trim().length < 3) return onMessage("Add a short correction note before saving.", "error");
    const unresolved = rows.find((item) => item.broad_route === "needs_review" && !String(item.route_reason || "").trim());
    if (unresolved) return onMessage("Describe every document section that still needs classification.", "error");
    setBusy("save");
    try { const saved = await correctClassification(project.project_id, vendor, { logical_documents: rows, reason: reason.trim(), reviewed_by: account.user.name || "Local procurement operator", reviewed_at: new Date().toISOString() }); const logical = clone(saved.classification?.logical_documents || []); setPayload(saved); setRows(logical); setSavedRows(clone(logical)); setReason(""); onMessage("Classification changes saved.", "success"); }
    catch (error) { onMessage(error.message || "The changes could not be saved.", "error"); }
    finally { setBusy(""); }
  };
  const approve = async () => {
    if (dirty) return onMessage("Save your changes before approval.", "error");
    if (rows.some((item) => item.needs_review || item.broad_route === "needs_review")) return onMessage("Resolve every bidder document classification before approval.", "error");
    setBusy("approve");
    try { const result = await approveClassification(project.project_id, vendor, { reviewed_by: account.user.name || "Local procurement operator", reviewed_at: new Date().toISOString(), reason: "Confirmed the saved document classification against the submitted PDFs." }); if (result.state) onProjectChange(result.state); setPayload((current) => ({ ...current, status: "approved" })); onMessage("Classification approved.", "success"); }
    catch (error) { onMessage(error.message || "The classification could not be approved.", "error"); }
    finally { setBusy(""); }
  };
  const reanalyze = async () => {
    if (dirty && !window.confirm("Discard the unsaved classification changes and re-analyze these documents?")) return;
    setBusy("propose");
    try { const operation = await proposeClassification(project.project_id, vendor); for (;;) { const job = await getJob(operation.job_id); if (["complete", "succeeded"].includes(job.status)) break; if (["failed", "cancelled"].includes(job.status)) throw new Error(job.error || "Document classification failed."); await new Promise((resolve) => window.setTimeout(resolve, 2000)); } const updated = await getProject(project.project_id); onProjectChange(updated); await load(vendor); onMessage("The bidder package was reclassified. Review the proposed sections before approval.", "success"); }
    catch (error) { onMessage(error.message || "The document classification could not be refreshed.", "error"); }
    finally { setBusy(""); }
  };
  if (!preparedVendors.length) return <section className="workflow-panel"><div className="step-head"><h2><span className="step-num">Step 6</span> · Review bidder documents</h2></div><p>Run bidder intake before reviewing documents.</p><button className="secondary mt-4" type="button" onClick={() => onNavigate("bids")}>Back to bidders</button></section>;
  const approved = payload?.status === "approved";
  const tenderMismatch = payload?.tender_match?.status === "mismatch" || review?.tender_match?.status === "mismatch";
  return <section className="workflow-panel"><div className="step-head"><h2><span className="step-num">Step 6</span> · Review bidder documents</h2></div><p>Confirm what each submitted file contains. Split combined PDFs into page ranges and correct a document category when needed.</p><div className="manual-review-toolbar"><button className="secondary" type="button" onClick={() => onNavigate("bids")}>Back to bidders</button><label className="field-label">Bidder<select value={vendor} onChange={(event) => setVendor(event.target.value)}>{preparedVendors.map((id) => <option key={id} value={id}>{recordFor(project, id).bidder_name || id}</option>)}</select></label>{!approved && <button className="secondary" type="button" disabled={busy === "propose"} onClick={reanalyze}>{busy === "propose" ? "Re-analyzing…" : "Re-analyze documents"}</button>}</div><h3 className="manual-bidder-name">{review?.bidder_name || recordFor(project, vendor).bidder_name || vendor}</h3>{tenderMismatch && <div className="tender-mismatch"><strong>This bidder package appears to belong to a different tender.</strong><button type="button" onClick={() => onNavigate("bids")}>Return to bidder uploads</button></div>}<div className="manual-readiness"><h3>First, confirm what each document contains</h3><p>The bidder files are grouped into procurement categories. Correct a category only when needed; page ranges appear when one PDF contains multiple documents.</p></div>
    <div className="classification-save-bar"><div><span>{busy === "load" ? "Loading bidder documents…" : dirty ? "Unsaved changes" : approved ? "Classification approved" : "No unsaved changes"}</span>{dirty && <label className="field-label mt-2">Reason for change (required)<input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Example: Confirmed pages 1–19 are technical specifications" /></label>}</div><div className="flex flex-wrap gap-2"><button type="button" disabled={!dirty || busy === "save" || approved} onClick={save}>{busy === "save" ? "Saving…" : "Save changes"}</button><button className="secondary" type="button" disabled={dirty || busy === "approve" || approved || tenderMismatch} onClick={approve}>{approved ? "Classification approved" : busy === "approve" ? "Approving…" : "Approve classification"}</button></div></div>
    <div className="manual-review-layout"><div className="manual-document-list">{rows.map((row) => { const document = review?.documents?.find((item) => item.document_id === row.source_document_id); return <article key={row.logical_document_id} className="classification-file"><div className="classification-file-head"><div><strong>{row.title || "Bid document"}</strong><small>{document?.file_name || row.source_document_id} · pages {row.start_page}–{row.end_page}</small></div><span className={`status-pill ${row.broad_route === "needs_review" ? "status-review" : "status-pass"}`}>{categories.find(([value]) => value === row.broad_route)?.[1] || row.broad_route}</span></div><div className="classification-sections"><label className="field-label">Business document name<input disabled={approved} value={row.title || ""} onChange={(event) => updateRow(row.logical_document_id, { title: event.target.value })} /></label><div className="grid grid-cols-2 gap-3"><label className="field-label">From page<input type="number" min="1" disabled={approved} value={row.start_page || 1} onChange={(event) => updateRow(row.logical_document_id, { start_page: Number(event.target.value) })} /></label><label className="field-label">To page<input type="number" min="1" disabled={approved} value={row.end_page || 1} onChange={(event) => updateRow(row.logical_document_id, { end_page: Number(event.target.value) })} /></label></div><label className="field-label">Document classification<select disabled={approved} value={row.broad_route || "needs_review"} onChange={(event) => updateRow(row.logical_document_id, { broad_route: event.target.value, needs_review: event.target.value === "needs_review", route_reason: event.target.value === "needs_review" ? row.route_reason || "Classification requires reviewer confirmation." : "" })}>{categories.map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select></label>{row.broad_route === "needs_review" && <label className="field-label">What remains unclear?<input disabled={approved} value={row.route_reason || ""} onChange={(event) => updateRow(row.logical_document_id, { route_reason: event.target.value })} /></label>}<button className="secondary" type="button" disabled={!document?.url} onClick={() => setActiveDocument(document)}>Open source</button></div></article>; })}</div><div className="manual-pdf-viewer"><p>{activeDocument ? `${activeDocument.file_name} · click the PDF, then use Ctrl+F` : "Choose a PDF to inspect it here. Click inside it, then use Ctrl+F."}</p>{activeDocument?.url && <iframe className="document-frame" src={activeDocument.url} title="Bidder document" />}</div></div>
    <EvaluationControls project={project} vendor={vendor} onProjectChange={onProjectChange} onNavigate={onNavigate} onMessage={onMessage} />
  </section>;
}
