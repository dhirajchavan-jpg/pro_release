import { api } from "./client.js";
import { endpoints } from "../config/api.js";

export function addBid(projectId, vendorName, file) {
  const body = new FormData();
  body.append("vendor_name", vendorName);
  body.append("received_at", new Date().toISOString());
  body.append("file", file);
  return api.post(endpoints.bids(projectId), body);
}

export function replaceBid(projectId, vendorId, file, reason) {
  const body = new FormData();
  body.append("file", file);
  body.append("reason", reason);
  return api.post(endpoints.replaceBid(projectId, vendorId), body);
}

export const deleteBid = (projectId, vendorId) => api.delete(endpoints.bid(projectId, vendorId));
export const runIntake = (projectId) => api.post(endpoints.intake(projectId));
export const getClassification = (projectId, vendorId) => api.get(endpoints.classification(projectId, vendorId));
export const getClassificationReview = (projectId, vendorId) => api.get(endpoints.classificationReview(projectId, vendorId));
export const proposeClassification = (projectId, vendorId) => api.post(endpoints.proposeClassification(projectId, vendorId));
export const correctClassification = (projectId, vendorId, payload) => api.post(endpoints.correctClassification(projectId, vendorId), payload);
export const approveClassification = (projectId, vendorId, payload) => api.post(endpoints.approveClassification(projectId, vendorId), payload);
