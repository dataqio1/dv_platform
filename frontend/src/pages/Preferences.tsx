import { useState } from "react";
import { usePrefs } from "../prefs";
import { Card, PageTitle } from "../components/vex";
import { Combobox } from "../components/Combobox";
import { allTimezones, browserTz, fmtDateTime, tzLabel } from "../tz";

export default function Preferences() {
  const { prefs, setPref, timezone } = usePrefs();
  const [now, setNow] = useState(() => new Date().toISOString());

  const zones = allTimezones();
  const options = zones.map((z) => ({ value: z, label: `${z} (${tzLabel(z)})` }));

  const choose = (tz: string) => {
    if (tz) setPref("timezone", tz);
    setNow(new Date().toISOString());
  };

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <PageTitle title="Preferences" subtitle="Personal settings — these apply only to you, across every device." />

      <Card className="p-5">
        <div className="mb-1 font-semibold text-navy">Timezone</div>
        <p className="mb-3 text-sm text-slate-500">
          All dates and times across the app are shown in this timezone. It doesn't change any data —
          only how timestamps are displayed for you.
        </p>
        <div className="max-w-md">
          <Combobox
            value={timezone}
            onChange={choose}
            allowCustom={false}
            options={options}
            placeholder="Search for your timezone…"
          />
        </div>
        <div className="mt-3 rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600">
          <span className="text-slate-400">Current time here:</span>{" "}
          <span className="font-medium text-navy">{fmtDateTime(now)} </span>
          <span className="text-xs text-slate-400">({tzLabel(timezone)})</span>
        </div>
        {!prefs.timezone && (
          <p className="mt-2 text-xs text-slate-400">
            Defaulting to your browser's timezone ({browserTz}). Pick one above to set it explicitly.
          </p>
        )}
      </Card>
    </div>
  );
}
