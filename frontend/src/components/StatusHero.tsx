/**
 * Full-bleed photographic hero.
 *
 * design.md: "every marketing band is a full-viewport photograph... Type and
 * CTA sit overlaid on the photograph at high opacity with no scrim." The
 * backdrop is graded dark at generation time rather than scrimmed here, per
 * "Grade the photo, not the canvas".
 */

import type { FireStatus, Stats } from '@/types';
import { formatInteger, statusHex, statusLabel, statusTextClass } from '@/lib/format';

interface StatusHeroProps {
  status: FireStatus;
  stats: Stats | null;
  onViewAlerts: () => void;
}

export function StatusHero({ status, stats, onViewAlerts }: StatusHeroProps) {
  const headline =
    status === 'FIRE'
      ? 'Fire detected'
      : status === 'WARNING'
        ? 'Elevated risk'
        : 'All systems safe';

  return (
    <section
      className="band-photo band-hero relative flex min-h-[88vh] items-center"
      aria-labelledby="hero-heading"
    >
      <div className="reading-column py-24">
        <p className="eyebrow" data-testid="hero-eyebrow">
          FireProtect · Real-time monitoring
        </p>

        <h1
          id="hero-heading"
          className="mt-md text-display-md sm-mobile:text-display-lg mobile:text-display-xl desktop:text-display-xxl"
          data-testid="hero-headline"
        >
          {headline}
        </h1>

        {/*
          The status is carried by text first and colour second. Colour alone
          would fail for colour-blind users and in monochrome, which is not
          acceptable for a life-safety readout.
        */}
        <p
          className={`mt-xl flex items-center gap-sm text-body-lg ${statusTextClass(status)}`}
          data-testid="hero-status"
        >
          <span
            className={status === 'FIRE' ? 'motion-safe-pulse' : undefined}
            style={{
              display: 'inline-block',
              width: 10,
              height: 10,
              borderRadius: 9999,
              backgroundColor: statusHex(status),
            }}
            aria-hidden="true"
          />
          <span className="uppercase tracking-[0.96px]">{statusLabel(status)}</span>
        </p>

        <dl className="mt-xxl flex flex-wrap gap-huge" data-testid="hero-stats">
          <HeroStat label="Devices online" value={
            stats ? `${formatInteger(stats.online_devices)} / ${formatInteger(stats.total_devices)}` : '--'
          } />
          <HeroStat label="Open alerts" value={stats ? formatInteger(stats.open_alerts) : '--'} />
          <HeroStat label="Readings" value={stats ? formatInteger(stats.total_readings) : '--'} />
        </dl>

        {/* design.md: "a single ghost-outlined pill CTA per band" — never two. */}
        <div className="mt-huge">
          <button type="button" className="btn-ghost" onClick={onViewAlerts}>
            View alert history
          </button>
        </div>
      </div>
    </section>
  );
}

function HeroStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="eyebrow">{label}</dt>
      <dd className="mt-xxs font-display text-display-md">{value}</dd>
    </div>
  );
}
