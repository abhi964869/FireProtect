/** Device roster with per-device status and latest readings. */

import type { Device, FireStatus, Reading } from '@/types';
import { EmptyState } from '@/components/EmptyState';
import {
  formatNumber,
  formatRelative,
  statusBorderClass,
  statusHex,
  statusLabel,
  statusTextClass,
} from '@/lib/format';

interface DeviceGridProps {
  devices: Device[];
  readings: Reading[];
  selectedId: string | null;
  onSelect: (deviceId: string | null) => void;
}

function latestFor(readings: Reading[], deviceId: string): Reading | undefined {
  for (let i = readings.length - 1; i >= 0; i--) {
    if (readings[i]!.device_id === deviceId) return readings[i];
  }
  return undefined;
}

export function DeviceGrid({ devices, readings, selectedId, onSelect }: DeviceGridProps) {
  if (devices.length === 0) {
    return (
      <EmptyState
        image="/assets/empty-state-no-devices.webp"
        title="No devices reporting"
        body="Nothing has published telemetry yet. Start the simulator with `python simulator/virtual_device.py --scenario normal`, or power on a sensor node."
        testId="devices-empty"
      />
    );
  }

  return (
    <div
      className="grid grid-cols-1 gap-xl sm-mobile:grid-cols-2 laptop:grid-cols-3 desktop:grid-cols-4"
      data-testid="device-grid"
    >
      {devices.map((device) => {
        const latest = latestFor(readings, device.id);
        const status: FireStatus = latest?.server_status ?? 'SAFE';
        const selected = selectedId === device.id;

        return (
          <button
            key={device.id}
            type="button"
            onClick={() => onSelect(selected ? null : device.id)}
            aria-pressed={selected}
            className={[
              'panel p-xl text-left transition-colors',
              statusBorderClass(status),
              selected ? 'bg-canvas-cool text-ink' : 'hover:border-on-primary',
            ].join(' ')}
            data-testid={`device-card-${device.id}`}
          >
            <div className="flex items-start justify-between gap-sm">
              <div>
                <p className={selected ? 'text-micro-cap uppercase text-ink-mute' : 'eyebrow'}>
                  {device.location || 'Unassigned'}
                </p>
                <h3 className="mt-xxs font-display text-body-lg">{device.name || device.id}</h3>
                {/* Labelled explicitly so nobody reads camera evidence as a
                    gas-sensor reading. */}
                <p
                  className={[
                    'mt-xxs text-micro-cap uppercase',
                    selected ? 'text-ink-mute' : 'text-on-primary-mute',
                  ].join(' ')}
                >
                  {device.kind === 'camera' ? 'Camera node' : 'Sensor node'}
                </p>
              </div>
              <span
                className={device.online ? undefined : 'opacity-40'}
                style={{
                  display: 'inline-block',
                  width: 10,
                  height: 10,
                  borderRadius: 9999,
                  backgroundColor: statusHex(status),
                  flexShrink: 0,
                  marginTop: 4,
                }}
                aria-hidden="true"
              />
            </div>

            {/* Status is stated in words, not conveyed by the dot alone. */}
            <p
              className={[
                'mt-md text-caption uppercase tracking-[0.96px]',
                selected ? 'text-ink' : statusTextClass(status),
              ].join(' ')}
            >
              {statusLabel(status)}
            </p>

            {/*
              A camera node and a gas node measure different things, so they
              show different numbers. Rendering "0 ppm" for a device with no
              gas sensor would read as "clean air" — a confident false claim.
            */}
            <dl className="mt-md grid grid-cols-2 gap-x-md gap-y-xs text-caption">
              {device.kind === 'camera' ? (
                <>
                  <Metric
                    selected={selected}
                    label="Flame"
                    value={
                      latest === undefined || latest.flame_ratio === null
                        ? '—'
                        : `${(latest.flame_ratio * 100).toFixed(1)} %`
                    }
                  />
                  <Metric
                    selected={selected}
                    label="Flicker"
                    value={formatNumber(latest?.flicker ?? undefined, 2)}
                  />
                  <Metric
                    selected={selected}
                    label="Haze"
                    value={
                      latest === undefined || latest.haze_index === null
                        ? '—'
                        : `${(latest.haze_index * 100).toFixed(0)} %`
                    }
                  />
                  <Metric selected={selected} label="Risk" value={formatNumber(latest?.risk_score, 0)} />
                </>
              ) : (
                <>
                  <Metric selected={selected} label="Temp" value={`${formatNumber(latest?.temperature_c ?? undefined)} °C`} />
                  <Metric selected={selected} label="Humidity" value={`${formatNumber(latest?.humidity_pct ?? undefined)} %`} />
                  <Metric selected={selected} label="Smoke" value={`${formatNumber(latest?.smoke_ppm ?? undefined, 0)} ppm`} />
                  <Metric selected={selected} label="Risk" value={formatNumber(latest?.risk_score, 0)} />
                </>
              )}
            </dl>

            <p
              className={[
                'mt-md text-caption',
                selected ? 'text-ink-mute' : 'text-on-primary-mute',
              ].join(' ')}
            >
              {device.online ? 'Online' : 'Offline'} · seen {formatRelative(device.last_seen)}
            </p>
          </button>
        );
      })}
    </div>
  );
}

function Metric({
  label,
  value,
  selected,
}: {
  label: string;
  value: string;
  selected: boolean;
}) {
  return (
    <div>
      <dt className={selected ? 'text-ink-mute' : 'text-on-primary-mute'}>{label}</dt>
      <dd className="font-display">{value}</dd>
    </div>
  );
}
