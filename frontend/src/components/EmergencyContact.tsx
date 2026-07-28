/**
 * Emergency contact settings and the permanent high-temperature record.
 *
 * The one rule this screen follows above all others: never let someone believe
 * they are protected when they are not. If the server has no mail transport
 * configured, or notifications are muted, or no contact is saved, it says so
 * in plain words at the top — because the failure mode of a fire alarm nobody
 * knows is switched off is the worst one available.
 */

import { useCallback, useEffect, useState, type FormEvent } from 'react';

import { api } from '@/lib/api';
import { authErrorMessage, useAuth } from '@/lib/useAuth';
import type { NotificationStatus, TemperatureEvent } from '@/types';

function formatWhen(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

export function EmergencyContact() {
  const { user, setUser } = useAuth();
  const [email, setEmail] = useState(user?.emergency_email ?? '');
  const [name, setName] = useState(user?.emergency_name ?? '');
  const [limit, setLimit] = useState(String(user?.temperature_limit_c ?? 55));
  const [enabled, setEnabled] = useState(user?.notifications_enabled ?? true);

  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [status, setStatus] = useState<NotificationStatus | null>(null);
  const [events, setEvents] = useState<TemperatureEvent[]>([]);
  const [historyError, setHistoryError] = useState<string | null>(null);

  /*
   * The session resolves asynchronously, so on first render `user` is null and
   * every field initialises empty. Without this the form would show blanks and
   * a "no contact saved" warning to someone who has one — and saving would
   * then wipe it. Syncing on identity change (not on every render) keeps the
   * user's in-progress edits intact.
   */
  useEffect(() => {
    if (user === null) return;
    setEmail(user.emergency_email);
    setName(user.emergency_name);
    setLimit(String(user.temperature_limit_c));
    setEnabled(user.notifications_enabled);
  }, [user?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const loadHistory = useCallback(() => {
    api
      .temperatureEvents()
      .then(setEvents)
      .catch((cause: unknown) => {
        setHistoryError(authErrorMessage(cause));
      });
  }, []);

  useEffect(() => {
    api.notificationStatus().then(setStatus).catch(() => undefined);
    loadHistory();
  }, [loadHistory]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setSaved(false);

    const parsed = Number(limit);
    if (!Number.isFinite(parsed) || parsed < 30 || parsed > 150) {
      setError('Temperature limit must be between 30 °C and 150 °C.');
      return;
    }

    setSaving(true);
    try {
      const updated = await api.updateEmergencyContact({
        emergency_email: email.trim(),
        emergency_name: name.trim(),
        temperature_limit_c: parsed,
        notifications_enabled: enabled,
      });
      setUser(updated);
      setSaved(true);
      const refreshed = await api.notificationStatus();
      setStatus(refreshed);
    } catch (cause) {
      setError(authErrorMessage(cause));
    } finally {
      setSaving(false);
    }
  };

  // Ordered worst-first: a server with no mail transport makes every other
  // warning here irrelevant, so it must be the one the user reads.
  const warning = (() => {
    if (status?.configured === false) {
      return 'This server has no email transport configured, so no emergency email can be sent. Set RESEND_API_KEY, or SMTP_HOST, SMTP_USERNAME and SMTP_PASSWORD.';
    }
    if (!enabled) return 'Notifications are muted. No email will be sent.';
    if (email.trim() === '') {
      return 'No emergency contact saved, so no email will be sent.';
    }
    return null;
  })();

  return (
    <div className="grid grid-cols-1 gap-xl laptop:grid-cols-2">
      <div className="panel p-xl">
        <p className="eyebrow">Escalation</p>
        <h3 className="mt-xs text-display-sm">Emergency contact</h3>
        <p className="mt-md text-body-md text-on-primary-mute">
          When a device you own reports a temperature above your limit, this
          address is emailed immediately.
        </p>

        {warning !== null ? (
          <p
            className="mt-lg border-hairline border-status-warning p-md text-body-sm text-status-warning"
            role="status"
            data-testid="notification-warning"
          >
            {warning}
          </p>
        ) : (
          <p className="mt-lg text-body-sm text-status-safe" data-testid="notification-ok">
            Active — alerts will be sent to {email.trim()} via{' '}
            {status?.transport === 'resend' ? 'Resend' : 'email'}.
          </p>
        )}

        <form className="mt-xl flex flex-col gap-md" onSubmit={submit} noValidate>
          <label className="flex flex-col gap-xs">
            <span className="text-caption text-on-primary-mute">
              Emergency email address
            </span>
            <input
              className="text-input"
              type="email"
              value={email}
              placeholder="someone@example.com"
              autoComplete="email"
              onChange={(event) => {
                setEmail(event.target.value);
              }}
            />
          </label>

          <label className="flex flex-col gap-xs">
            <span className="text-caption text-on-primary-mute">
              Their name (optional)
            </span>
            <input
              className="text-input"
              type="text"
              value={name}
              maxLength={128}
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
          </label>

          <label className="flex flex-col gap-xs">
            <span className="text-caption text-on-primary-mute">
              Alert above (°C)
            </span>
            <input
              className="text-input"
              type="number"
              value={limit}
              min={30}
              max={150}
              step={1}
              inputMode="decimal"
              onChange={(event) => {
                setLimit(event.target.value);
              }}
            />
            <span className="text-caption text-on-primary-mute">
              55 °C is a good default: hotter than any normal room, well below
              flashover.
            </span>
          </label>

          <label className="flex items-center gap-sm">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => {
                setEnabled(event.target.checked);
              }}
            />
            <span className="text-body-sm">Send emergency emails</span>
          </label>

          {error !== null ? (
            <p className="text-body-sm text-status-fire" role="alert">
              {error}
            </p>
          ) : null}
          {saved && error === null ? (
            <p className="text-body-sm text-status-safe" role="status">
              Saved.
            </p>
          ) : null}

          <button className="btn-ghost mt-sm" type="submit" disabled={saving}>
            {saving ? 'Saving…' : 'Save contact'}
          </button>
        </form>
      </div>

      <div className="panel p-xl">
        <div className="flex items-start justify-between gap-md">
          <div>
            <p className="eyebrow">Permanent record</p>
            <h3 className="mt-xs text-display-sm">Temperature history</h3>
          </div>
          <button className="text-body-sm underline" type="button" onClick={loadHistory}>
            Refresh
          </button>
        </div>
        <p className="mt-md text-body-md text-on-primary-mute">
          Every time one of your devices crossed its limit. Kept indefinitely,
          unlike raw readings, which are pruned.
        </p>

        {historyError !== null ? (
          <p className="mt-lg text-body-sm text-status-warning" role="alert">
            {historyError}
          </p>
        ) : null}

        {events.length === 0 ? (
          <p className="mt-xl text-body-md text-on-primary-mute" data-testid="no-events">
            No high-temperature events recorded. That is the result you want.
          </p>
        ) : (
          <ul className="mt-xl flex flex-col gap-md" data-testid="event-list">
            {events.map((event) => (
              <li
                key={event.id}
                className="border-t-hairline border-hairline-on-dark pt-md"
              >
                <div className="flex flex-wrap items-baseline justify-between gap-sm">
                  <span className="font-mono text-body-lg tabular-nums text-status-fire">
                    {event.peak_temperature_c.toFixed(1)} °C
                  </span>
                  <span className="text-caption text-on-primary-mute">
                    {formatWhen(event.started_at)}
                  </span>
                </div>
                <p className="mt-xxs text-body-sm text-on-primary-mute">
                  {event.device_id} · limit {event.threshold_c.toFixed(0)} °C ·{' '}
                  {event.ended_at === null ? 'ongoing' : 'resolved'}
                </p>
                <p className="mt-xxs text-caption text-on-primary-mute">
                  {event.notified
                    ? 'Emergency email sent'
                    : event.notify_error !== ''
                      ? `Email failed: ${event.notify_error}`
                      : 'No email sent'}
                </p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export default EmergencyContact;
