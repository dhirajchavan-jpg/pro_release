function normalizeBaseUrl(value) {
  const baseUrl = value?.trim();

  if (!baseUrl) {
    throw new Error("VITE_API_BASE_URL is required.");
  }

  return baseUrl.replace(/\/+$/, "");
}

function normalizeTimeout(value) {
  const timeout = Number(value);
  return Number.isFinite(timeout) && timeout > 0 ? timeout : 30000;
}

export const apiConfig = Object.freeze({
  baseUrl: normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL),
  timeoutMs: normalizeTimeout(import.meta.env.VITE_API_TIMEOUT_MS),
});

export const endpoints = Object.freeze({
  health: "/health",
  ready: "/ready",
  account: "/workbench/account",
  login: "/workbench/account/login",
  register: "/workbench/account/register",
  logout: "/workbench/account/logout",
  users: "/workbench/account/users",
  user: (userId) => `/workbench/account/users/${encodeURIComponent(userId)}`,
  invites: "/workbench/account/invites",
  invite: (inviteId) => `/workbench/account/invites/${encodeURIComponent(inviteId)}`,
  verifyInvite: (token) => `/workbench/account/invites/verify?token=${encodeURIComponent(token)}`,
  acceptInvite: "/workbench/account/invites/accept",
  projects: "/v1/workbench/projects",
  project: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}`,
  defaultFieldSchema: "/v1/workbench/field-schema/default",
  projectFieldSchema: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/field-schema`,
  understandProject: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/understand`,
  job: (jobId) => `/v1/workbench/jobs/${encodeURIComponent(jobId)}`,
  ledger: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/ledger`,
  ledgerCorrections: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/ledger/corrections`,
  projectDocuments: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/documents`,
  contractDraft: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/contract/draft`,
  freezeContract: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/contract/freeze`,
  bids: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/bids`,
  bid: (projectId, vendorId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/bids/${encodeURIComponent(vendorId)}`,
  replaceBid: (projectId, vendorId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/bids/${encodeURIComponent(vendorId)}/replace`,
  intake: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/intake`,
  classification: (projectId, vendorId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/bids/${encodeURIComponent(vendorId)}/classification`,
  classificationReview: (projectId, vendorId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/bids/${encodeURIComponent(vendorId)}/review`,
  proposeClassification: (projectId, vendorId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/bids/${encodeURIComponent(vendorId)}/classification/propose`,
  correctClassification: (projectId, vendorId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/bids/${encodeURIComponent(vendorId)}/classification/correct`,
  approveClassification: (projectId, vendorId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/bids/${encodeURIComponent(vendorId)}/classification/approve`,
  compliance: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/compliance`,
  runCompliance: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/compliance/run`,
  compliancePolicy: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/compliance/policy`,
  resolveComplianceFinding: (projectId, findingId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/compliance/findings/${encodeURIComponent(findingId)}/resolve`,
  evaluate: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/evaluate`,
  results: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/results`,
  humanReview: (projectId, vendorId, criterionId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/vendors/${encodeURIComponent(vendorId)}/criteria/${encodeURIComponent(criterionId)}/human-review`,
  report: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/report`,
  generateReport: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/report/generate`,
  reportMarkdown: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/report/markdown`,
  reportExcel: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/report/excel`,
  reviewLog: (projectId) => `/v1/workbench/projects/${encodeURIComponent(projectId)}/review-log`,
  usage: "/v1/workbench/usage",
  auditVerify: "/v1/workbench/audit/verify",
});
