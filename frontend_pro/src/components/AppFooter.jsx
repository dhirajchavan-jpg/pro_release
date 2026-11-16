import BrandMark from "./BrandMark.jsx";

export default function AppFooter() {
  return <footer className="border-t border-[var(--color-line)] bg-white"><div className="page-width grid min-h-14 grid-cols-[1fr_auto_1fr] items-center gap-6 text-[11px] text-[var(--color-muted)] max-sm:grid-cols-[1fr_auto] max-sm:py-3"><div className="flex items-center gap-2"><BrandMark className="size-7" /><strong className="text-xs">Tenderabl - Bid Evaluation</strong></div><span className="max-sm:hidden">Controlled procurement workspace</span><nav className="flex justify-end gap-4"><span>Evidence-led</span><span>Auditable</span></nav></div></footer>;
}
