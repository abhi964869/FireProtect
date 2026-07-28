/**
 * Real outdoor conditions at the visitor's location.
 *
 * The honesty rules this screen follows:
 *
 * 1. Every number here is a genuine instrument reading from a public
 *    monitoring network. Nothing is simulated.
 * 2. Every number here describes air OUTSIDE, near the visitor — not the room
 *    the phone is in. That is stated plainly, not buried in a tooltip, because
 *    the difference matters enormously for a fire product.
 * 3. What a phone cannot measure is named explicitly. Leaving it unsaid invites
 *    the reader to assume their phone is sniffing the air, which it is not.
 */

import { useCallback, useEffect, useState } from 'react';

import { aqiBand } from '@/lib/ambient';
import type { AmbientConditions, AmbientMonitor } from '@/lib/ambient';

interface LocalConditionsProps {
  /** Shared with the camera node so both use one lookup and one permission. */
  monitor: AmbientMonitor;
  onLoaded?: (conditions: AmbientConditions | null) => void;
}

function formatAge(fetchedAt: number): string {
  const seconds = Math.max(0, Math.round((Date.now() - fetchedAt) / 1000));
  if (seconds < 90) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  return `${Math.round(minutes / 60)} h ago`;
}

export function LocalConditions({ monitor, onLoaded }: LocalConditionsProps) {
  const [conditions, setConditions] = useState<AmbientConditions | null>(
    monitor.current,
  );
  const [busy, setBusy] = useState(false);
  const [asked, setAsked] = useState(monitor.current !== null);

  const load = useCallback(async () => {
    setBusy(true);
    setAsked(true);
    try {
      const result = await monitor.refresh();
      setConditions(result);
      onLoaded?.(result);
    } finally {
      setBusy(false);
    }
  }, [monitor, onLoaded]);

  // Refresh on the same slow cadence the monitor caches at, so a tab left open
  // does not sit on hour-old numbers presented as current.
  useEffect(() => {
    if (conditions === null) return;
    const timer = window.setInterval(() => {
      void monitor.refresh().then(setConditions);
    }, 10 * 60 * 1000);
    return () => {
      window.clearInterval(timer);
    };
  }, [conditions, monitor]);

  const band = aqiBand(conditions?.us_aqi ?? null);

  return (
    <div className="panel p-xl" data-testid="local-conditions">
      <p className="eyebrow">Your location · real measurements</p>
      <h3 className="mt-xs text-display-sm">Outdoor air</h3>
      <p className="mt-md max-w-[62ch] text-body-md text-on-primary-mute">
        Live readings from public weather and air-quality stations near you.
        These are real instruments — but they are <strong>outdoors</strong>, not
        in your room. A phone has no humidity, gas or particulate sensor of its
        own, so nothing here is measured by this device.
      </p>

      {conditions === null ? (
        <div className="mt-xl">
          <button
            className="btn-ghost"
            type="button"
            onClick={() => {
              void load();
            }}
            disabled={busy}
            data-testid="enable-location"
          >
            {busy ? 'Locating…' : 'Use my location'}
          </button>
          {asked && !busy ? (
            <p
              className="mt-md text-body-sm text-status-warning"
              role="status"
              data-testid="location-unavailable"
            >
              {monitor.permissionDenied
                ? 'Location permission was declined, so outdoor conditions stay unmeasured.'
                : 'Could not reach the monitoring service. Nothing is shown rather than a guess.'}
            </p>
          ) : (
            <p className="mt-md text-caption text-on-primary-mute">
              Your approximate position is used to look up nearby stations. It
              is never sent to the FireProtect server.
            </p>
          )}
        </div>
      ) : (
        <>
          <dl className="mt-xl grid grid-cols-2 gap-lg laptop:grid-cols-3">
            <Reading
              label="Temperature"
              value={conditions.temperature_c}
              unit="°C"
              digits={1}
            />
            <Reading
              label="Humidity"
              value={conditions.humidity_pct}
              unit="%"
              digits={0}
            />
            <Reading label="AQI (US)" value={conditions.us_aqi} unit="" digits={0} />
            <Reading
              label="PM2.5"
              value={conditions.pm2_5_ugm3}
              unit="µg/m³"
              digits={1}
              hint="Fine particulate — what smoke is made of"
            />
            <Reading
              label="PM10"
              value={conditions.pm10_ugm3}
              unit="µg/m³"
              digits={1}
              hint="Coarse particulate — dust, pollen"
            />
            <div>
              <dt className="eyebrow">Air quality</dt>
              <dd
                className="mt-xxs text-body-lg"
                data-testid="aqi-band"
              >
                {band.label}
              </dd>
              <p className="mt-xxs text-caption text-on-primary-mute">
                {band.detail}
              </p>
            </div>
          </dl>

          <p className="mt-xl border-t-hairline border-hairline-on-dark pt-lg text-caption text-on-primary-mute">
            Updated {formatAge(conditions.fetchedAt)}. Stations report hourly.
            High PM2.5 outdoors can indicate wildfire smoke in the area — it is
            not a substitute for a detector inside the building.
          </p>
        </>
      )}
    </div>
  );
}

function Reading({
  label,
  value,
  unit,
  digits,
  hint,
}: {
  label: string;
  value: number | null;
  unit: string;
  digits: number;
  hint?: string;
}) {
  return (
    <div>
      <dt className="eyebrow">{label}</dt>
      <dd className="mt-xxs font-mono text-body-lg tabular-nums">
        {/* An em dash, never a zero. A missing reading and a reading of zero
            are different facts and must not look the same. */}
        {value === null ? '—' : value.toFixed(digits)}
        {value !== null && unit !== '' ? (
          <span className="ml-xxs text-caption text-on-primary-mute">{unit}</span>
        ) : null}
      </dd>
      {hint !== undefined ? (
        <p className="mt-xxs text-caption text-on-primary-mute">{hint}</p>
      ) : null}
    </div>
  );
}

export default LocalConditions;
