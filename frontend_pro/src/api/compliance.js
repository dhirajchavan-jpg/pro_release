import { api } from "./client.js";
import { endpoints } from "../config/api.js";

export const getCompliance = (projectId) => api.get(endpoints.compliance(projectId));
export const runCompliance = (projectId) => api.post(endpoints.runCompliance(projectId));
export const selectCompliancePolicy = (projectId, policyId) => api.post(endpoints.compliancePolicy(projectId), { policy_id: policyId });
export const resolveComplianceFinding = (projectId, findingId, payload) => api.post(endpoints.resolveComplianceFinding(projectId, findingId), payload);
