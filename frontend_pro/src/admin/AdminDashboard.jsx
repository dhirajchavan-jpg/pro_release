import { useEffect, useState } from "react";

import { getProjectActivity, getUsage, verifyAudit } from "../api/admin.js";
import {
  listUsers,
  updateUser,
  listRoles,
  listInvites,
  createInvite,
  deleteInvite,
} from "../api/account.js";
import Notice from "../components/Notice.jsx";

const number = (value) => Number(value || 0).toLocaleString("en-IN");
const words = (value) =>
  String(value || "Activity")
    .replace(/^workbench\./, "")
    .replace(/[._-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
const date = (value) =>
  value
    ? new Date(value).toLocaleString("en-IN", {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : "—";
const percentage = (value) =>
  value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value))
    ? `${Math.max(0, Number(value)).toFixed(1)}%`
    : "—";
const duration = (milliseconds) => {
  const seconds = Math.round(Number(milliseconds || 0) / 1000);
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
};
const budgetShare = (value, budget) =>
  budget?.enabled && Number(budget.limit_inr) > 0 && Number.isFinite(Number(value))
    ? percentage((Number(value) / Number(budget.limit_inr)) * 100)
    : "—";

const DEFAULT_ROLES = [
  { name: "admin", label: "Administrator" },
  { name: "procurement_officer", label: "Procurement Officer" },
  { name: "reviewer", label: "Technical Reviewer" },
  { name: "commercial_officer", label: "Commercial Officer" },
  { name: "approver", label: "Approver" },
  { name: "auditor", label: "Auditor" },
];
const roleLabel = (role) => role?.label || words(role?.name);

export default function AdminDashboard({ account, onBack }) {
  const [tab, setTab] = useState("team"); // "team" or "usage"
  const [projectId, setProjectId] = useState("");
  const [usage, setUsage] = useState(null);
  const [audit, setAudit] = useState(null);
  const [activity, setActivity] = useState([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  const [updated, setUpdated] = useState("");

  // Team & Invites state
  const [users, setUsers] = useState([]);
  const [roles, setRoles] = useState(DEFAULT_ROLES);
  const [invites, setInvites] = useState([]);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("reviewer");
  const [lastInviteUrl, setLastInviteUrl] = useState("");
  const [copied, setCopied] = useState(false);
  const [teamLoading, setTeamLoading] = useState(false);

  const loadUsageData = async () => {
    setBusy(true);
    setNotice("");
    try {
      const [usageData, auditData, activityData] = await Promise.all([
        getUsage(projectId),
        verifyAudit(),
        projectId ? getProjectActivity(projectId) : Promise.resolve({ records: [] }),
      ]);
      setUsage(usageData);
      setAudit(auditData);
      setActivity(activityData.records || []);
      setUpdated(
        new Date().toLocaleTimeString("en-IN", {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        })
      );
    } catch (error) {
      setNotice(error.message || "Dashboard data could not be loaded.");
    } finally {
      setBusy(false);
    }
  };

  const loadTeamData = async () => {
    setTeamLoading(true);
    setNotice("");
    try {
      const [usersRes, rolesRes, invitesRes] = await Promise.all([listUsers(), listRoles(), listInvites()]);
      setUsers(usersRes.users || []);
      setRoles(rolesRes.roles || DEFAULT_ROLES);
      setInvites(invitesRes.invites || []);
    } catch (error) {
      setNotice(error.message || "Failed to load team data.");
    } finally {
      setTeamLoading(false);
    }
  };

  useEffect(() => {
    if (tab === "usage") {
      loadUsageData();
    } else {
      loadTeamData();
    }
  }, [tab, projectId]);

  const handleSendInvite = async (e) => {
    e.preventDefault();
    if (!inviteEmail.trim()) return;
    setBusy(true);
    setNotice("");
    setSuccessMsg("");
    setLastInviteUrl("");
    setCopied(false);
    try {
      const res = await createInvite({
        email: inviteEmail.trim(),
        role: inviteRole,
        ttl_days: 7,
      });
      setLastInviteUrl(res.invite_url);
      setInviteEmail("");
      setSuccessMsg(`Invitation generated for ${res.email}! Share the link below.`);
      loadTeamData();
    } catch (err) {
      setNotice(err.message || "Could not create invitation.");
    } finally {
      setBusy(false);
    }
  };

  const handleCopyLink = () => {
    if (!lastInviteUrl) return;
    navigator.clipboard.writeText(lastInviteUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 3000);
  };

  const handleRevokeInvite = async (inviteId) => {
    if (!window.confirm("Are you sure you want to revoke this invitation?")) return;
    setBusy(true);
    try {
      await deleteInvite(inviteId);
      loadTeamData();
    } catch (err) {
      setNotice(err.message || "Failed to revoke invitation.");
    } finally {
      setBusy(false);
    }
  };

  const handleRoleChange = async (userId, newRole) => {
    setBusy(true);
    setNotice("");
    try {
      await updateUser(userId, { role: newRole });
      setSuccessMsg("User role updated successfully.");
      loadTeamData();
    } catch (err) {
      setNotice(err.message || "Failed to update role.");
    } finally {
      setBusy(false);
    }
  };

  const handleToggleActive = async (userId, currentActive) => {
    setBusy(true);
    setNotice("");
    try {
      await updateUser(userId, { is_active: !currentActive });
      loadTeamData();
    } catch (err) {
      setNotice(err.message || "Failed to update user status.");
    } finally {
      setBusy(false);
    }
  };

  const summary = usage?.summary || {};
  const budget = usage?.budget || {};
  const percentUsed = budget.enabled ? percentage(budget.percent_used) : "—";

  return (
    <main id="main-content" className="admin-page page-width py-7">
      <div className="admin-page-head">
        <div>
          <span className="eyebrow">Administration</span>
          <h1>Workspace settings & access</h1>
          <button className="link-button mt-2" type="button" onClick={onBack}>
            ← Tender workspace
          </button>
        </div>

        <div className="flex items-center gap-3">
          <div className="inline-flex rounded-lg border border-[var(--color-line)] bg-white p-1">
            <button
              type="button"
              className={`rounded px-3 py-1 text-sm font-medium transition ${
                tab === "team"
                  ? "bg-[var(--color-brand)] text-white"
                  : "text-[var(--color-muted)] hover:text-black"
              }`}
              onClick={() => setTab("team")}
            >
              Team & access
            </button>
            <button
              type="button"
              className={`rounded px-3 py-1 text-sm font-medium transition ${
                tab === "usage"
                  ? "bg-[var(--color-brand)] text-white"
                  : "text-[var(--color-muted)] hover:text-black"
              }`}
              onClick={() => setTab("usage")}
            >
              Usage & audit
            </button>
          </div>
          <button
            className="secondary"
            type="button"
            disabled={busy || teamLoading}
            onClick={tab === "usage" ? loadUsageData : loadTeamData}
          >
            {busy || teamLoading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      <Notice kind="error" onDismiss={() => setNotice("")}>
        {notice}
      </Notice>
      {successMsg && (
        <div className="mb-4 rounded-md border border-emerald-300 bg-emerald-50 p-4 text-sm text-emerald-800">
          {successMsg}
        </div>
      )}

      {tab === "team" ? (
        <div className="mt-6 grid gap-6">
          {/* Invite Member Section */}
          <section className="admin-panel">
            <div className="admin-panel-head">
              <div>
                <span className="eyebrow">Invite member</span>
                <h2>Add team member with pre-assigned role</h2>
              </div>
            </div>

            <form onSubmit={handleSendInvite} className="mt-4 grid max-w-2xl gap-4">
              <div className="grid grid-cols-[1fr_200px] gap-3 max-sm:grid-cols-1">
                <div>
                  <label className="field-label" htmlFor="invite-email-input">
                    Email address
                  </label>
                  <input
                    id="invite-email-input"
                    type="email"
                    placeholder="colleague@organization.gov"
                    value={inviteEmail}
                    onChange={(e) => setInviteEmail(e.target.value)}
                    required
                  />
                </div>
                <div>
                  <label className="field-label" htmlFor="invite-role-select">
                    Assigned role
                  </label>
                  <select
                    id="invite-role-select"
                    value={inviteRole}
                    onChange={(e) => setInviteRole(e.target.value)}
                  >
                    {roles.map((r) => (
                      <option key={r.id || r.name} value={r.name}>
                        {roleLabel(r)}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <div className="flex items-center gap-4">
                <button type="submit" disabled={busy || !inviteEmail.trim()}>
                  {busy ? "Generating…" : "Generate invitation link"}
                </button>
                <span className="text-xs text-[var(--color-muted)]">
                  Invites expire automatically after 7 days.
                </span>
              </div>
            </form>

            {lastInviteUrl && (
              <div className="mt-5 rounded-lg border border-blue-200 bg-blue-50 p-4">
                <p className="text-xs font-semibold uppercase tracking-wider text-blue-900">
                  Share this invitation link:
                </p>
                <div className="mt-2 flex items-center gap-2">
                  <input
                    type="text"
                    readOnly
                    value={lastInviteUrl}
                    className="flex-1 bg-white text-sm font-mono text-gray-800"
                  />
                  <button type="button" onClick={handleCopyLink}>
                    {copied ? "Copied!" : "Copy link"}
                  </button>
                </div>
              </div>
            )}
          </section>

          {/* Members Table */}
          <section className="admin-panel">
            <div className="admin-panel-head">
              <div>
                <span className="eyebrow">Workspace members</span>
                <h2>Active accounts ({users.length})</h2>
              </div>
            </div>

            <div className="table-wrap mt-3">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Member</th>
                    <th>Email</th>
                    <th>Role</th>
                    <th>Status</th>
                    <th>Joined</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((u) => (
                    <tr key={u.id}>
                      <td>
                        <strong>{u.name || "Unnamed"}</strong>
                        {u.phone && <small>{u.phone}</small>}
                      </td>
                      <td>{u.email}</td>
                      <td>
                        <select
                          className="text-xs py-1 px-2 border rounded"
                          value={u.role}
                          disabled={busy}
                          onChange={(e) => handleRoleChange(u.id, e.target.value)}
                        >
                          {roles.map((r) => (
                            <option key={r.id || r.name} value={r.name}>
                              {roleLabel(r)}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td>
                        <span
                          className={`status-pill ${
                            u.is_active ? "status-pass" : "status-fail"
                          }`}
                        >
                          {u.is_active ? "Active" : "Inactive"}
                        </span>
                      </td>
                      <td>{date(u.created_at)}</td>
                      <td>
                        <button
                          type="button"
                          className="link-button text-xs"
                          disabled={busy}
                          onClick={() => handleToggleActive(u.id, u.is_active)}
                        >
                          {u.is_active ? "Deactivate" : "Activate"}
                        </button>
                      </td>
                    </tr>
                  ))}
                  {!users.length && (
                    <tr>
                      <td colSpan="6" className="admin-empty">
                        No team members registered yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          {/* Pending Invites Table */}
          <section className="admin-panel">
            <div className="admin-panel-head">
              <div>
                <span className="eyebrow">Pending invitations</span>
                <h2>Outstanding invites ({invites.filter((i) => !i.accepted_at).length})</h2>
              </div>
            </div>

            <div className="table-wrap mt-3">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Email</th>
                    <th>Role</th>
                    <th>Created</th>
                    <th>Expires</th>
                    <th>Status</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {invites.map((inv) => (
                    <tr key={inv.id}>
                      <td>
                        <strong>{inv.email}</strong>
                      </td>
                      <td>
                        {roleLabel(roles.find((r) => r.name === inv.role) || { name: inv.role })}
                      </td>
                      <td>{date(inv.created_at)}</td>
                      <td>{date(inv.expires_at)}</td>
                      <td>
                        <span
                          className={`status-pill ${
                            inv.accepted_at ? "status-pass" : "status-review"
                          }`}
                        >
                          {inv.accepted_at ? "Accepted" : "Pending"}
                        </span>
                      </td>
                      <td>
                        {!inv.accepted_at && (
                          <button
                            type="button"
                            className="link-button text-red-600 text-xs"
                            disabled={busy}
                            onClick={() => handleRevokeInvite(inv.id)}
                          >
                            Revoke
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                  {!invites.length && (
                    <tr>
                      <td colSpan="6" className="admin-empty">
                        No pending invitations.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      ) : (
        /* Usage & Audit Tab */
        <>
          <div className="admin-filter mt-4">
            <label className="field-label">
              Tender
              <select
                value={projectId}
                onChange={(event) => setProjectId(event.target.value)}
              >
                <option value="">All tenders</option>
                {(account.projects || []).map((project) => (
                  <option key={project.project_id} value={project.project_id}>
                    {project.title || project.tender_number || project.project_id}
                  </option>
                ))}
              </select>
            </label>
            <small>{updated ? `Updated ${updated}` : "Loading…"}</small>
          </div>

          <section className="admin-metrics mt-4" aria-label="Usage summary">
            <article>
              <span>Budget used</span>
              <strong>{percentUsed}</strong>
              <small>
                {budget.enabled ? "of account budget" : "Budget tracking disabled"}
              </small>
            </article>
            <article>
              <span>Budget remaining</span>
              <strong>
                {budget.enabled && budget.percent_used != null
                  ? percentage(100 - Math.max(0, Number(budget.percent_used)))
                  : "—"}
              </strong>
              <small>{budget.enabled ? "available share" : "Budget tracking disabled"}</small>
            </article>
            <article>
              <span>Tokens</span>
              <strong>{number(summary.total_tokens)}</strong>
              <small>
                {number(summary.input_tokens)} in · {number(summary.output_tokens)} out
              </small>
            </article>
            <article>
              <span>Provider calls</span>
              <strong>{number(summary.call_count)}</strong>
              <small>{number(summary.cache_hits)} cache hits</small>
            </article>
            <article>
              <span>Failures</span>
              <strong>
                {number(
                  Number(summary.failed_calls || 0) +
                    Number(summary.budget_blocked_calls || 0)
                )}
              </strong>
              <small>{number(summary.retry_count)} retries</small>
            </article>
            <article>
              <span>Audit chain</span>
              <strong className={audit?.valid ? "text-emerald-700" : "text-red-700"}>
                {audit ? (audit.valid ? "Verified" : "Check failed") : "—"}
              </strong>
              <small>{number(audit?.event_count)} protected events</small>
            </article>
          </section>

          <section className="admin-grid mt-6">
            <article className="admin-panel">
              <div className="admin-panel-head">
                <div>
                  <span className="eyebrow">Activity</span>
                  <h2>What changed</h2>
                </div>
                <span>{number(activity.length)} events</span>
              </div>
              {!activity.length ? (
                <p className="admin-empty">
                  {projectId
                    ? "No activity has been recorded for this tender."
                    : "Select a tender to view its activity."}
                </p>
              ) : (
                <ol className="admin-timeline">
                  {activity
                    .slice()
                    .reverse()
                    .map((record, index) => (
                      <li key={record.record_id || index}>
                        <time>{date(record.recorded_at)}</time>
                        <div>
                          <strong>
                            {words(record.reason_code || record.action)}
                          </strong>
                          {record.reason && <p>{record.reason}</p>}
                          <small>
                            {record.actor_role === "system" ? "System" : "User"} ·{" "}
                            {words(
                              record.entity_type || record.subject_artifact_name
                            )}
                          </small>
                        </div>
                      </li>
                    ))}
                </ol>
              )}
            </article>

            <aside className="admin-side">
              <article className="admin-panel">
                <div className="admin-panel-head">
                  <div>
                    <span className="eyebrow">Tenders</span>
                    <h2>Workspace</h2>
                  </div>
                </div>
                <div className="table-wrap">
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th>Tender</th>
                        <th>Stage</th>
                        <th>Bidders</th>
                        <th>Updated</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(account.projects || []).map((project) => (
                        <tr
                          key={project.project_id}
                          tabIndex="0"
                          onClick={() => setProjectId(project.project_id)}
                          onKeyDown={(event) => {
                            if (["Enter", " "].includes(event.key))
                              setProjectId(project.project_id);
                          }}
                        >
                          <td>
                            <strong>{project.title || "Untitled tender"}</strong>
                            <small>
                              {project.tender_number || project.project_id}
                            </small>
                          </td>
                          <td>{words(project.stage)}</td>
                          <td>
                            {number(
                              project.portfolio_summary?.bidder_count ??
                                project.bidders?.length
                            )}
                          </td>
                          <td>{date(project.updated_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </article>

              <article className="admin-panel">
                <div className="admin-panel-head">
                  <div>
                    <span className="eyebrow">Compute</span>
                    <h2>Usage by stage</h2>
                  </div>
                </div>
                <div className="table-wrap">
                  <table className="admin-table">
                    <thead>
                      <tr>
                        <th>Stage</th>
                        <th>Calls</th>
                        <th>Tokens / units</th>
                        <th>Budget share</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(usage?.stages || []).map((stage) => (
                        <tr key={stage.stage}>
                          <td>{words(stage.stage)}</td>
                          <td>{number(stage.call_count)}</td>
                          <td>
                            {stage.total_tokens
                              ? `${number(stage.total_tokens)} tokens`
                              : `${number(stage.input_units)} units`}
                          </td>
                          <td>
                            {budgetShare(stage.estimated_cost_inr, budget)}
                          </td>
                        </tr>
                      ))}
                      {!(usage?.stages || []).length && (
                        <tr>
                          <td colSpan="4">No provider usage recorded.</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </article>
            </aside>
          </section>

          <section className="admin-panel mt-5">
            <div className="admin-panel-head">
              <div>
                <span className="eyebrow">Provider log</span>
                <h2>Recent calls</h2>
              </div>
              <span>{number(usage?.recent_calls?.length)} shown</span>
            </div>
            <div className="table-wrap">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Stage</th>
                    <th>Capability</th>
                    <th>Provider / model</th>
                    <th>Status</th>
                    <th>Tokens / units</th>
                    <th>Duration</th>
                    <th>Budget share</th>
                  </tr>
                </thead>
                <tbody>
                  {(usage?.recent_calls || []).map((call) => (
                    <tr key={call.id}>
                      <td>{date(call.started_at)}</td>
                      <td>{words(call.stage)}</td>
                      <td>{words(call.capability)}</td>
                      <td>
                        <strong>{call.provider || "—"}</strong>
                        <small>{call.model || "—"}</small>
                      </td>
                      <td>
                        <span
                          className={`status-pill ${
                            call.status === "succeeded"
                              ? "status-pass"
                              : call.status === "failed"
                              ? "status-fail"
                              : "status-review"
                          }`}
                        >
                          {words(call.status)}
                        </span>
                      </td>
                      <td>
                        {call.total_tokens
                          ? `${number(call.total_tokens)} tokens`
                          : `${number(call.input_units)} ${call.unit_name || "units"}`}
                      </td>
                      <td>{duration(call.duration_ms)}</td>
                      <td>
                        {call.priced
                          ? budgetShare(call.estimated_cost_inr, budget)
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </main>
  );
}
