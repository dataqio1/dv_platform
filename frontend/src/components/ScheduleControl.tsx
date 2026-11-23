// Recurring-schedule UI for compare test cases, shared by the list (read-only icon) and the
// detail pages (editable control). Moved off the test-case list rows — those now show only a
// compact clock icon when a schedule exists; the actual editor lives on the detail/results page.

// Common cadences; the value is a 5-field cron. "Custom…" prompts for a raw expression.
export const SCHEDULE_PRESETS: { label: string; cron: string }[] = [
  { label: "Hourly", cron: "0 * * * *" },
  { label: "Daily · 02:00", cron: "0 2 * * *" },
  { label: "Weekdays · 06:00", cron: "0 6 * * 1-5" },
  { label: "Weekly · Mon 02:00", cron: "0 2 * * 1" },
];

/** Human label for a cron if it matches a known preset, else the raw expression. */
export function scheduleLabel(cron: string | null | undefined): string | null {
  if (!cron) return null;
  return SCHEDULE_PRESETS.find((p) => p.cron === cron)?.label ?? cron;
}

/** Compact, read-only clock chip for a test-case row: shown only when a schedule exists.
 *  Hover shows the cadence and (if known) the next run. Renders nothing when unscheduled. */
export function ScheduledIcon({ cron, nextRun }: { cron: string | null; nextRun?: string | null }) {
  if (!cron) return null;
  const label = scheduleLabel(cron);
  const title = nextRun
    ? `Scheduled: ${label} · next run ${new Date(nextRun).toLocaleString()}`
    : `Scheduled: ${label}`;
  return (
    <span
      title={title}
      className="inline-flex shrink-0 items-center gap-1 rounded-full border border-teal-200 bg-teal-50 px-2 py-0.5 text-[11px] font-medium text-teal-700"
    >
      <span aria-hidden>⏱</span>
      <span className="hidden lg:inline">{label}</span>
    </span>
  );
}

/** Editable recurring-schedule picker for the detail page. "Manual" clears the schedule;
 *  "Custom…" accepts any 5-field cron. */
export function ScheduleControl({ cron, nextRun, onSet }: {
  cron: string | null;
  nextRun?: string | null;
  onSet: (cron: string | null) => void;
}) {
  const scheduled = !!cron;
  const onChange = (e: { target: { value: string } }) => {
    const v = e.target.value;
    if (v === "__off__") return onSet(null);
    if (v === "__custom__") {
      const entered = window.prompt("Cron schedule (min hour day-of-month month day-of-week):", cron || "0 2 * * *");
      if (entered && entered.trim()) onSet(entered.trim());
      return;
    }
    onSet(v);
  };
  // If the current cron isn't a known preset, surface it as its own selected option.
  const isPreset = !cron || SCHEDULE_PRESETS.some((p) => p.cron === cron);
  return (
    <span
      className="inline-flex items-center"
      title={nextRun ? `Next run ${new Date(nextRun).toLocaleString()}` : "Not scheduled"}
    >
      <select
        value={scheduled ? cron! : "__off__"}
        onChange={onChange}
        className={`max-w-[11rem] truncate rounded border px-1.5 py-0.5 text-xs font-medium ${
          scheduled ? "border-teal-300 bg-teal-50 text-teal-700" : "border-slate-200 text-slate-500"
        }`}
      >
        <option value="__off__">Manual (no schedule)</option>
        {!isPreset && <option value={cron!}>⏱ {cron}</option>}
        {SCHEDULE_PRESETS.map((p) => (
          <option key={p.cron} value={p.cron}>⏱ {p.label}</option>
        ))}
        <option value="__custom__">⏱ Custom…</option>
      </select>
    </span>
  );
}
