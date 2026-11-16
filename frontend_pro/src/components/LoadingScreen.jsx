export default function LoadingScreen({ label }) {
  return <div className="grid min-h-screen place-items-center bg-[var(--color-canvas)]"><div className="flex items-center gap-3"><span className="size-5 animate-spin rounded-full border-2 border-slate-300 border-t-[var(--color-brand)]" /><strong className="text-sm">{label}</strong></div></div>;
}
