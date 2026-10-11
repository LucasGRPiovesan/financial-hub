import { useEffect, useState } from "react";

import { cn } from "../lib/cn";
import { formatDateTime, formatRelative, parseIsoInstant, type DateTimeOptions } from "../lib/datetime";

interface DateTimeProps extends DateTimeOptions {
  /** ISO 8601 UTC timestamp from the API. */
  value: string;
  className?: string;
}

/** Absolute time in the operator's locale with a visible timezone label. */
export function DateTime({ value, locale, timeZone, className }: DateTimeProps) {
  let formatted;
  try {
    formatted = formatDateTime(value, { locale, timeZone });
  } catch {
    return <span className={cn("text-ink-faint", className)}>&mdash;</span>;
  }
  return (
    <time dateTime={value} title={`${value} (UTC)`} className={cn("figures whitespace-nowrap", className)}>
      {formatted.text}
      {formatted.zone ? <span className="ml-1 text-ink-faint">{formatted.zone}</span> : null}
    </time>
  );
}

interface RelativeTimeProps {
  value: string;
  locale?: string;
  className?: string;
  /** Refresh interval in ms; 0 disables. */
  refreshMs?: number;
}

/** "3 minutes ago": keeps itself fresh; hover (title) shows the absolute time and zone. */
export function RelativeTime({ value, locale, className, refreshMs = 30_000 }: RelativeTimeProps) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (refreshMs <= 0) return;
    const timer = window.setInterval(() => setNow(new Date()), refreshMs);
    return () => window.clearInterval(timer);
  }, [refreshMs]);

  let text: string;
  let title: string;
  try {
    parseIsoInstant(value);
    text = formatRelative(value, now, locale);
    const absolute = formatDateTime(value, { locale });
    title = `${absolute.text} ${absolute.zone}`.trim();
  } catch {
    return <span className={cn("text-ink-faint", className)}>&mdash;</span>;
  }
  return (
    <time dateTime={value} title={title} className={cn("whitespace-nowrap", className)}>
      {text}
    </time>
  );
}
