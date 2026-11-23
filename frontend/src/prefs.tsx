import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "./api";
import { browserTz, setAppTz } from "./tz";

/**
 * Per-user UI preferences, persisted server-side (follows the user across devices).
 * The store is a single JSON blob; add a key here and it's saved automatically.
 * Known keys: lastSection, lastProjectId.
 */
type Prefs = Record<string, unknown>;

export type RecentItem = { kind: string; id: string; name: string; path: string };
const MAX_RECENTS = 8;

const PrefsContext = createContext<{
  prefs: Prefs;
  loaded: boolean;
  setPref: (key: string, value: unknown) => void;
  permissions: string[];
  can: (permission: string) => boolean;
  role: string;
  groupId: string;
  meLoaded: boolean;
  timezone: string;
  recents: RecentItem[];
  recordRecent: (item: RecentItem) => void;
}>({
  prefs: {}, loaded: false, setPref: () => {}, permissions: [], can: () => false,
  role: "", groupId: "", meLoaded: false, timezone: "UTC",
  recents: [], recordRecent: () => {},
});

export function PrefsProvider({ children }: { children: ReactNode }) {
  const [prefs, setPrefs] = useState<Prefs>({});
  const [loaded, setLoaded] = useState(false);
  const [permissions, setPermissions] = useState<string[]>([]);
  const [role, setRole] = useState("");
  const [groupId, setGroupId] = useState("");
  const [meLoaded, setMeLoaded] = useState(false);

  useEffect(() => {
    api.getPreferences()
      .then((r) => setPrefs(r.data || {}))
      .catch(() => undefined)
      .finally(() => setLoaded(true));
    // Capabilities + active tenant for the signed-in user — gates admin-only UI.
    api.me()
      .then((r) => {
        setPermissions(r.data.permissions || []);
        setRole(r.data.role || "");
        setGroupId(r.data.group_id || "");
      })
      .catch(() => setPermissions([]))
      .finally(() => setMeLoaded(true));
  }, []);

  // Optimistic local update + fire-and-forget persistence (never block the UI on it).
  const setPref = (key: string, value: unknown) => {
    setPrefs((prev) => {
      if (prev[key] === value) return prev;
      api.savePreferences({ [key]: value }).catch(() => undefined);
      return { ...prev, [key]: value };
    });
  };

  const can = (permission: string) => permissions.includes(permission);

  // The user's timezone preference (falls back to the browser's) drives every tz-aware
  // formatter app-wide via the shared tz module. Set it synchronously during render so
  // children (which render after this provider) format with the correct tz on the SAME
  // render that changes it — not one render late. The effect is a belt-and-suspenders sync.
  const timezone = (prefs.timezone as string) || browserTz;
  setAppTz(timezone);
  useEffect(() => {
    setAppTz(timezone);
  }, [timezone]);

  const recents = (prefs.recents as RecentItem[] | undefined) ?? [];
  // Push an item to the front of the recently-viewed list (deduped by path, capped).
  const recordRecent = (item: RecentItem) => {
    setPrefs((prev) => {
      const prior = (prev.recents as RecentItem[] | undefined) ?? [];
      if (prior[0]?.path === item.path && prior[0]?.name === item.name) return prev; // no-op
      const next = [item, ...prior.filter((r) => r.path !== item.path)].slice(0, MAX_RECENTS);
      api.savePreferences({ recents: next }).catch(() => undefined);
      return { ...prev, recents: next };
    });
  };

  return (
    <PrefsContext.Provider
      value={{ prefs, loaded, setPref, permissions, can, role, groupId, meLoaded, timezone, recents, recordRecent }}
    >
      {children}
    </PrefsContext.Provider>
  );
}

export const usePrefs = () => useContext(PrefsContext);
