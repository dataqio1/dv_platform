import { useEffect, useMemo, useState } from "react";

/**
 * Friendly schedule picker that produces a standard 5-field cron string (UTC). Covers the
 * common cadences — Manual, Hourly, Every N hours, Daily at a time, Weekly on a day, Monthly
 * on a day — without exposing raw cron. `value` is the current cron ("" / null = Manual).
 */

type Freq = "manual" | "hourly" | "every_n_hours" | "daily" | "weekly" | "monthly";

const DOW = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

const selectCls =
  "rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal";

function parseCron(cron: string | null | undefined): {
  freq: Freq;
  hour: number;
  minute: number;
  everyN: number;
  dow: number;
  dom: number;
} {
  const def = { freq: "manual" as Freq, hour: 6, minute: 0, everyN: 4, dow: 1, dom: 1 };
  if (!cron) return def;
  const p = cron.split(/\s+/);
  if (p.length !== 5) return def;
  const [mi, ho, dom, , dw] = p;
  if (mi === "0" && ho === "*") return { ...def, freq: "hourly" };
  if (/^\d+$/.test(mi) && /^\*\/\d+$/.test(ho) && dom === "*" && dw === "*")
    return { ...def, freq: "every_n_hours", everyN: Number(ho.slice(2)), minute: Number(mi) };
  if (/^\d+$/.test(mi) && /^\d+$/.test(ho)) {
    const base = { ...def, hour: Number(ho), minute: Number(mi) };
    if (dom === "*" && dw === "*") return { ...base, freq: "daily" };
    if (dom === "*" && /^\d+$/.test(dw)) return { ...base, freq: "weekly", dow: Number(dw) % 7 };
    if (/^\d+$/.test(dom) && dw === "*") return { ...base, freq: "monthly", dom: Number(dom) };
  }
  return def;
}

function buildCron(s: ReturnType<typeof parseCron>): string | null {
  const m = Math.max(0, Math.min(59, s.minute));
  const h = Math.max(0, Math.min(23, s.hour));
  switch (s.freq) {
    case "manual":
      return null;
    case "hourly":
      return "0 * * * *";
    case "every_n_hours":
      return `0 */${Math.max(1, Math.min(23, s.everyN))} * * *`;
    case "daily":
      return `${m} ${h} * * *`;
    case "weekly":
      return `${m} ${h} * * ${s.dow}`;
    case "monthly":
      return `${m} ${h} ${Math.max(1, Math.min(28, s.dom))} * *`;
  }
}

export function SchedulePicker({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (cron: string | null) => void;
}) {
  const initial = useMemo(() => parseCron(value), [value]);
  const [s, setS] = useState(initial);

  // Re-sync when the incoming cron changes (e.g. opening the edit form).
  useEffect(() => setS(parseCron(value)), [value]);

  const patch = (p: Partial<typeof s>) => {
    const next = { ...s, ...p };
    setS(next);
    onChange(buildCron(next));
  };

  const timeInputs = (
    <span className="inline-flex items-center gap-1 text-sm text-slate-600">
      at
      <select className={selectCls} value={s.hour} onChange={(e) => patch({ hour: Number(e.target.value) })}>
        {Array.from({ length: 24 }, (_, i) => (
          <option key={i} value={i}>
            {String(i).padStart(2, "0")}
          </option>
        ))}
      </select>
      :
      <select className={selectCls} value={s.minute} onChange={(e) => patch({ minute: Number(e.target.value) })}>
        {[0, 15, 30, 45].map((mm) => (
          <option key={mm} value={mm}>
            {String(mm).padStart(2, "0")}
          </option>
        ))}
      </select>
      <span className="text-xs text-slate-400">UTC</span>
    </span>
  );

  return (
    <div className="flex flex-wrap items-center gap-2">
      <select className={selectCls} value={s.freq} onChange={(e) => patch({ freq: e.target.value as Freq })}>
        <option value="manual">Manual (no schedule)</option>
        <option value="hourly">Every hour</option>
        <option value="every_n_hours">Every N hours</option>
        <option value="daily">Daily</option>
        <option value="weekly">Weekly</option>
        <option value="monthly">Monthly</option>
      </select>

      {s.freq === "every_n_hours" && (
        <span className="inline-flex items-center gap-1 text-sm text-slate-600">
          every
          <select className={selectCls} value={s.everyN} onChange={(e) => patch({ everyN: Number(e.target.value) })}>
            {[2, 3, 4, 6, 8, 12].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          hours
        </span>
      )}
      {s.freq === "daily" && timeInputs}
      {s.freq === "weekly" && (
        <>
          <span className="inline-flex items-center gap-1 text-sm text-slate-600">
            on
            <select className={selectCls} value={s.dow} onChange={(e) => patch({ dow: Number(e.target.value) })}>
              {DOW.map((d, i) => (
                <option key={d} value={i}>
                  {d}
                </option>
              ))}
            </select>
          </span>
          {timeInputs}
        </>
      )}
      {s.freq === "monthly" && (
        <>
          <span className="inline-flex items-center gap-1 text-sm text-slate-600">
            on the
            <select className={selectCls} value={s.dom} onChange={(e) => patch({ dom: Number(e.target.value) })}>
              {Array.from({ length: 28 }, (_, i) => i + 1).map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </span>
          {timeInputs}
        </>
      )}
    </div>
  );
}
