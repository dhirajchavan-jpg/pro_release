import { api, downloadRequest } from "./client.js";
import { endpoints } from "../config/api.js";

const vendorQuery = (vendors) => {
  const query = new URLSearchParams();
  vendors.forEach((vendor) => query.append("vendor", vendor));
  return query.toString();
};

export const getReport = (projectId) => api.get(endpoints.report(projectId));
export const generateReport = (projectId) => api.post(endpoints.generateReport(projectId));
export const getReviewLog = (projectId) => api.get(endpoints.reviewLog(projectId));
export const downloadMarkdownReport = (projectId, vendors) => downloadRequest(`${endpoints.reportMarkdown(projectId)}?${vendorQuery(vendors)}`);
export const downloadExcelReport = (projectId, vendors) => downloadRequest(`${endpoints.reportExcel(projectId)}?${vendorQuery(vendors)}`);
