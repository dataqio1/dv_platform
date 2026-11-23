import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "./api";
import { usePrefs } from "./prefs";
import type { Project } from "./types";

/**
 * Shared project context: the list of (active, non-archived) projects and the user's
 * "active project". Data Validation is scoped to the active project; switching it changes
 * which test cases you see. Persisted per-user via the `activeProjectId` preference.
 */
const Ctx = createContext<{
  projects: Project[];
  active: string;
  setActive: (id: string) => void;
  refresh: () => Promise<void>;
  loading: boolean;
}>({ projects: [], active: "", setActive: () => {}, refresh: async () => {}, loading: true });

export function ProjectsProvider({ children }: { children: ReactNode }) {
  const { prefs, setPref } = usePrefs();
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const r = await api.listProjects();
      setProjects(r.data);
    } catch {
      setProjects([]);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  const active = typeof prefs.activeProjectId === "string" ? prefs.activeProjectId : "";
  const setActive = (id: string) => setPref("activeProjectId", id);

  // Keep the active project valid: if it's unset or points at a gone/archived project,
  // fall back to the first available.
  useEffect(() => {
    if (projects.length && !projects.some((p) => p.id === active)) setActive(projects[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projects]);

  return (
    <Ctx.Provider value={{ projects, active, setActive, refresh, loading }}>{children}</Ctx.Provider>
  );
}

export const useProjects = () => useContext(Ctx);
