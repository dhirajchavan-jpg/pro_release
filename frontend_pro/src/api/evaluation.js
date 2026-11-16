import { api } from "./client.js";
import { endpoints } from "../config/api.js";

export const runEvaluation = (projectId, vendorId = null, force = false) => api.post(`${endpoints.evaluate(projectId)}?force=${force}`, vendorId ? { vendor_id: vendorId } : {});
export const getResults = (projectId) => api.get(endpoints.results(projectId));
export const saveHumanReview = (projectId, vendorId, criterionId, payload) => api.post(endpoints.humanReview(projectId, vendorId, criterionId), payload);
