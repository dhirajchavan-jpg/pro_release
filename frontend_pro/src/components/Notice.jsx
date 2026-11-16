export default function Notice({ children, kind = "info", onDismiss }) {
  if (!children) return null;
  const styles = kind === "error" ? "border-red-200 bg-red-50 text-red-700" : kind === "success" ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-slate-200 bg-slate-50 text-slate-700";
  return <div className={`flex items-start justify-between gap-4 rounded-lg border px-4 py-3 text-sm ${styles}`} role={kind === "error" ? "alert" : "status"}><span>{children}</span>{onDismiss && <button className="link-button text-lg" type="button" onClick={onDismiss} aria-label="Dismiss">×</button>}</div>;
}
