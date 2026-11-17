import { useState } from "react";

import { login, register } from "../api/account.js";
import BrandMark from "../components/BrandMark.jsx";
import FormField from "../components/FormField.jsx";
import Notice from "../components/Notice.jsx";

export default function AuthPage({ mode, serviceError, onAuthenticated, onNavigate }) {
  const [form, setForm] = useState({
    workspaceName: "",
    name: "",
    email: "",
    phone: "",
    username: "",
    password: "",
  });
  const [message, setMessage] = useState(serviceError);
  const [submitting, setSubmitting] = useState(false);
  const isRegister = mode === "register";

  const update = (event) => {
    setForm((current) => ({ ...current, [event.target.name]: event.target.value }));
  };

  const submit = async (event) => {
    event.preventDefault();
    setSubmitting(true);
    setMessage("");
    try {
      const data = isRegister
        ? await register({
            workspace_name: form.workspaceName.trim(),
            name: form.name.trim(),
            email: form.email.trim(),
            phone: form.phone.trim(),
            password: form.password,
          })
        : await login({ username: form.username.trim(), password: form.password });
      onAuthenticated(data);
    } catch (error) {
      setMessage(error.message || "The request could not be processed.");
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
          <span className="text-[11px] font-bold tracking-[.08em] text-[#aebdd1] uppercase">Bid evaluation</span>
          <h1 className="mt-3 text-[clamp(28px,3vw,38px)] leading-tight font-semibold tracking-[-.025em] text-white">
            Your tender workspace
          </h1>
          <p className="mt-2 text-sm text-[#ced8e6]">Access previous tenders or start a new evaluation.</p>
        </div>
      </section>

      <section className="flex items-center justify-center px-8 py-12 max-sm:px-4">
        <div className="w-full max-w-110">
          <Notice kind="error">{message}</Notice>
          <form className="mt-4 rounded-[10px] border border-[var(--color-line)] bg-white p-7 shadow-[var(--shadow-card)]" onSubmit={submit}>
            <h2 className="text-xl font-semibold">{isRegister ? "Create your workspace" : "Sign in"}</h2>
            <div className="mt-5 grid gap-4">
              {isRegister ? (
                <>
                  <FormField id="register-workspace-name" label="Workspace name">
                    <input id="register-workspace-name" name="workspaceName" value={form.workspaceName} onChange={update} autoComplete="organization" placeholder="e.g. Acme Procurement" required autoFocus />
                  </FormField>
                  <FormField id="register-name" label="Name">
                    <input id="register-name" name="name" value={form.name} onChange={update} autoComplete="name" required />
                  </FormField>
                  <FormField id="register-email" label="Email">
                    <input id="register-email" name="email" type="email" value={form.email} onChange={update} autoComplete="email" required />
                  </FormField>
                  <FormField id="register-phone" label="Phone (optional)">
                    <input id="register-phone" name="phone" value={form.phone} onChange={update} autoComplete="tel" />
                  </FormField>
                </>
              ) : (
                <FormField id="login-username" label="Username">
                  <input id="login-username" name="username" value={form.username} onChange={update} autoComplete="username" required autoFocus />
                </FormField>
              )}
              <FormField id="auth-password" label="Password">
                <input id="auth-password" name="password" type="password" value={form.password} onChange={update} autoComplete={isRegister ? "new-password" : "current-password"} required />
              </FormField>
            </div>
            <button className="mt-5 w-full" type="submit" disabled={submitting}>
              {submitting ? "Please wait…" : isRegister ? "Register" : "Sign in"}
            </button>
            {isRegister ? (
              <p className="mt-4 text-center text-sm text-[var(--color-muted)]">
                Already have an account?{" "}
                <button className="link-button" type="button" onClick={() => onNavigate("login")}>Sign in</button>
              </p>
            ) : (
              <div className="mt-4 text-center text-sm text-[var(--color-muted)]">
                <p>Access is managed by your procurement administrator.</p>
                <p className="mt-2">
                  Need an account?{" "}
                  <button className="link-button" type="button" onClick={() => onNavigate("register")}>Create account</button>
                </p>
              </div>
            )}
          </form>
        </div>
      </section>
    </main>
  );
}
