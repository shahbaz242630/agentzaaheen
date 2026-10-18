// Turning API values into words. Money arrives as integer minor units and times as UTC;
// both are formatted only here, in the viewer's own locale and time zone.
import type { Freshness, Value } from "./api";

export function minorDigits(currency: string): number {
  return new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions().maximumFractionDigits ?? 2;
}

export function formatValue(value: Value, locale?: string): string {
  switch (value.kind) {
    case "money": {
      // Whole amounts drop the cents ($1,240); others keep all of them ($58.50).
      const digits = minorDigits(value.currency);
      const whole = value.minor % 10 ** digits === 0;
      return new Intl.NumberFormat(locale, {
        style: "currency",
        currency: value.currency,
        minimumFractionDigits: whole ? 0 : digits,
        maximumFractionDigits: whole ? 0 : digits,
      }).format(value.minor / 10 ** digits);
    }
    case "count":
      return new Intl.NumberFormat(locale).format(value.value);
    case "percent":
      return `${(value.basis_points / 100).toFixed(2)}%`;
    case "unknown":
      return "unknown";
  }
}

const UNKNOWN_REASONS: Record<string, string> = {
  "source.not_connected": "Not connected yet",
  "freshness.stale": "Data is out of date",
  "freshness.alert": "Data is badly out of date",
  "freshness.never": "Never synced",
  "mrr.price_not_exact": "A price could not be worked out exactly, so no guess is shown",
};

export function explainUnknown(reason: string): string {
  return UNKNOWN_REASONS[reason] ?? "Cannot be worked out right now";
}

export function ago(iso: string, now: Date = new Date()): string {
  const seconds = Math.max(0, Math.round((now.getTime() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

export function freshnessLabel(freshness: Freshness, lastSuccess: string | null, now?: Date): string {
  switch (freshness) {
    case "fresh":
      return lastSuccess === null ? "Up to date" : `Updated ${ago(lastSuccess, now)}`;
    case "stale":
      return "Out of date: numbers hidden";
    case "alert":
      return "Badly out of date: numbers hidden";
    case "never":
      return "Not connected yet";
  }
}
