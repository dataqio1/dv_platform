// App-wide timezone for formatting timestamps. Set from each user's saved preference
// (prefs.timezone) by the PrefsProvider, so every tz-aware formatter reflects the user's choice.

let TZ = "UTC";

export function setAppTz(tz: string | undefined | null): void {
  if (tz) TZ = tz;
}
export function appTz(): string {
  return TZ;
}

export const browserTz = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
})();

/** Format an ISO timestamp as date + time in the user's timezone. */
export function fmtDateTime(iso: string | null | undefined, opts?: Intl.DateTimeFormatOptions): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(undefined, {
    timeZone: TZ,
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    ...opts,
  });
}

/** Date only, in the user's timezone. */
export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString(undefined, { timeZone: TZ, year: "numeric", month: "short", day: "numeric" });
}

/** Short timezone label for the current tz, e.g. "PST", "GMT+5:30", "UTC". */
export function tzLabel(tz: string = TZ): string {
  try {
    const parts = new Intl.DateTimeFormat(undefined, { timeZone: tz, timeZoneName: "short" }).formatToParts(new Date());
    return parts.find((p) => p.type === "timeZoneName")?.value || tz;
  } catch {
    return tz;
  }
}

/** All IANA timezones the runtime knows, with a small curated fallback. */
export function allTimezones(): string[] {
  const sv = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf;
  if (typeof sv === "function") {
    try {
      return sv("timeZone");
    } catch {
      /* fall through */
    }
  }
  return [
    "UTC", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Sao_Paulo", "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow",
    "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney",
  ];
}
