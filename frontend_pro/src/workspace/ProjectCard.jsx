import { useState } from "react";
import { updateProject } from "../api/projects.js";
import { projectPage, projectStageLabel } from "./project.js";

export default function ProjectCard({ project, onChange, onMessage, onOpen }) {
  const [editing, setEditing] = useState(false); const [saving, setSaving] = useState(false);
  const [number, setNumber] = useState(project.tender_number || project.project_id); const [title, setTitle] = useState(project.title || "");
  const cancel = () => { setNumber(project.tender_number || project.project_id); setTitle(project.title || ""); setEditing(false); };
  const save = async () => { setSaving(true); try { const updated = await updateProject(project.project_id, { tender_number: number.trim(), title: title.trim() }); onChange(updated); setEditing(false); onMessage("Tender details saved.", "success"); } catch (error) { onMessage(error.message || "Could not save tender details.", "error"); } finally { setSaving(false); } };
  const resume = () => onOpen(project, projectPage(project));
  return <article className="project-card"><div><div className="font-bold break-all">{project.tender_number || project.project_id}</div><div className="mt-1 text-xs text-[var(--color-muted)]">Project {project.project_id}</div></div><h3 className="font-semibold">{project.title || "Untitled tender"}</h3><div><span className={`status-pill ${project.stage === "report_generated" ? "status-pass" : "status-review"}`}>{projectStageLabel(project.stage)}</span></div><div className="flex flex-wrap gap-2"><button className="secondary" type="button" onClick={() => setEditing(true)}>Edit</button><button type="button" onClick={resume}>Continue</button></div>{editing && <div className="col-span-full grid grid-cols-[minmax(180px,230px)_minmax(280px,1fr)_auto] items-end gap-3 border-t border-[var(--color-line)] pt-3 max-lg:grid-cols-1"><label className="field-label">Tender number<input value={number} onChange={(event) => setNumber(event.target.value)} autoFocus /></label><label className="field-label">Tender title<input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Tender title" /></label><div className="flex gap-2"><button type="button" disabled={saving} onClick={save}>{saving ? "Saving…" : "Save"}</button><button className="secondary" type="button" disabled={saving} onClick={cancel}>Cancel</button></div></div>}</article>;
}
