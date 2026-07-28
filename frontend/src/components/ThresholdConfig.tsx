/** Threshold configuration form. */

import { useEffect, useMemo, useState } from 'react';

import { api } from '@/lib/api';
import type { Threshold } from '@/types';
import { humaniseKey } from '@/lib/format';

type Draft = Record<string, string>;

export function ThresholdConfig() {
  const [thresholds, setThresholds] = useState<Threshold[] | null>(null);
  const [draft, setDraft] = useState<Draft>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .thresholds()
      .then((rows) => {
        if (cancelled) return;
        setThresholds(rows);
        setDraft(Object.fromEntries(rows.map((row) => [row.key, String(row.value)])));
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        setError(cause instanceof Error ? cause.message : 'Failed to load thresholds');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const dirty = useMemo(() => {
    if (!thresholds) return false;
    return thresholds.some((row) => draft[row.key] !== String(row.value));
  }, [thresholds, draft]);

  /** Per-field validation. Empty, non-numeric and negative are all rejected. */
  const fieldErrors = useMemo(() => {
    const errors: Record<string, string> = {};
    for (const [key, raw] of Object.entries(draft)) {
      if (raw.trim() === '') {
        errors[key] = 'Required';
      } else if (!Number.isFinite(Number(raw))) {
        errors[key] = 'Must be a number';
      } else if (Number(raw) < 0) {
        errors[key] = 'Must be zero or greater';
      }
    }
    return errors;
  }, [draft]);

  const hasFieldErrors = Object.keys(fieldErrors).length > 0;

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!thresholds || hasFieldErrors) return;

    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      // Send only what actually changed, so a concurrent edit elsewhere is not
      // clobbered by fields this form never touched.
      const changed: Record<string, number> = {};
      for (const row of thresholds) {
        const next = Number(draft[row.key]);
        if (next !== row.value) changed[row.key] = next;
      }
      if (Object.keys(changed).length === 0) return;

      const updated = await api.updateThresholds(changed);
      setThresholds(updated);
      setDraft(Object.fromEntries(updated.map((row) => [row.key, String(row.value)])));
      setSaved(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to save thresholds');
    } finally {
      setSaving(false);
    }
  }

  function reset() {
    if (!thresholds) return;
    setDraft(Object.fromEntries(thresholds.map((row) => [row.key, String(row.value)])));
    setSaved(false);
    setError(null);
  }

  if (error && !thresholds) {
    return (
      <div className="panel p-xxl" role="alert" data-testid="thresholds-error">
        <p className="eyebrow text-status-warning">Thresholds unavailable</p>
        <p className="mt-sm text-body-md text-on-primary-mute">{error}</p>
      </div>
    );
  }

  if (!thresholds) {
    return (
      <div className="panel p-xxl" data-testid="thresholds-loading">
        <p className="eyebrow">Loading thresholds…</p>
      </div>
    );
  }

  return (
    <form className="panel p-xxl" onSubmit={handleSubmit} data-testid="threshold-config">
      <p className="eyebrow">Alert thresholds</p>
      <p className="mt-sm max-w-[64ch] text-body-md text-on-primary-mute">
        These values shape the wording of alert messages and the fallback rules used
        when the trained model is unavailable. The random forest itself is not
        retrained by changing them.
      </p>

      <div className="mt-xxl grid grid-cols-1 gap-xl sm-mobile:grid-cols-2 laptop:grid-cols-3">
        {thresholds.map((row) => {
          const fieldError = fieldErrors[row.key];
          const inputId = `threshold-${row.key}`;
          return (
            <div key={row.key}>
              <label htmlFor={inputId} className="eyebrow block">
                {humaniseKey(row.key)}
              </label>
              <input
                id={inputId}
                name={row.key}
                type="number"
                step="any"
                min="0"
                inputMode="decimal"
                className="text-input mt-xs w-full"
                value={draft[row.key] ?? ''}
                aria-invalid={fieldError ? true : undefined}
                aria-describedby={fieldError ? `${inputId}-error` : undefined}
                onChange={(event) => {
                  setSaved(false);
                  setDraft((current) => ({ ...current, [row.key]: event.target.value }));
                }}
              />
              {fieldError ? (
                <p id={`${inputId}-error`} className="mt-xxs text-caption text-status-fire">
                  {fieldError}
                </p>
              ) : null}
            </div>
          );
        })}
      </div>

      {error ? (
        <p className="mt-xl text-caption text-status-fire" role="alert">
          {error}
        </p>
      ) : null}
      {saved ? (
        <p className="mt-xl text-caption text-status-safe" role="status">
          Thresholds saved.
        </p>
      ) : null}

      <div className="mt-xxl flex flex-wrap gap-md">
        <button type="submit" className="btn-ghost" disabled={saving || !dirty || hasFieldErrors}>
          {saving ? 'Saving' : 'Save thresholds'}
        </button>
        <button type="button" className="btn-ghost" onClick={reset} disabled={saving || !dirty}>
          Reset
        </button>
      </div>
    </form>
  );
}
