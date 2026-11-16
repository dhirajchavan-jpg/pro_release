import { api } from "./client.js";
import { endpoints } from "../config/api.js";

export function createProject({ file, projectKey, title, policyAudit, schemaJson }) {
  const body = new FormData();
  body.append("file", file);
  if (projectKey) body.append("project_key", projectKey);
  if (title) body.append("title", title);
  if (schemaJson) body.append("schema_json", schemaJson);
  body.append("policy_audit", String(policyAudit));
  return api.post(endpoints.projects, body);
}

export const updateProject = (projectId, changes) => api.patch(endpoints.project(projectId), changes);
export const getProject = (projectId) => api.get(endpoints.project(projectId));
export const getDefaultFieldSchema = () => api.get(endpoints.defaultFieldSchema);
export const getProjectFieldSchema = (projectId) => api.get(endpoints.projectFieldSchema(projectId));
export function understandProject(projectId, reviewer) {
  const body = new FormData();
  body.append("reviewed_by", reviewer);
  body.append("reviewed_at", new Date().toISOString());
  return api.post(endpoints.understandProject(projectId), body);
}
export const getJob = (jobId) => api.get(endpoints.job(jobId));
export const getLedger = (projectId) => api.get(endpoints.ledger(projectId));
export const correctLedger = (projectId, correction) => api.post(endpoints.ledgerCorrections(projectId), correction);
export const getProjectDocuments = (projectId) => api.get(endpoints.projectDocuments(projectId));
export const getContractDraft = (projectId) => api.get(endpoints.contractDraft(projectId));
export const createContractDraft = (projectId) => api.post(endpoints.contractDraft(projectId));
export const updateContractDraft = (projectId, payload) => api.patch(endpoints.contractDraft(projectId), payload);
export const freezeContract = (projectId, payload) => api.post(endpoints.freezeContract(projectId), payload);
