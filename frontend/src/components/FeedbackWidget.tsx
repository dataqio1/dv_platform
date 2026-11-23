import { useState } from "react";
import { useLocation } from "react-router-dom";
import { api } from "../api";
import { Button, Field, Modal } from "./ui";

const CATEGORIES: { key: string; label: string; hint: string }[] = [
  { key: "idea", label: "💡 Idea", hint: "A suggestion or improvement" },
  { key: "issue", label: "🐞 Issue", hint: "Something’s broken or confusing" },
  { key: "praise", label: "🎉 Praise", hint: "Something you like" },
  { key: "other", label: "💬 Other", hint: "Anything else" },
];

/** A floating "Feedback" button present on every page. Opens a short form and captures the
 *  current page + browser context so admins can act on it without a repro. */
export default function FeedbackWidget() {
  const loc = useLocation();
  const [open, setOpen] = useState(false);
  const [category, setCategory] = useState("idea");
  const [message, setMessage] = useState("");
  const [rating, setRating] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const reset = () => {
    setCategory("idea"); setMessage(""); setRating(null); setErr(null); setDone(false);
  };
  const close = () => { setOpen(false); setTimeout(reset, 200); };

  const submit = async () => {
    if (!message.trim()) { setErr("Please add a message."); return; }
    setBusy(true); setErr(null);
    try {
      await api.submitFeedback({
        message: message.trim(),
        category,
        rating,
        page: loc.pathname,
        url: window.location.href,
        user_agent: navigator.userAgent,
        context: {
          viewport: `${window.innerWidth}x${window.innerHeight}`,
          referrer: document.referrer || null,
          language: navigator.language,
          submitted_at: new Date().toISOString(),
        },
      });
      setDone(true);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="fixed bottom-5 right-5 z-40 inline-flex items-center gap-1.5 rounded-full bg-navy px-4 py-2.5 text-sm font-semibold text-white shadow-lg transition hover:bg-navy-600"
        title="Send feedback about this page"
      >
        💬 Feedback
      </button>

      <Modal open={open} onClose={close} title="Send feedback">
        {done ? (
          <div className="space-y-4 text-center">
            <div className="text-3xl">🙏</div>
            <p className="text-sm text-slate-600">Thanks! Your feedback was sent to the team.</p>
            <Button onClick={close}>Close</Button>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-500">
              About this page: <span className="font-mono text-slate-600">{loc.pathname}</span>
            </div>

            <Field label="Type">
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {CATEGORIES.map((c) => (
                  <button
                    key={c.key}
                    onClick={() => setCategory(c.key)}
                    title={c.hint}
                    className={`rounded-lg border px-2 py-2 text-xs font-medium transition ${
                      category === c.key
                        ? "border-teal bg-teal/10 text-teal-700"
                        : "border-slate-300 text-slate-600 hover:bg-slate-50"
                    }`}
                  >
                    {c.label}
                  </button>
                ))}
              </div>
            </Field>

            <Field label="Your feedback">
              <textarea
                autoFocus
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                rows={5}
                placeholder="What’s on your mind about this page?"
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-teal focus:ring-2 focus:ring-teal/20"
              />
            </Field>

            <Field label="How’s this page working for you? (optional)">
              <div className="flex gap-1">
                {[1, 2, 3, 4, 5].map((n) => (
                  <button
                    key={n}
                    onClick={() => setRating(rating === n ? null : n)}
                    className={`text-2xl leading-none transition ${
                      rating && n <= rating ? "opacity-100" : "opacity-30 hover:opacity-70"
                    }`}
                    title={`${n} star${n > 1 ? "s" : ""}`}
                  >
                    ⭐
                  </button>
                ))}
              </div>
            </Field>

            {err && <div className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">{err}</div>}

            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
              <Button onClick={submit} disabled={busy}>{busy ? "Sending…" : "Send feedback"}</Button>
            </div>
            <p className="text-[11px] text-slate-400">
              We also capture the page URL and your browser/screen size to help us reproduce issues.
            </p>
          </div>
        )}
      </Modal>
    </>
  );
}
