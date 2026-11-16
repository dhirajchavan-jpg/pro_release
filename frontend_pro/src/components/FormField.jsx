export default function FormField({ label, id, children, hint }) {
  return <div className="grid gap-1.5"><label className="field-label" htmlFor={id}>{label}</label>{children}{hint && <small className="text-[11px] text-[var(--color-muted)]">{hint}</small>}</div>;
}
