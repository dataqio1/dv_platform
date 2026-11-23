import type { ReactNode } from "react";

/* ------------------------------------------------------------------ *
 * Data Validator design-system primitives (data_observability Figma).
 * Palette: brand teal #00a8c8, ink/navy #182050, page #f6f8fa.
 * ------------------------------------------------------------------ */

// ---- status tokens (monitor + incident) ----
export type StatusTone = "healthy" | "attention" | "learning" | "paused" | "error" | "neutral";

const STATUS_STYLE: Record<StatusTone, string> = {
  healthy: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  attention: "bg-amber-50 text-amber-700 ring-amber-200",
  learning: "bg-violet-50 text-violet-700 ring-violet-200",
  paused: "bg-slate-100 text-slate-500 ring-slate-200",
  error: "bg-rose-50 text-rose-700 ring-rose-200",
  neutral: "bg-slate-100 text-slate-600 ring-slate-200",
};

const DOT: Record<StatusTone, string> = {
  healthy: "bg-emerald-500",
  attention: "bg-amber-500",
  learning: "bg-violet-500",
  paused: "bg-slate-400",
  error: "bg-rose-500",
  neutral: "bg-slate-400",
};

export function Pill({ tone, children, dot = true }: { tone: StatusTone; children: ReactNode; dot?: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLE[tone]}`}
    >
      {dot && <span className={`h-1.5 w-1.5 rounded-full ${DOT[tone]}`} />}
      {children}
    </span>
  );
}

// ---- card ----
export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-slate-200 bg-white shadow-card ${className}`}>{children}</div>
  );
}

// ---- KPI metric card (dashboard top row) ----
export function KpiCard({
  label,
  value,
  hint,
  tone = "neutral",
  active = false,
  onClick,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  tone?: StatusTone;
  active?: boolean;
  onClick?: () => void;
}) {
  const valueColor: Record<StatusTone, string> = {
    healthy: "text-emerald-600",
    attention: "text-amber-600",
    learning: "text-violet-600",
    paused: "text-slate-500",
    error: "text-rose-600",
    neutral: "text-navy",
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className={`block rounded-xl border bg-white px-4 py-3.5 text-left shadow-card transition ${
        active ? "border-teal ring-2 ring-teal/20" : "border-slate-200 hover:border-slate-300"
      } ${onClick ? "cursor-pointer" : "cursor-default"}`}
    >
      <div className="text-xs font-medium text-slate-500">{label}</div>
      <div className={`mt-1 text-3xl font-bold tabular-nums ${valueColor[tone]}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-slate-400">{hint}</div>}
    </button>
  );
}

// ---- filter chip (count badge + active state) ----
export function Chip({
  label,
  count,
  active,
  onClick,
  tone = "neutral",
}: {
  label: string;
  count?: number;
  active?: boolean;
  onClick?: () => void;
  tone?: StatusTone;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset transition ${
        active
          ? "bg-navy text-white ring-navy"
          : "bg-white text-slate-600 ring-slate-200 hover:bg-slate-50"
      }`}
    >
      {!active && tone !== "neutral" && <span className={`h-1.5 w-1.5 rounded-full ${DOT[tone]}`} />}
      {label}
      {count !== undefined && (
        <span className={`tabular-nums ${active ? "text-white/80" : "text-slate-400"}`}>{count}</span>
      )}
    </button>
  );
}

