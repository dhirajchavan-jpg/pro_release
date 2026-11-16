import { useEffect, useState } from "react";
import { verifyInvite, acceptInvite } from "../api/account.js";
import BrandMark from "../components/BrandMark.jsx";
import FormField from "../components/FormField.jsx";
import Notice from "../components/Notice.jsx";

const ROLE_LABELS = {
  admin: "Administrator",
  procurement_officer: "Procurement Officer",
  reviewer: "Technical Reviewer",
  commercial_officer: "Commercial Officer",
  approver: "Approver",
  auditor: "Auditor",
};

export default function InvitePage({ token, onAuthenticated, onNavigate }) {
  const [loading, setLoading] = useState(true);
  const [invite, setInvite] = useState(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [form, setForm] = useState({
    name: "",
    phone: "",
    password: "",
    confirmPassword: "",
  });

  useEffect(() => {
    if (!token) {
      setError("No invitation token was provided.");
      setLoading(false);
      return;
    }

    verifyInvite(token)
      .then((data) => {
        setInvite(data);
      })
      .catch((err) => {
        setError(err.message || "Invitation link is invalid or has expired.");
      })
      .finally(() => {
        setLoading(false);
      });
  }, [token]);

  const update = (e) => {
    setForm((current) => ({ ...current, [e.target.name]: e.target.value }));
  };

  const submit = async (e) => {
    e.preventDefault();
    if (form.password.length < 8) {
      setError("Password must contain at least 8 characters.");
      return;
    }
    if (form.password !== form.confirmPassword) {
      setError("Passwords do not match.");
      return;
    }

    setSubmitting(true);
    setError("");
    try {
      const data = await acceptInvite({
        token,
        name: form.name.trim(),
        phone: form.phone.trim(),
        password: form.password,
      });
      onAuthenticated(data);
    } catch (err) {
      setError(err.message || "Failed to accept invitation.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="grid min-h-screen grid-cols-[minmax(360px,.9fr)_minmax(500px,1.1fr)] bg-[var(--color-canvas)] max-md:grid-cols-1">
      <section className="flex flex-col justify-center bg-[var(--color-brand)] p-[clamp(44px,6vw,88px)] text-white max-md:min-h-72">
        <div className="flex items-center gap-3">
          <BrandMark className="size-9 bg-[#e9cdb3]" />
          <strong className="text-sm text-white">Tenderabl - Bid Evaluation</strong>
        </div>
        <div className="mt-[clamp(70px,10vh,130px)] max-w-120 max-md:mt-10">
          <span className="text-[11px] font-bold tracking-[.08em] text-[#aebdd1] uppercase">Team onboarding</span>
          <h1 className="mt-3 text-[clamp(28px,3vw,38px)] leading-tight font-semibold tracking-[-.025em] text-white">
            Join your procurement team
          </h1>
          <p className="mt-2 text-sm text-[#ced8e6]">
            Accept your invitation to collaborate on tender evaluations and audits.
          </p>
        </div>
      </section>

      <section className="flex items-center justify-center px-8 py-12 max-sm:px-4">
        <div className="w-full max-w-110">
          <Notice kind="error">{error}</Notice>

          {loading ? (
            <div className="rounded-[10px] border border-[var(--color-line)] bg-white p-7 text-center shadow-[var(--shadow-card)]">
              <p className="text-sm text-[var(--color-muted)]">Verifying invitation link…</p>
            </div>
          ) : invite ? (
            <form
              className="mt-4 rounded-[10px] border border-[var(--color-line)] bg-white p-7 shadow-[var(--shadow-card)]"
              onSubmit={submit}
            >
              <h2 className="text-xl font-semibold">Accept invitation</h2>
              <div className="mt-2 rounded-md bg-[var(--color-canvas)] p-3 text-sm">
                <p>
                  You've been invited to <strong>{invite.tenant_name}</strong> as{" "}
                  <span className="font-semibold text-[var(--color-brand)]">
                    {ROLE_LABELS[invite.role] || invite.role}
                  </span>.
                </p>
              </div>

              <div className="mt-5 grid gap-4">
                <FormField id="invite-email" label="Email address">
                  <input
                    id="invite-email"
                    value={invite.email}
                    disabled
                    className="bg-gray-100 text-[var(--color-muted)]"
                  />
                </FormField>

                <FormField id="invite-name" label="Your full name">
                  <input
                    id="invite-name"
                    name="name"
                    value={form.name}
                    onChange={update}
                    autoComplete="name"
                    required
                    autoFocus
                  />
                </FormField>

                <FormField id="invite-phone" label="Phone number (optional)">
                  <input
                    id="invite-phone"
                    name="phone"
                    value={form.phone}
                    onChange={update}
                    autoComplete="tel"
                  />
                </FormField>

                <FormField id="invite-password" label="Create password">
                  <input
                    id="invite-password"
                    name="password"
                    type="password"
                    value={form.password}
                    onChange={update}
                    autoComplete="new-password"
                    required
                  />
                </FormField>

                <FormField id="invite-confirm-password" label="Confirm password">
                  <input
                    id="invite-confirm-password"
                    name="confirmPassword"
                    type="password"
                    value={form.confirmPassword}
                    onChange={update}
                    autoComplete="new-password"
                    required
                  />
                </FormField>
              </div>

              <button className="mt-5 w-full" type="submit" disabled={submitting}>
                {submitting ? "Joining workspace…" : "Accept invitation & sign in"}
              </button>

              <div className="mt-4 text-center text-sm text-[var(--color-muted)]">
                Already have an account?{" "}
                <button className="link-button" type="button" onClick={() => onNavigate("login")}>
                  Sign in
                </button>
              </div>
            </form>
          ) : (
            <div className="mt-4 rounded-[10px] border border-[var(--color-line)] bg-white p-7 text-center shadow-[var(--shadow-card)]">
              <h2 className="text-lg font-semibold text-red-600">Invitation unavailable</h2>
              <p className="mt-2 text-sm text-[var(--color-muted)]">
                This invitation link is invalid or has expired. Please contact your workspace administrator for a new invite.
              </p>
              <button className="mt-5" type="button" onClick={() => onNavigate("login")}>
                Return to sign in
              </button>
            </div>
          )}
        </div>
      </section>
    </main>
  );
}
