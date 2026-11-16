import { useEffect, useState } from "react";

import { correctLedger, getLedger, getProjectDocuments } from "../../api/projects.js";

const textValue = (value) => value == null ? "—" : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
const lines = (value) => value.split("\n").map((item) => item.trim()).filter(Boolean);

function Evidence({ refs }) {
  if (!refs?.length) return <span className="text-xs text-[var(--color-muted)]">No citation available</span>;
  return <details className="evidence-group"><summary>{refs.length} {refs.length === 1 ? "citation" : "citations"}</summary>{refs.map((ref, index) => <div key={`${ref.document_id}-${ref.page}-${index}`} className="evidence-detail"><strong>Page {ref.page || "—"} · original source document</strong>{ref.quote && <blockquote>“{ref.quote}”</blockquote>}{ref.page_image_url && <img className="evidence-preview" src={ref.page_image_url} crossOrigin="use-credentials" alt={`Tender evidence page ${ref.page}`} />}</div>)}</details>;
}

function CorrectionEditor({ entry, projectId, reviewer, onSaved, onMessage }) {
  const [open, setOpen] = useState(false); const [status, setStatus] = useState(entry.status || "unresolved");
  const [value, setValue] = useState(textValue(entry.value) === "—" ? "" : textValue(entry.value));
  const [conditions, setConditions] = useState((entry.conditions || []).join("\n")); const [exceptions, setExceptions] = useState((entry.exceptions || []).join("\n")); const [documents, setDocuments] = useState((entry.required_documents || []).join("\n")); const [reason, setReason] = useState(""); const [busy, setBusy] = useState(false); const [sourceDocuments, setSourceDocuments] = useState(null);
  const save = async () => {
    if (reason.trim().length < 10) return onMessage("Explain the correction in at least 10 characters.", "error");
    let parsedValue = null;
    if (status === "found") { try { parsedValue = value.trim().startsWith("{") || value.trim().startsWith("[") ? JSON.parse(value) : value.trim(); } catch { return onMessage("The corrected value is not valid JSON.", "error"); } }
    setBusy(true);
    try { const result = await correctLedger(projectId, { field_key: entry.key, status, value: parsedValue, conditions: lines(conditions), exceptions: lines(exceptions), required_documents: lines(documents), reason: reason.trim(), reviewed_by: reviewer, reviewed_at: new Date().toISOString() }); onSaved(result); onMessage("Tender finding corrected.", "success"); }
    catch (error) { onMessage(error.message || "The correction could not be saved.", "error"); }
    finally { setBusy(false); }
  };
  const toggle = async () => { const next = !open; setOpen(next); if (next && sourceDocuments == null) { try { setSourceDocuments((await getProjectDocuments(projectId)).documents || []); } catch { setSourceDocuments([]); } } };
  return <><button className="secondary" type="button" onClick={toggle}>{open ? "Close review" : entry.review_source === "human_corrected" ? "Edit again" : "Edit finding"}</button>{open && <div className="ledger-workspace"><aside className="document-library"><h3>Uploaded documents</h3>{sourceDocuments == null ? <p>Loading uploaded documents…</p> : sourceDocuments.length ? sourceDocuments.map((document) => <a key={document.document_id} className="document-link" href={document.url} target="tender-source" rel="noreferrer"><strong>{document.document_kind}</strong><span>{document.file_name}</span></a>) : <p>No viewable document was found.</p>}</aside><div className="document-viewer"><p>Choose a document. Click inside the PDF, then use Ctrl+F to find text in it.</p><iframe name="tender-source" title="Original tender document" /></div><div className="ledger-correction"><label className="field-label">Correct status<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="found">Found</option><option value="not_found">Not found</option><option value="not_applicable">Not applicable</option><option value="unresolved">Unresolved</option></select></label><label className="field-label">Correct value<textarea rows="3" disabled={status !== "found"} value={value} onChange={(event) => setValue(event.target.value)} /></label><details><summary>Conditions, exceptions, and required documents</summary><label className="field-label">Conditions<textarea rows="2" value={conditions} onChange={(event) => setConditions(event.target.value)} /></label><label className="field-label">Exceptions<textarea rows="2" value={exceptions} onChange={(event) => setExceptions(event.target.value)} /></label><label className="field-label">Required bidder documents<textarea rows="2" value={documents} onChange={(event) => setDocuments(event.target.value)} /></label></details><label className="field-label">Reason for correction (required)<textarea rows="3" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="What was wrong, the correct interpretation, and why" /></label><button type="button" disabled={busy} onClick={save}>{busy ? "Saving correction…" : "Save human correction"}</button></div></div>}</>;
}

export default function ReviewStep({ account, project, onProjectChange, onNavigate, onMessage }) {
  const [ledger, setLedger] = useState(null); const [loading, setLoading] = useState(true);
  const load = () => { setLoading(true); getLedger(project.project_id).then(setLedger).catch((error) => onMessage(error.message, "error")).finally(() => setLoading(false)); };
  useEffect(load, [project.project_id]);
  const saved = (result) => { setLedger(result.ledger); if (result.state) onProjectChange(result.state); };
  return <section className="workflow-panel"><div className="step-head tender-review-head"><h2><span className="step-num">Step 2</span> · Review tender understanding</h2><div className="flex gap-2"><button className="secondary" type="button" onClick={load}>Reload findings</button><button type="button" onClick={() => onNavigate(project.compliance?.enabled ? "compliance" : "checklist")}>Proceed to {project.compliance?.enabled ? "compliance audit" : "evaluation checklist"}</button></div></div><p>Review the extracted requirements. Items needing attention appear first; edit only where a correction is required or mark an item not applicable.</p>
    {loading ? <p className="py-8">Loading tender findings…</p> : <div className="table-wrap"><table className="ledger-table"><thead><tr><th>Requirement</th><th>Status</th><th>Extracted value</th><th>Conditions / evidence</th><th>Review</th></tr></thead><tbody>{(ledger?.entries || []).map((entry) => <tr key={entry.key}><td><strong>{entry.label || entry.key.replaceAll("_", " ")}</strong><small>{entry.key}</small></td><td><span className={`status-pill ${entry.status === "found" ? "status-pass" : "status-review"}`}>{String(entry.status || "unresolved").replaceAll("_", " ")}</span></td><td><pre>{textValue(entry.value)}</pre></td><td>{entry.conditions?.length ? <ul>{entry.conditions.map((item, index) => <li key={index}>{textValue(item)}</li>)}</ul> : null}<Evidence refs={entry.evidence || []} /></td><td><CorrectionEditor entry={entry} projectId={project.project_id} reviewer={account.user.name || "Local procurement operator"} onSaved={saved} onMessage={onMessage} /></td></tr>)}</tbody></table></div>}
  </section>;
}
