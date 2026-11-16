import { api } from "./client.js";
import { endpoints } from "../config/api.js";

export const getUsage = (projectId = "") => api.get(`${endpoints.usage}?limit=100${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`);
export const verifyAudit = () => api.get(endpoints.auditVerify);
export const getProjectActivity = (projectId) => api.get(endpoints.reviewLog(projectId));
