import BrandMark from "./BrandMark.jsx";

function initials(name) {
  return (name || "Local procurement operator").split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0].toUpperCase()).join("") || "LO";
}

export default function AppHeader({ user, onAdmin }) {
  const name = user?.name || "Local procurement operator";
  const isAdmin = user?.role === "admin";

  return (
    <header className="sticky top-0 z-50 border-b border-[var(--color-line)] bg-white">
      <div className="page-width flex min-h-15 items-center justify-between gap-6">
        <div className="flex items-center gap-3"><BrandMark /><strong className="text-sm">Tenderabl - Bid Evaluation</strong></div>
        <div className="flex items-center gap-4">
          {isAdmin && <button className="secondary text-sm" type="button" onClick={onAdmin}>Team &amp; access</button>}
          <span className="size-[7px] rounded-full bg-emerald-400 shadow-[0_0_0_3px_rgb(71_205_137_/_18%)]" aria-label="System online" />
          <span className="flex items-center gap-2.5">
            <span className="grid text-right leading-tight max-sm:hidden"><small className="text-[9px] font-semibold tracking-[.06em] text-[var(--color-muted)] uppercase">Reviewer</small><strong className="max-w-55 truncate text-xs">{name}</strong></span>
            <span className="grid size-8.5 place-items-center rounded-full border border-[var(--color-line)] bg-[var(--color-brand-soft)] text-[11px] font-bold">{initials(name)}</span>
          </span>
        </div>
      </div>
    </header>
  );
}
