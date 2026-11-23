const nf = new Intl.NumberFormat(undefined);
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });

export const formatNumber = (n: number | null | undefined): string => (n == null ? "—" : nf.format(Math.round(n)));
export const formatCompact = (n: number | null | undefined): string => (n == null ? "—" : compact.format(n));

const EXP: Record<string, number> = { JPY: 0, KRW: 0, VND: 0, BHD: 3, KWD: 3, OMR: 3, JOD: 3 };

export function formatMoney(
  minor: number | null | undefined,
  currency = "USD",
  options?: { compact?: boolean },
): string {
  if (minor == null) return "—";
  const exp = EXP[currency] ?? 2;
  const value = minor / 10 ** exp;
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    notation: options?.compact ? "compact" : "standard",
    maximumFractionDigits: options?.compact ? 1 : value % 1 === 0 ? 0 : exp,
  }).format(value);
}

export function formatPercent(rate: number | null | undefined, digits = 1): string {
  if (rate == null) return "—";
  return `${(rate * 100).toFixed(digits)}%`;
}

const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31536000],
  ["month", 2592000],
  ["week", 604800],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
];

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const seconds = (new Date(iso).getTime() - Date.now()) / 1000;
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return rtf.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

export function formatDate(
  iso: string | null | undefined,
  opts: Intl.DateTimeFormatOptions = { day: "numeric", month: "short" },
): string {
  if (!iso) return "—";
  return new Intl.DateTimeFormat(undefined, opts).format(new Date(iso));
}

export function formatDateTime(iso: string | null | undefined): string {
  return formatDate(iso, { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}

export const titleCase = (s: string): string => s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
export const sentence = (s: string): string => {
  const t = s.replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
};