// ---- tabs ----
export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { key: string; label: string; count?: number }[];
  active: string;
  onChange: (key: string) => void;
}) {
  return (
    <div className="flex items-center gap-6 border-b border-slate-200">
      {tabs.map((t) => {
        const on = t.key === active;
        return (
          <button
            key={t.key}
            onClick={() => onChange(t.key)}
            className={`-mb-px border-b-2 px-0.5 pb-2.5 text-sm font-medium transition ${
              on ? "border-teal text-navy" : "border-transparent text-slate-500 hover:text-slate-700"
            }`}
          >
            {t.label}
            {t.count !== undefined && <span className="ml-1.5 text-xs text-slate-400">· {t.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

// ---- wizard stepper ----
export function Stepper({ steps, current }: { steps: string[]; current: number }) {
  return (
    <div className="flex items-center">
      {steps.map((label, i) => {
        const done = i < current;
        const active = i === current;
        return (
          <div key={label} className="flex flex-1 items-center last:flex-none">
            <div className="flex items-center gap-2">
              <span
                className={`grid h-6 w-6 place-items-center rounded-full text-xs font-semibold ring-1 ${
                  done
                    ? "bg-teal text-white ring-teal"
                    : active
                      ? "border-2 border-teal bg-white text-teal ring-transparent"
                      : "bg-white text-slate-400 ring-slate-300"
                }`}
              >
                {done ? "✓" : i + 1}
              </span>
              <span className={`text-sm font-medium ${active ? "text-navy" : done ? "text-slate-600" : "text-slate-400"}`}>
                {label}
              </span>
            </div>
            {i < steps.length - 1 && <div className="mx-4 h-px flex-1 bg-slate-200" />}
          </div>
        );
      })}
    </div>
  );
}

// ---- styled select ----
export function Select(
  props: React.SelectHTMLAttributes<HTMLSelectElement> & { children: ReactNode },
) {
  const { children, className = "", ...rest } = props;
  return (
    <select
      {...rest}
      className={`w-full appearance-none rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20 ${className}`}
    >
      {children}
    </select>
  );
}

// ---- 3-segment threshold bar (healthy/warn/error) ----
export function ThresholdBar({ labels }: { labels: [string, string, string] }) {
  return (
    <div>
      <div className="flex h-1.5 overflow-hidden rounded-full">
        <div className="flex-1 bg-teal-200" />
        <div className="flex-1 bg-amber-200" />
        <div className="w-10 bg-rose-200" />
      </div>
      <div className="mt-1 flex justify-between text-[11px]">
        <span className="text-teal-700">{labels[0]}</span>
        <span className="text-amber-600">{labels[1]}</span>
        <span className="text-rose-600">{labels[2]}</span>
      </div>
    </div>
  );
}

// ---- page title block ----
export function PageTitle({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <h1 className="text-2xl font-bold text-navy">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
      </div>
      {actions && <div className="shrink-0">{actions}</div>}
    </div>
  );
}

// ---- button ----
export function Button({
  children,
  onClick,
  variant = "primary",
  size = "md",
  disabled,
  type = "button",
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "ghost" | "navy" | "danger";
  size?: "sm" | "md";
  disabled?: boolean;
  type?: "button" | "submit";
  className?: string;
}) {
  const styles = {
    primary: "bg-teal text-white hover:bg-teal-600 disabled:bg-teal/40",
    navy: "bg-navy text-white hover:bg-navy-600 disabled:opacity-40",
    ghost: "bg-white text-slate-700 ring-1 ring-inset ring-slate-300 hover:bg-slate-50",
    danger: "bg-rose-600 text-white hover:bg-rose-700 disabled:opacity-40",
  }[variant];
  const sz = size === "sm" ? "px-3 py-1.5 text-xs" : "px-4 py-2 text-sm";
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center justify-center gap-1.5 rounded-lg font-semibold transition ${sz} ${styles} ${className}`}
    >
      {children}
    </button>
  );
}

// ---- lightweight inline icon set ----
type IconName =
  | "projects" | "validation" | "connections" | "observability" | "admin" | "help"
  | "bell" | "gear" | "chevron" | "search" | "refresh" | "plus" | "dot" | "trash";

const PATHS: Record<IconName, ReactNode> = {
  projects: <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" />,
  validation: <path d="M9 12l2 2 4-4M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18Z" />,
  connections: <path d="M8 7h8M8 12h8M8 17h5M5 7h.01M5 12h.01M5 17h.01" />,
  observability: <path d="M3 12h4l2 6 4-14 2 8h6" />,
  admin: <path d="M12 3l7 3v5c0 4-3 7-7 9-4-2-7-5-7-9V6l7-3Z" />,
  help: <path d="M9.1 9a3 3 0 1 1 4.6 2.5c-.9.6-1.7 1.2-1.7 2.5M12 17h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />,
  bell: <path d="M6 8a6 6 0 1 1 12 0c0 7 3 7 3 7H3s3 0 3-7M10.3 21a2 2 0 0 0 3.4 0" />,
  gear: <path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 6.6 19.4l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 4.6 14H4.5a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.1-2.7l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.6 1.6 0 0 0 11 4.6V4.5a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 2.7 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8v.1a1.6 1.6 0 0 0 1.4 1h.1a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.4 1Z" />,
  chevron: <path d="M6 9l6 6 6-6" />,
  search: <path d="M21 21l-4.3-4.3M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Z" />,
  refresh: <path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5" />,
  plus: <path d="M12 5v14M5 12h14" />,
  dot: <circle cx="12" cy="12" r="4" />,
  trash: <path d="M4 7h16M10 11v6M14 11v6M5 7l1 13a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-13M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3" />,
};

export function Icon({ name, className = "h-4 w-4" }: { name: IconName; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" className={className}>
      {PATHS[name]}
    </svg>
  );
}

// ---- monitor status → tone + label ----
export function monitorTone(status: string): StatusTone {
  if (status === "healthy") return "healthy";
  if (status === "attention" || status === "warning" || status === "needs_attention") return "attention";
  if (status === "learning") return "learning";
  if (status === "paused") return "paused";
  if (status === "error" || status === "critical") return "error";
  return "neutral";
}
