/** Display formatting helpers, shared by every component. */

import type { FireStatus } from '@/types';

/** Tailwind text colour class for a status. */
export function statusTextClass(status: FireStatus): string {
  switch (status) {
    case 'FIRE':
      return 'text-status-fire';
    case 'WARNING':
      return 'text-status-warning';
    default:
      return 'text-status-safe';
  }
}

/** Tailwind border colour class for a status. */
export function statusBorderClass(status: FireStatus): string {
  switch (status) {
    case 'FIRE':
      return 'border-status-fire';
    case 'WARNING':
      return 'border-status-warning';
    default:
      return 'border-hairline-on-dark';
  }
}

/** Raw hex for a status, for canvas/SVG contexts where classes do not apply. */
export function statusHex(status: FireStatus): string {
  switch (status) {
    case 'FIRE':
      return '#ff4d3d';
    case 'WARNING':
      return '#f5a524';
    default:
      return '#e8e8ee';
  }
}

/**
 * A short, human phrase for each status.
 *
 * Colour must never be the only carrier of a life-safety state, so every
 * status indicator pairs its colour with this text.
 */
export function statusLabel(status: FireStatus): string {
  switch (status) {
    case 'FIRE':
      return 'Fire detected';
    case 'WARNING':
      return 'Elevated risk';
    default:
      return 'All clear';
  }
}

export function formatNumber(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '--';
  }
  return value.toFixed(digits);
}

export function formatInteger(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '--';
  }
  return Math.round(value).toLocaleString();
}

/** HH:MM:SS in the viewer's locale, for chart axes and reading timestamps. */
export function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return '--:--:--';
  }
  return date.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return 'unknown';
  }
  return date.toLocaleString();
}

/** "3m ago" style relative time, for device last-seen and alert timestamps. */
export function formatRelative(iso: string, now: number = Date.now()): string {
  const timestamp = new Date(iso).getTime();
  if (Number.isNaN(timestamp)) {
    return 'unknown';
  }
  const seconds = Math.max(0, Math.round((now - timestamp) / 1000));
  if (seconds < 5) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

/** Turn a threshold key into a readable label. */
export function humaniseKey(key: string): string {
  return key
    .replace(/_c$/, ' (°C)')
    .replace(/_ppm$/, ' (ppm)')
    .replace(/_volts$/, ' (V)')
    .replace(/_/g, ' ')
    .replace(/^./, (c) => c.toUpperCase());
}
