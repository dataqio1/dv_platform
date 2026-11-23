import { useEffect, type ReactNode } from "react";
import type { Health } from "../types";

const HEALTH_STYLES: Record<Health, string> = {
  healthy: "bg-emerald-100 text-emerald-700 ring-emerald-200",
  warning: "bg-amber-100 text-amber-700 ring-amber-200",
  critical: "bg-rose-100 text-rose-700 ring-rose-200",
  unknown: "bg-slate-100 text-slate-500 ring-slate-200",
};

const HEALTH_LABEL: Record<Health, string> = {
  healthy: "Healthy",
  warning: "Warning",
  critical: "Critical",
  unknown: "Unknown",
};

/** Overall badge that refuses to look healthy when a pillar was never measured. */
export function honestyBadge(
  health: Health,
  score: number,
  pillarStatuses: Array<Health | string | undefined>,
): { status: Health; label: string } {
  const statuses = pillarStatuses.filter((s): s is Health => Boolean(s));
  const measured = statuses.filter((s) => s !== "unknown").length;
  const incomplete = statuses.length > 0 && measured < statuses.length;
  if (incomplete && health !== "warning" && health !== "critical") {
    return { status: "unknown", label: `incomplete · ${measured}/${statuses.length}` };
  }
  return { status: health, label: `${health} · ${score}` };
}

export function HealthBadge({ status, label, title }: { status: Health; label?: string; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold ring-1 ring-inset ${HEALTH_STYLES[status]}`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
      {label ?? HEALTH_LABEL[status]}
    </span>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  // shadow-card matches the Figma/Observability design system (vex.tsx).
  return (
    <div className={`rounded-xl border border-slate-200 bg-white shadow-card ${className}`}>{children}</div>
  );
}

type BtnProps = {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "ghost" | "navy" | "danger";
  size?: "sm" | "md";
  disabled?: boolean;
  type?: "button" | "submit";
  className?: string;
};

export function Button({
  children,
  onClick,
  variant = "primary",
  size = "md",
  disabled,
  type = "button",
  className = "",
}: BtnProps) {
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

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <div className="mb-1 text-sm font-medium text-slate-700">{label}</div>
      {children}
      {hint && <div className="mt-1 text-xs text-slate-400">{hint}</div>}
    </label>
  );
}

export function TextInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20 ${props.className ?? ""}`}
    />
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-slate-400">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-teal" />
      {label ?? "Loading…"}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-300 bg-white/50 p-10 text-center">
      <div className="text-sm font-medium text-slate-600">{title}</div>
      {hint && <div className="mt-1 text-xs text-slate-400">{hint}</div>}
    </div>
  );
}

/** Centered modal dialog — dims the page, closes on backdrop click or Escape. */
export function Modal({ open, onClose, title, children, wide }: {
  open: boolean; onClose: () => void; title?: ReactNode; children: ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-slate-900/40" onClick={onClose} />
      <div className={`relative z-10 flex max-h-[85vh] w-full flex-col overflow-hidden rounded-2xl bg-white shadow-xl ${wide ? "max-w-4xl" : "max-w-2xl"}`}>
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3">
          <div className="min-w-0 truncate text-sm font-semibold text-navy">{title}</div>
          <button onClick={onClose} aria-label="Close" className="ml-3 shrink-0 text-slate-400 hover:text-slate-600">✕</button>
        </div>
        <div className="overflow-auto p-5">{children}</div>
      </div>
    </div>
  );
}
