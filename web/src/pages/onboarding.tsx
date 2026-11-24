import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { ArrowRight, Check, Globe, Sparkles } from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";
import { Button, Field, Input, Select, Switch, Textarea } from "@/components/ui";
import { api, errorMessage } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { Business } from "@/lib/types";

const MODELS = [
  { value: "media", label: "Media & newsletters", hint: "Audience, subscribers, sponsors" },
  { value: "service", label: "Services & agency", hint: "Leads, calls, clients" },
  { value: "software", label: "Software & apps", hint: "Signups, trials, paid users" },
  { value: "ecommerce", label: "Online store", hint: "Shoppers, orders, repeat buyers" },
  { value: "creator", label: "Creator & coaching", hint: "Followers, community, offers" },
  { value: "other", label: "Something else", hint: "We'll adapt" },
];

const CURRENCIES = ["USD", "INR", "EUR", "GBP", "AUD", "CAD", "SGD", "AED", "JPY"];

function guessCurrency(): string {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (tz.startsWith("Asia/Kolkata") || tz.startsWith("Asia/Calcutta")) return "INR";
  if (tz.startsWith("Europe/London")) return "GBP";
  if (tz.startsWith("Europe/")) return "EUR";
  if (tz.startsWith("Australia/")) return "AUD";
  if (tz.startsWith("Asia/Singapore")) return "SGD";
  if (tz.startsWith("Asia/Dubai")) return "AED";
  if (tz.startsWith("Asia/Tokyo")) return "JPY";
  return "USD";
}

function Steps({ step }: { step: number }) {
  const labels = ["Your business", "Brand book", "Ready"];
  return (
    <ol className="mb-8 flex items-center justify-center gap-2 text-[13px]">
      {labels.map((label, i) => (
        <li key={label} className="flex items-center gap-2">
          <span
            className={cn(
              "flex size-6 items-center justify-center rounded-full text-[12px] font-semibold",
              i < step ? "bg-good text-white" : i === step ? "bg-accent-fill text-white" : "bg-fill text-muted",
            )}
          >
            {i < step ? <Check className="size-3.5" /> : i + 1}
          </span>
          <span className={i === step ? "font-medium text-text" : "text-muted"}>{label}</span>
          {i < labels.length - 1 ? <span className="mx-1 h-px w-8 bg-hairline" /> : null}
        </li>
      ))}
    </ol>
  );
}

