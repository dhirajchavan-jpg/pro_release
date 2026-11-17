import { useEffect, useRef, useState } from "react";

import { createProject, getDefaultFieldSchema, getJob, getProject, understandProject } from "../../api/projects.js";

function schemaSummary(schema) {
  if (!schema || schema.schema_version !== "rfp-field-schema.v1" || !Array.isArray(schema.sections) || !schema.sections.length) throw new Error("Use an rfp-field-schema.v1 JSON object with at least one section.");
  const fieldCount = schema.sections.reduce((total, section) => {
    if (!Array.isArray(section?.fields) || !section.fields.length) throw new Error("Every schema section must contain at least one field.");
    return total + section.fields.length;
  }, 0);
  return { schema_id: schema.schema_id || "custom-schema", field_count: fieldCount };
}

export default function TenderStep({ account, project, onProjectChange, onNavigate, onMessage }) {
  const fileRef = useRef(null);
  const [file, setFile] = useState(null);
  const [policyAudit, setPolicyAudit] = useState(true);
  const [policyFile, setPolicyFile] = useState(null);
  const [schemaOpen, setSchemaOpen] = useState(false);
  const [schemaText, setSchemaText] = useState("");
  const [selectedSchema, setSelectedSchema] = useState(null);
  const [defaultSchema, setDefaultSchema] = useState(null);
  const [schemaStatus, setSchemaStatus] = useState("");
  const [activity, setActivity] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => { getDefaultFieldSchema().then((info) => { setDefaultSchema(info); setSchemaStatus("The standard organization schema will be used unless you provide a client JSON."); }).catch(() => {}); }, []);

  const selectSchema = (text) => {
    try {
      const parsed = JSON.parse(text); const info = schemaSummary(parsed);
      setSelectedSchema(JSON.stringify(parsed)); setSchemaText(JSON.stringify(parsed, null, 2));
      setSchemaStatus("Client schema selected. It will be validated again and pinned when the project is created.");
      return info;
    } catch (error) { setSelectedSchema(null); setSchemaStatus(error.message); return null; }
  };
  const useDefault = () => { setSelectedSchema(null); setSchemaText(""); setSchemaStatus("The standard organization schema will be used unless you provide a client JSON."); };

  const poll = async (jobId, projectId) => {
    for (;;) {
      const job = await getJob(jobId);
      setActivity(job.status === "retrying" ? "Tender analysis is retrying…" : `Tender analysis: ${job.status}`);
      if (["complete", "succeeded"].includes(job.status)) break;
      if (["failed", "cancelled"].includes(job.status)) throw new Error(job.error || "Tender analysis failed.");
      await new Promise((resolve) => window.setTimeout(resolve, 2000));
    }
    const updated = await getProject(projectId); onProjectChange(updated); onNavigate("review", updated.project_id);
  };

  const analyze = async (target) => {
    setActivity("Starting tender analysis…");
    const job = await understandProject(target.project_id, account.user.name || "Local procurement operator");
    await poll(job.job_id, target.project_id);
  };

  const submit = async () => {
    if (!project && !file) return onMessage("Choose a tender PDF or ZIP.", "error");
    setBusy(true); onMessage("");
    try {
      let target = project;
      if (!target) {
        setActivity("Uploading tender package…");
        target = await createProject({ file, policyFile, policyAudit, schemaJson: selectedSchema });
        onProjectChange(target);
      }
      await analyze(target);
    } catch (error) { setActivity(""); onMessage(error.message || "Tender analysis could not start.", "error"); }
    finally { setBusy(false); }
  };

  const info = selectedSchema ? (() => { try { return schemaSummary(JSON.parse(selectedSchema)); } catch { return null; } })() : defaultSchema;
  return <section className="workflow-panel tender-create"><div className="tender-create-heading"><span className="step-num">Step 1</span><h2>Create tender evaluation</h2></div>
    <div className="tender-create-grid"><section className="tender-create-card"><h3>Upload tender package</h3><label className={`tender-upload-zone ${project ? "pointer-events-none opacity-70" : ""}`}><span className="tender-file-icon" /><strong>{file?.name || (project ? "Tender package uploaded" : "Upload tender package")}</strong><span>PDF or ZIP</span><span className="tender-file-button">Choose file</span><input ref={fileRef} className="sr-only" type="file" accept=".pdf,.zip" disabled={Boolean(project)} onChange={(event) => setFile(event.target.files?.[0] || null)} /></label></section>
      <section className="tender-create-card"><h3>Analysis setup</h3><div className="analysis-schema"><span className="analysis-schema-icon" /><span><strong>{selectedSchema ? "Custom tender fields" : "Standard tender fields"}</strong><small>{info ? `${info.field_count} fields` : "Loading fields…"}</small></span></div><button className="link-button mt-3" type="button" disabled={Boolean(project)} onClick={() => setSchemaOpen((value) => !value)}>{schemaOpen ? "Hide custom fields" : "Use custom fields"}</button><label className="policy-switch"><span><strong>Compliance policy audit</strong><small>Upload the company policy used for review</small></span><span className="optional-badge">Optional</span><input type="checkbox" checked={policyAudit} disabled={Boolean(project)} onChange={(event) => setPolicyAudit(event.target.checked)} /></label>{policyAudit && !project && <label className="schema-dropzone mt-3"><strong>Upload compliance policy</strong><span>PDF, DOCX, JSON or TXT. It will require human review.</span><input type="file" accept=".pdf,.doc,.docx,.json,.txt" onChange={(event) => setPolicyFile(event.target.files?.[0] || null)} /></label>}</section></div>
    {schemaOpen && !project && <div className="schema-setup-panel"><div className="flex justify-between gap-4"><div><strong>Evaluation fields</strong><p>Use the standard fields or upload an approved client-specific field definition.</p></div><span className="schema-selection">{info ? `${info.schema_id} · ${info.field_count} fields` : "Loading standard fields…"}</span></div><div className="mt-4 grid grid-cols-2 gap-4 max-md:grid-cols-1"><label className="schema-dropzone"><strong>Upload custom fields</strong><span>Choose an approved JSON field definition. Maximum size: 2 MB.</span><input type="file" accept=".json,application/json" onChange={(event) => { const selected = event.target.files?.[0]; if (selected) selected.text().then(selectSchema); }} /></label><label className="field-label">Or paste JSON<textarea rows="8" value={schemaText} onChange={(event) => setSchemaText(event.target.value)} placeholder="Paste the approved field-definition JSON" /></label></div><div className="mt-3 flex gap-2"><button className="secondary" type="button" onClick={useDefault}>Use standard fields</button><button type="button" onClick={() => selectSchema(schemaText)}>Use this JSON</button></div><p className="mt-2 text-xs text-[var(--color-muted)]">{schemaStatus}</p></div>}
    <div className="setup-footer"><div><span>Tender package</span><strong>{file?.name || (project ? "Uploaded tender package" : "No tender selected")}</strong></div><button type="button" disabled={busy || (!project && !file)} onClick={submit}>{busy ? "Analyzing tender…" : project ? "Analyze tender" : "Create and analyze tender"}</button></div>
    {activity && <div className="operation-progress"><span className="operation-spinner" /><div><strong>Analyzing tender</strong><p>{activity}</p><small>You can leave this page. Progress is saved and updates automatically.</small></div></div>}
  </section>;
}
