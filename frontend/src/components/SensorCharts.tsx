/**
 * Live sensor charts.
 *
 * Four small multiples rather than one combined chart: the sensors have wildly
 * different units and ranges (°C, %, ppm, V), so a shared axis would compress
 * three of them into a flat line. Recharts is configured with flat strokes and
 * no gradients, per design.md.
 */

import { useMemo } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import type { Reading } from '@/types';
import { formatClock, formatNumber } from '@/lib/format';

interface SensorChartsProps {
  readings: Reading[];
  deviceId: string | null;
}

interface SeriesConfig {
  key: keyof Reading;
  label: string;
  unit: string;
  colour: string;
  digits: number;
}

/* Every stroke is white or a near-white mute: design.md allows no brand
   accent, and chart lines are chrome, not status signals. */
const HARDWARE_SERIES: readonly SeriesConfig[] = [
  { key: 'temperature_c', label: 'Temperature', unit: '°C', colour: '#ffffff', digits: 1 },
  { key: 'humidity_pct', label: 'Humidity', unit: '%', colour: '#f0f0fa', digits: 1 },
  { key: 'smoke_ppm', label: 'Smoke (MQ-2)', unit: 'ppm', colour: '#ffffff', digits: 0 },
  { key: 'air_quality_ppm', label: 'Air quality (MQ-135)', unit: 'ppm', colour: '#f0f0fa', digits: 0 },
];

/*
 * A camera node gets its own axes. Charting its evidence under "Smoke (MQ-2)"
 * would be a lie about where the number came from, and plotting a gas series
 * that is permanently null would just be four empty panels.
 */
const CAMERA_SERIES: readonly SeriesConfig[] = [
  { key: 'flame_ratio', label: 'Flame in frame', unit: '', colour: '#ffffff', digits: 4 },
  { key: 'flicker', label: 'Flicker', unit: '', colour: '#f0f0fa', digits: 2 },
  { key: 'haze_index', label: 'Haze', unit: '', colour: '#ffffff', digits: 2 },
  { key: 'luminance', label: 'Brightness', unit: '', colour: '#f0f0fa', digits: 2 },
];

export default function SensorCharts({ readings, deviceId }: SensorChartsProps) {
  const data = useMemo(() => {
    const filtered = deviceId
      ? readings.filter((reading) => reading.device_id === deviceId)
      : readings;
    return filtered.map((reading) => ({
      ...reading,
      clock: formatClock(reading.recorded_at),
    }));
  }, [readings, deviceId]);

  /*
   * Only switch to camera axes when a camera device is actually selected. With
   * the whole fleet in view the hardware series stay: mixed data leaves gaps
   * in the lines, which Recharts renders as breaks — an honest depiction of
   * "this sensor did not report that quantity".
   */
  const series = useMemo(() => {
    if (deviceId === null) return HARDWARE_SERIES;
    const kinds = new Set(data.map((reading) => reading.sensor_kind));
    return kinds.size === 1 && kinds.has('camera') ? CAMERA_SERIES : HARDWARE_SERIES;
  }, [data, deviceId]);

  if (data.length === 0) {
    return (
      <div className="panel p-xxl text-center" data-testid="charts-empty">
        <p className="eyebrow">Waiting for telemetry</p>
        <p className="mt-sm text-body-md text-on-primary-mute">
          No readings yet for this device. Start the simulator or power on a sensor node.
        </p>
      </div>
    );
  }

  return (
    <div
      className="grid grid-cols-1 gap-xl laptop:grid-cols-2"
      data-testid="sensor-charts"
    >
      {series.map((series) => (
        <figure key={series.key as string} className="panel p-xl">
          <figcaption className="mb-md flex items-baseline justify-between">
            <span className="eyebrow">{series.label}</span>
            <span className="font-display text-body-lg">
              {formatNumber(
                data[data.length - 1]?.[series.key] as number | undefined,
                series.digits,
              )}
              <span className="ml-xxs text-caption text-on-primary-mute">{series.unit}</span>
            </span>
          </figcaption>
          <div style={{ width: '100%', height: 180 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
                <CartesianGrid stroke="#3a3a3f" strokeDasharray="2 4" vertical={false} />
                <XAxis
                  dataKey="clock"
                  stroke="#5a5a5f"
                  tick={{ fill: '#f0f0fa', fontSize: 11 }}
                  tickLine={false}
                  minTickGap={48}
                />
                <YAxis
                  stroke="#5a5a5f"
                  tick={{ fill: '#f0f0fa', fontSize: 11 }}
                  tickLine={false}
                  width={52}
                  domain={['auto', 'auto']}
                />
                <Tooltip
                  contentStyle={{
                    background: '#0a0a0a',
                    border: '1px solid #3a3a3f',
                    borderRadius: 4,
                    color: '#ffffff',
                    fontSize: 13,
                  }}
                  labelStyle={{ color: '#f0f0fa' }}
                  formatter={(value: number) => [
                    `${formatNumber(value, series.digits)} ${series.unit}`,
                    series.label,
                  ]}
                />
                <Line
                  type="monotone"
                  dataKey={series.key as string}
                  stroke={series.colour}
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </figure>
      ))}
    </div>
  );
}