export function Onboarding() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [step, setStep] = useState(0);
  const [business, setBusiness] = useState<Business | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const timezone = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone, []);
  const [form, setForm] = useState({
    name: "",
    website: "",
    business_model: "media",
    description: "",
    industry: "",
    currency: guessCurrency(),
    country: (navigator.language.split("-")[1] ?? "").toUpperCase() || "US",
    practice_mode: true,
  });
  const [brand, setBrand] = useState({ website: "", notes: "" });

  const create = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const b = await api.post<Business>("/api/v1/businesses", {
        ...form,
        website: form.website || null,
        industry: form.industry || null,
        description: form.description || null,
        timezone,
      });
      setBusiness(b);
      setBrand({ website: form.website, notes: form.description });
      await qc.invalidateQueries({ queryKey: ["/api/v1/auth/me"] });
      setStep(1);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const draftBrand = async (e: FormEvent) => {
    e.preventDefault();
    if (!business) return;
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/v1/b/${business.id}/gtm/brand-book/draft`, {
        website: brand.website || null,
        notes: brand.notes || null,
      });
      setStep(2);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const set =
    (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
      setForm({ ...form, [k]: e.target.value });

  return (
    <div className="min-h-dvh bg-bg px-4 py-12">
      <div className="mx-auto max-w-[560px]">
        <Steps step={step} />
        <div
          className="rounded-[22px] bg-surface p-7 shadow-[var(--shadow-md)] ring-1 ring-hairline sm:p-9 animate-in"
          key={step}
        >
          {step === 0 ? (
            <form onSubmit={create} className="space-y-5">
              <div>
                <h1 className="text-[26px] font-bold tracking-[-0.03em]">Tell us about your business</h1>
                <p className="mt-1 text-[14px] text-muted">Two minutes. Everything can be changed later.</p>
              </div>
              <Field label="Business name">
                {(id) => <Input id={id} required autoFocus value={form.name} onChange={set("name")} />}
              </Field>
              <div>
                <span className="mb-1.5 block text-[13px] font-medium text-text-2">What kind of business?</span>
                <div className="grid grid-cols-2 gap-2">
                  {MODELS.map((m) => (
                    <button
                      key={m.value}
                      type="button"
                      onClick={() => setForm({ ...form, business_model: m.value })}
                      className={cn(
                        "rounded-[12px] px-3.5 py-3 text-left ring-1 transition-all",
                        form.business_model === m.value
                          ? "bg-accent-soft ring-accent"
                          : "ring-hairline hover:bg-fill-2",
                      )}
                    >
                      <span className="block text-[14px] font-medium">{m.label}</span>
                      <span className="block text-[12px] text-muted">{m.hint}</span>
                    </button>
                  ))}
                </div>
              </div>
              <Field label="Website" hint="Optional. We read it to draft your brand book.">
                {(id) => (
                  <Input id={id} type="url" placeholder="https://" value={form.website} onChange={set("website")} />
                )}
              </Field>
              <Field label="In one or two sentences, what do you offer and to whom?">
                {(id) => <Textarea id={id} rows={3} value={form.description} onChange={set("description")} />}
              </Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Currency">
                  {(id) => (
                    <Select id={id} value={form.currency} onChange={set("currency")}>
                      {CURRENCIES.map((c) => (
                        <option key={c}>{c}</option>
                      ))}
                    </Select>
                  )}
                </Field>
                <Field label="Country code">
                  {(id) => <Input id={id} maxLength={2} value={form.country} onChange={set("country")} />}
                </Field>
              </div>
              <div className="flex items-center justify-between gap-4 rounded-[14px] bg-fill-2 px-4 py-3">
                <div>
                  <div className="text-[14px] font-medium">Start in practice mode</div>
                  <div className="text-[12px] text-muted">
                    Simulated platforms, so you can see everything work before going live.
                  </div>
                </div>
                <Switch
                  label="Practice mode"
                  checked={form.practice_mode}
                  onCheckedChange={(v) => setForm({ ...form, practice_mode: v })}
                />
              </div>
              {error ? (
                <p role="alert" className="text-[13px] text-critical">
                  {error}
                </p>
              ) : null}
              <Button type="submit" variant="primary" size="lg" className="w-full justify-center" loading={busy}>
                Continue <ArrowRight className="size-4" />
              </Button>
            </form>
          ) : step === 1 ? (
            <form onSubmit={draftBrand} className="space-y-5">
              <div>
                <div className="mb-3 flex size-11 items-center justify-center rounded-[13px] bg-accent-soft text-accent">
                  <Sparkles className="size-5" />
                </div>
                <h1 className="text-[26px] font-bold tracking-[-0.03em]">Draft your brand book</h1>
                <p className="mt-1 text-[14px] text-muted">
                  Your voice, audience and positioning. Every post is checked against it. You'll review and approve it
                  before anything goes out.
                </p>
              </div>
              <Field label="Website to read">
                {(id) => (
                  <div className="relative">
                    <Globe className="absolute top-3 left-3 size-4 text-faint" />
                    <Input
                      id={id}
                      type="url"
                      className="pl-9"
                      placeholder="https://"
                      value={brand.website}
                      onChange={(e) => setBrand({ ...brand, website: e.target.value })}
                    />
                  </div>
                )}
              </Field>
              <Field
                label="Anything we should know?"
                hint="Tone, words to avoid, who you serve, what makes you different."
              >
                {(id) => (
                  <Textarea
                    id={id}
                    rows={4}
                    value={brand.notes}
                    onChange={(e) => setBrand({ ...brand, notes: e.target.value })}
                  />
                )}
              </Field>
              {error ? (
                <p role="alert" className="text-[13px] text-critical">
                  {error}
                </p>
              ) : null}
              <div className="flex gap-2">
                <Button size="lg" onClick={() => setStep(2)}>
                  Skip for now
                </Button>
                <Button type="submit" variant="primary" size="lg" className="flex-1 justify-center" loading={busy}>
                  {busy ? "Reading and drafting…" : "Draft it"}
                </Button>
              </div>
            </form>
          ) : (
            <div className="space-y-5 text-center">
              <div className="mx-auto flex size-14 items-center justify-center rounded-full bg-[var(--good-soft)] text-good">
                <Check className="size-7" />
              </div>
              <h1 className="text-[26px] font-bold tracking-[-0.03em]">{business?.name} is set up</h1>
              <p className="text-[14px] text-muted">
                Next: approve your brand book, connect channels, and set one main goal. The Today screen walks you
                through it.
              </p>
              <Button
                variant="primary"
                size="lg"
                className="w-full justify-center"
                onClick={() => business && navigate({ to: "/b/$bid/today", params: { bid: business.id } })}
              >
                Open Today <ArrowRight className="size-4" />
              </Button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
