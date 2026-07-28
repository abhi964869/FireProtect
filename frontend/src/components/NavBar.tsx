/**
 * nav-bar-overlay — "top nav across the marketing site".
 *
 * Transparent over the hero photograph, white on image, sticky on scroll,
 * uppercase button-cap type, 24px/32px padding. Collapses to a hamburger
 * below 768px per design.md's collapsing strategy.
 */

import { useState } from 'react';

import type { ConnectionState } from '@/types';

interface NavBarProps {
  connection: ConnectionState;
  sections: readonly { id: string; label: string }[];
  onNavigate: (id: string) => void;
  /** null when signed out. */
  userEmail?: string | null;
  onSignOut?: () => void;
}

const CONNECTION_COPY: Record<ConnectionState, string> = {
  connecting: 'Connecting',
  open: 'Live',
  reconnecting: 'Reconnecting',
  closed: 'Offline',
};

export function NavBar({
  connection,
  sections,
  onNavigate,
  userEmail = null,
  onSignOut,
}: NavBarProps) {
  const [open, setOpen] = useState(false);

  function go(id: string) {
    setOpen(false);
    onNavigate(id);
  }

  return (
    <header
      className="sticky top-0 z-50 bg-canvas-night/0 px-xxl py-xl backdrop-blur-none"
      data-testid="nav-bar"
    >
      <div className="flex items-center justify-between gap-xl">
        <a
          href="#top"
          className="shrink-0 no-underline"
          onClick={(event) => {
            event.preventDefault();
            go('top');
          }}
        >
          <img
            src="/assets/logo-fireprotect.svg"
            alt="FireProtect"
            width={147}
            height={19}
            className="h-[19px] w-[147px]"
          />
        </a>

        {/* Desktop nav */}
        <nav className="hidden items-center gap-xxl mobile:flex" aria-label="Sections">
          {sections.map((section) => (
            <button
              key={section.id}
              type="button"
              onClick={() => go(section.id)}
              className="font-body text-button-cap font-bold uppercase text-on-primary transition-colors hover:text-on-primary-mute"
            >
              {section.label}
            </button>
          ))}
        </nav>

        <div className="flex items-center gap-md">
          {userEmail !== null && onSignOut !== undefined ? (
            <button
              type="button"
              onClick={onSignOut}
              className="hidden font-body text-micro-cap uppercase text-on-primary-mute transition-colors hover:text-on-primary laptop:inline"
              data-testid="sign-out"
            >
              Sign out
            </button>
          ) : null}
          <ConnectionPill connection={connection} />
          <button
            type="button"
            className="btn-ghost mobile:hidden"
            aria-expanded={open}
            aria-controls="mobile-nav"
            onClick={() => setOpen((value) => !value)}
          >
            {open ? 'Close' : 'Menu'}
          </button>
        </div>
      </div>

      {/* Mobile menu retains the dark overlay treatment. */}
      {open ? (
        <nav
          id="mobile-nav"
          aria-label="Sections"
          className="mt-xl flex flex-col gap-md border-t-hairline border-hairline-on-dark pt-xl mobile:hidden"
          data-testid="mobile-nav"
        >
          {sections.map((section) => (
            <button
              key={section.id}
              type="button"
              onClick={() => go(section.id)}
              className="text-left font-body text-button-cap font-bold uppercase text-on-primary"
            >
              {section.label}
            </button>
          ))}
        </nav>
      ) : null}
    </header>
  );
}

function ConnectionPill({ connection }: { connection: ConnectionState }) {
  const live = connection === 'open';
  return (
    <span
      className="flex items-center gap-xs rounded-pill border-hairline border-hairline-on-dark px-md py-xs"
      data-testid="connection-status"
      role="status"
      aria-live="polite"
    >
      <span
        aria-hidden="true"
        className={connection === 'reconnecting' ? 'motion-safe-pulse' : undefined}
        style={{
          display: 'inline-block',
          width: 7,
          height: 7,
          borderRadius: 9999,
          backgroundColor: live ? '#e8e8ee' : '#f5a524',
        }}
      />
      <span className="font-body text-micro-cap uppercase">
        {CONNECTION_COPY[connection]}
      </span>
    </span>
  );
}
