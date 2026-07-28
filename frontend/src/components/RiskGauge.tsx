/**
 * Fire-risk gauge.
 *
 * A 240-degree SVG arc rather than a filled bar: design.md forbids shadows and
 * gradients, so the gauge is drawn as flat strokes with the track in
 * hairline-on-dark and the value in the status colour.
 */

import type { FireStatus } from '@/types';
import { formatNumber, statusHex, statusLabel } from '@/lib/format';

interface RiskGaugeProps {
  /** 0-100 risk score from the backend. */
  score: number;
  status: FireStatus;
  size?: number;
}

const START_ANGLE = 150; // degrees, clockwise from +x axis
const SWEEP = 240;

function polar(cx: number, cy: number, radius: number, degrees: number) {
  const radians = (degrees * Math.PI) / 180;
  return { x: cx + radius * Math.cos(radians), y: cy + radius * Math.sin(radians) };
}

function arcPath(cx: number, cy: number, radius: number, from: number, to: number) {
  const start = polar(cx, cy, radius, from);
  const end = polar(cx, cy, radius, to);
  const largeArc = Math.abs(to - from) > 180 ? 1 : 0;
  return `M ${start.x} ${start.y} A ${radius} ${radius} 0 ${largeArc} 1 ${end.x} ${end.y}`;
}

export function RiskGauge({ score, status, size = 220 }: RiskGaugeProps) {
  // Guard against NaN/out-of-range from a malformed payload rather than
  // emitting an invalid SVG path that renders as nothing.
  const safeScore = Number.isFinite(score) ? Math.min(100, Math.max(0, score)) : 0;

  const cx = size / 2;
  const cy = size / 2;
  const radius = size / 2 - 18;
  const endAngle = START_ANGLE + (SWEEP * safeScore) / 100;
  const colour = statusHex(status);

  return (
    <div className="flex flex-col items-center" data-testid="risk-gauge">
      <svg
        width={size}
        height={size * 0.78}
        viewBox={`0 0 ${size} ${size * 0.78}`}
        role="img"
        aria-label={`Fire risk ${formatNumber(safeScore, 0)} out of 100. ${statusLabel(status)}.`}
      >
        <path
          d={arcPath(cx, cy, radius, START_ANGLE, START_ANGLE + SWEEP)}
          fill="none"
          stroke="#3a3a3f"
          strokeWidth={10}
          strokeLinecap="round"
        />
        {safeScore > 0 && (
          <path
            d={arcPath(cx, cy, radius, START_ANGLE, endAngle)}
            fill="none"
            stroke={colour}
            strokeWidth={10}
            strokeLinecap="round"
            data-testid="risk-gauge-value"
          />
        )}
        <text
          x={cx}
          y={cy + 4}
          textAnchor="middle"
          fill="#ffffff"
          fontFamily="D-DIN-Bold, Arial Narrow, Arial, sans-serif"
          fontWeight={700}
          fontSize={size * 0.24}
          letterSpacing="1.2"
        >
          {formatNumber(safeScore, 0)}
        </text>
        <text
          x={cx}
          y={cy + size * 0.17}
          textAnchor="middle"
          fill="#f0f0fa"
          fontFamily="D-DIN, Arial, sans-serif"
          fontSize={12}
          letterSpacing="0.96"
        >
          RISK SCORE
        </text>
      </svg>
      <p className="eyebrow mt-xs" data-testid="risk-gauge-label">
        {statusLabel(status)}
      </p>
    </div>
  );
}
