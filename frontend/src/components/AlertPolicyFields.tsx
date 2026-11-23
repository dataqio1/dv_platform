import type { AlertPolicy, FreshnessSource } from "../types";
import { DEFAULT_ALERT_POLICY } from "../types";
import { Field } from "./ui";

/** Per-monitor alert policy editor — freshness arrival window + per-pillar severity thresholds.
 *
 * Shared by Create Monitor and Monitor → Edit so the two stay identical (one source of truth).
 * `isFile` tailors the freshness wording to file arrival and recommends the file-arrival source. */
export function AlertPolicyFields({
  value,
  onChange,
  isFile = false,
}: {
  value: AlertPolicy;
  onChange: (next: AlertPolicy) => void;
  isFile?: boolean;
}) {
  const p = value ?? DEFAULT_ALERT_POLICY;
  const f = p.freshness;

  const setFresh = (patch: Partial<AlertPolicy["freshness"]>) =>
    onChange({ ...p, freshness: { ...f, ...patch } });
  const setQuality = (patch: Partial<AlertPolicy["quality"]>) =>
    onChange({ ...p, quality: { ...p.quality, ...patch } });
  const setVolume = (patch: Partial<AlertPolicy["volume"]>) =>
    onChange({ ...p, volume: { ...p.volume, ...patch } });

  const num = (v: string): number => Math.max(0, Number(v) || 0);
  const numOrNull = (v: string): number | null => (v.trim() === "" ? null : Math.max(0, Number(v) || 0));

  return (
    <div className="space-y-5">
      {/* ---- Freshness: when data should arrive ---- */}
      <div>
        <div className="mb-2 text-sm font-semibold text-navy">
          Freshness — when {isFile ? "files" : "data"} should arrive
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field
            label="Measured from"
            hint={
              isFile
                ? "Files: the newest file's arrival (last-modified) time."
                : "Watermark uses MAX(watermark column); file arrival uses the file's modified time."
            }
          >
            <select
              value={f.source}
              onChange={(e) => setFresh({ source: e.target.value as FreshnessSource })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal"
            >
              <option value="auto">Automatic</option>
              <option value="watermark">Watermark column</option>
              <option value="file_arrival">File arrival time</option>
            </select>
          </Field>
          <Field label="Expect new data every (hours)" hint="How often fresh data is expected.">
            <input
              type="number"
              min={1}
              value={f.cadence_hours}
              onChange={(e) => setFresh({ cadence_hours: num(e.target.value) || 1 })}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal"
            />
          </Field>
          <Field label="Warn if older than (hours)" hint="Raises a warning alert.">
            <input
              type="number"
              min={0}
              value={f.warn_after_hours}
              onChange={(e) => setFresh({ warn_after_hours: num(e.target.value) })}
              className="w-full rounded-lg border border-amber-300 px-3 py-2 text-sm outline-none focus:border-amber-500"
            />
          </Field>
          <Field label="Severe if older than (hours)" hint="Escalates to a severe (critical) alert.">
            <input
              type="number"
              min={0}
              value={f.severe_after_hours}
              onChange={(e) => setFresh({ severe_after_hours: num(e.target.value) })}
              className="w-full rounded-lg border border-rose-300 px-3 py-2 text-sm outline-none focus:border-rose-500"
            />
          </Field>
        </div>
      </div>

      {/* ---- Quality floor ---- */}
      <div>
        <div className="mb-2 text-sm font-semibold text-navy">Quality score</div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Warn below" hint="Table quality score 0–100.">
            <input
              type="number"
              min={0}
              max={100}
              value={p.quality.warn_below}
              onChange={(e) => setQuality({ warn_below: Math.min(100, num(e.target.value)) })}
              className="w-full rounded-lg border border-amber-300 px-3 py-2 text-sm outline-none focus:border-amber-500"
            />
          </Field>
          <Field label="Severe below">
            <input
              type="number"
              min={0}
              max={100}
              value={p.quality.severe_below}
              onChange={(e) => setQuality({ severe_below: Math.min(100, num(e.target.value)) })}
              className="w-full rounded-lg border border-rose-300 px-3 py-2 text-sm outline-none focus:border-rose-500"
            />
          </Field>
        </div>
      </div>

      {/* ---- Volume (optional % override) ---- */}
      <div>
        <div className="mb-2 text-sm font-semibold text-navy">
          Volume change <span className="font-normal text-slate-400">— optional</span>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Warn at % deviation" hint="Leave blank to use automatic anomaly detection.">
            <input
              type="number"
              min={0}
              placeholder="auto"
              value={p.volume.warn_pct ?? ""}
              onChange={(e) => setVolume({ warn_pct: numOrNull(e.target.value) })}
              className="w-full rounded-lg border border-amber-300 px-3 py-2 text-sm outline-none focus:border-amber-500"
            />
          </Field>
          <Field label="Severe at % deviation">
            <input
              type="number"
              min={0}
              placeholder="auto"
              value={p.volume.severe_pct ?? ""}
              onChange={(e) => setVolume({ severe_pct: numOrNull(e.target.value) })}
              className="w-full rounded-lg border border-rose-300 px-3 py-2 text-sm outline-none focus:border-rose-500"
            />
          </Field>
        </div>
      </div>

      {f.severe_after_hours < f.warn_after_hours && (
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
          Severe threshold is below the warn threshold — severe alerts would fire before warnings.
        </p>
      )}
    </div>
  );
}
