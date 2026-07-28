/** Alert timeline with acknowledge / resolve actions. */

import { useState } from 'react';

import type { Alert } from '@/types';
import { EmptyState } from '@/components/EmptyState';
import {
  formatDateTime,
  formatNumber,
  formatRelative,
  statusBorderClass,
  statusTextClass,
} from '@/lib/format';

interface AlertHistoryProps {
  alerts: Alert[];
  onAcknowledge: (id: number) => Promise<void>;
  onResolve: (id: number) => Promise<void>;
}

export function AlertHistory({ alerts, onAcknowledge, onResolve }: AlertHistoryProps) {
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(id: number, action: (id: number) => Promise<void>) {
    setBusyId(id);
    setError(null);
    try {
      await action(id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Action failed');
    } finally {
      setBusyId(null);
    }
  }

  if (alerts.length === 0) {
    return (
      <EmptyState
        image="/assets/empty-state-no-alerts.webp"
        title="No alerts recorded"
        body="Nothing has crossed a threshold. Alerts appear here the moment a device reports a sustained warning or fire condition."
        testId="alerts-empty"
      />
    );
  }

  return (
    <div data-testid="alert-history">
      {error ? (
        <p className="mb-md text-caption text-status-fire" role="alert">
          {error}
        </p>
      ) : null}

      <ol className="flex flex-col gap-md">
        {alerts.map((alert) => {
          const open = alert.resolved_at === null;
          return (
            <li
              key={alert.id}
              className={`panel border-l-4 p-xl ${statusBorderClass(alert.severity)}`}
              data-testid={`alert-${alert.id}`}
            >
              <div className="flex flex-wrap items-start justify-between gap-md">
                <div className="min-w-0 flex-1">
                  <p className="flex flex-wrap items-center gap-sm">
                    <span
                      className={`text-caption uppercase tracking-[0.96px] ${statusTextClass(alert.severity)}`}
                    >
                      {alert.severity}
                    </span>
                    <span className="eyebrow">{alert.device_id}</span>
                    {open ? (
                      <span className="eyebrow border-hairline border-hairline-on-dark px-xs">
                        Open
                      </span>
                    ) : (
                      <span className="eyebrow opacity-60">Resolved</span>
                    )}
                    {alert.acknowledged ? (
                      <span className="eyebrow opacity-60">Acknowledged</span>
                    ) : null}
                  </p>

                  <p className="mt-xs text-body-md">{alert.message}</p>

                  <p className="mt-xs text-caption text-on-primary-mute">
                    <time dateTime={alert.triggered_at} title={formatDateTime(alert.triggered_at)}>
                      {formatRelative(alert.triggered_at)}
                    </time>
                    {' · '}
                    {formatNumber(alert.temperature_c)} °C
                    {' · '}
                    {formatNumber(alert.smoke_ppm, 0)} ppm
                    {' · P(fire) '}
                    {formatNumber(alert.fire_probability, 2)}
                  </p>
                </div>

                <div className="flex shrink-0 gap-sm">
                  {!alert.acknowledged ? (
                    <button
                      type="button"
                      className="btn-ghost"
                      disabled={busyId === alert.id}
                      onClick={() => void run(alert.id, onAcknowledge)}
                    >
                      {busyId === alert.id ? 'Working' : 'Acknowledge'}
                    </button>
                  ) : null}
                  {open ? (
                    <button
                      type="button"
                      className="btn-ghost"
                      disabled={busyId === alert.id}
                      onClick={() => void run(alert.id, onResolve)}
                    >
                      Resolve
                    </button>
                  ) : null}
                </div>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
