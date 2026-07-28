import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import App from '@/App';
import { AlertHistory } from '@/components/AlertHistory';
import { EmptyState } from '@/components/EmptyState';
import { RiskGauge } from '@/components/RiskGauge';
import { ThresholdConfig } from '@/components/ThresholdConfig';
import type { Alert, Device, Reading, Stats, Threshold } from '@/types';
import { AuthProvider } from '@/lib/useAuth';
import { MockWebSocket } from '@/test/setup';

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const device: Device = {
  id: 'esp32-sim-01',
  name: 'esp32-sim-01',
  location: 'Kitchen',
  kind: 'hardware',
  firmware_version: '1.0.0',
  first_seen: new Date().toISOString(),
  last_seen: new Date().toISOString(),
  online: true,
};

function makeReading(overrides: Partial<Reading> = {}): Reading {
  return {
    id: 1,
    device_id: 'esp32-sim-01',
    recorded_at: new Date().toISOString(),
    sensor_kind: 'hardware',
    temperature_c: 22.4,
    humidity_pct: 45,
    smoke_ppm: 560,
    air_quality_ppm: 110,
    flame_analog_volts: 0.15,
    flame_detected: 0,
    flame_ratio: null,
    luminance: null,
    haze_index: null,
    flicker: null,
    pm2_5_ugm3: null,
    pm10_ugm3: null,
    us_aqi: null,
    temp_rate_c_per_min: 0,
    smoke_rate_ppm_per_min: 0,
    heat_index_c: 22,
    device_status: 'SAFE',
    server_status: 'SAFE',
    fire_probability: 0.01,
    risk_score: 2,
    ...overrides,
  };
}

function makeAlert(overrides: Partial<Alert> = {}): Alert {
  return {
    id: 10,
    device_id: 'esp32-sim-01',
    severity: 'FIRE',
    message: 'Fire detected: flame detected (P(fire)=0.94)',
    triggered_at: new Date().toISOString(),
    resolved_at: null,
    acknowledged: false,
    temperature_c: 88,
    smoke_ppm: 4200,
    fire_probability: 0.94,
    ...overrides,
  };
}

const stats: Stats = {
  total_devices: 1,
  online_devices: 1,
  total_readings: 120,
  open_alerts: 0,
  alerts_24h: 0,
  current_status: 'SAFE',
  max_fire_probability: 0.02,
};

const thresholds: Threshold[] = [
  { key: 'temperature_fire_c', value: 58, updated_at: new Date().toISOString() },
  { key: 'smoke_warning_ppm', value: 900, updated_at: new Date().toISOString() },
];

interface FetchOptions {
  devices?: Device[];
  readings?: Reading[];
  alerts?: Alert[];
  stats?: Stats;
  thresholds?: Threshold[];
  fail?: boolean;
}

function stubFetch(options: FetchOptions = {}) {
  const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (options.fail) throw new TypeError('network down');

    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });

    if (url.includes('/api/devices')) return json(options.devices ?? []);
    if (url.includes('/api/readings')) return json(options.readings ?? []);
    if (url.includes('/api/alerts')) return json(options.alerts ?? []);
    if (url.includes('/api/stats')) return json(options.stats ?? stats);
    if (url.includes('/api/thresholds')) {
      if (init?.method === 'PUT') {
        const body = JSON.parse(String(init.body)) as { thresholds: Record<string, number> };
        return json(
          (options.thresholds ?? thresholds).map((row) =>
            row.key in body.thresholds ? { ...row, value: body.thresholds[row.key]! } : row,
          ),
        );
      }
      return json(options.thresholds ?? thresholds);
    }
    return json({});
  });
  globalThis.fetch = handler as unknown as typeof fetch;
  return handler;
}

beforeEach(() => {
  MockWebSocket.reset();
});

/**
 * Render the whole app and wait for every independent async load to settle.
 *
 * `ThresholdConfig` fetches on its own, separately from `useLiveData`. If a
 * test asserts and finishes before that request resolves, the resulting
 * setState lands outside act() and React warns. Waiting for the panel to reach
 * a terminal state (loaded or errored) keeps every update inside act.
 */
/**
 * App reads session state from context, so every render needs the provider.
 * Signed out is the default here: it is what a first-time visitor sees, and
 * the whole dashboard has to work in that state.
 */
function renderWithAuth() {
  return render(
    <AuthProvider>
      <App />
    </AuthProvider>,
  );
}

async function renderApp() {
  const utils = renderWithAuth();
  await waitFor(() => {
    const settled =
      screen.queryByTestId('threshold-config') ?? screen.queryByTestId('thresholds-error');
    expect(settled).not.toBeNull();
  });
  return utils;
}

// --------------------------------------------------------------------------
// Loading / empty / error states
// --------------------------------------------------------------------------

describe('App states', () => {
  it('renders the empty state when no devices have reported', async () => {
    stubFetch();
    await renderApp();
    expect(await screen.findByTestId('devices-empty')).toBeInTheDocument();
    expect(screen.getByText(/no devices reporting/i)).toBeInTheDocument();
  });

  it('renders the empty alert state when there are no alerts', async () => {
    stubFetch();
    await renderApp();
    expect(await screen.findByTestId('alerts-empty')).toBeInTheDocument();
  });

  it('surfaces a backend outage instead of rendering a blank page', async () => {
    stubFetch({ fail: true });
    await renderApp();
    // Scoped to the page-level banner: the threshold panel raises its own
    // `role="alert"` in this scenario, so an unscoped role query is ambiguous.
    const banner = await screen.findByTestId('load-error');
    expect(banner).toHaveAttribute('role', 'alert');
    expect(banner).toHaveTextContent(/cannot reach the fireprotect api/i);
  });

  it('renders devices and telemetry once loaded', async () => {
    stubFetch({ devices: [device], readings: [makeReading()], stats });
    await renderApp();
    expect(await screen.findByTestId('device-card-esp32-sim-01')).toBeInTheDocument();
    expect(screen.getByTestId('sensor-charts')).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Status derivation
// --------------------------------------------------------------------------

describe('status derivation', () => {
  it('shows ALL SYSTEMS SAFE when every device is safe', async () => {
    stubFetch({ devices: [device], readings: [makeReading()], stats });
    await renderApp();
    await waitFor(() =>
      expect(screen.getByTestId('hero-headline')).toHaveTextContent(/all systems safe/i),
    );
  });

  it('escalates the hero to FIRE when any device reports fire', async () => {
    stubFetch({
      devices: [device],
      readings: [makeReading({ server_status: 'FIRE', risk_score: 95 })],
      stats,
    });
    await renderApp();
    await waitFor(() =>
      expect(screen.getByTestId('hero-headline')).toHaveTextContent(/fire detected/i),
    );
  });

  it('states the status in words, not colour alone', async () => {
    stubFetch({
      devices: [device],
      readings: [makeReading({ server_status: 'WARNING' })],
      stats,
    });
    await renderApp();
    await waitFor(() =>
      expect(screen.getByTestId('hero-status')).toHaveTextContent(/elevated risk/i),
    );
  });
});

// --------------------------------------------------------------------------
// WebSocket
// --------------------------------------------------------------------------

describe('live updates', () => {
  it('opens a websocket and reports Live once connected', async () => {
    stubFetch({ devices: [device], readings: [makeReading()], stats });
    await renderApp();

    await waitFor(() => expect(MockWebSocket.last).toBeDefined());
    act(() => MockWebSocket.last!.open());

    await waitFor(() =>
      expect(screen.getByTestId('connection-status')).toHaveTextContent(/live/i),
    );
  });

  it('applies a reading pushed over the websocket', async () => {
    stubFetch({ devices: [device], readings: [makeReading()], stats });
    await renderApp();
    await waitFor(() => expect(MockWebSocket.last).toBeDefined());
    act(() => MockWebSocket.last!.open());

    act(() => MockWebSocket.last!.emit('reading', makeReading({ id: 2, server_status: 'FIRE' })));

    await waitFor(() =>
      expect(screen.getByTestId('hero-headline')).toHaveTextContent(/fire detected/i),
    );
  });

  it('shows Reconnecting after the socket drops', async () => {
    stubFetch({ devices: [device], readings: [makeReading()], stats });
    await renderApp();
    await waitFor(() => expect(MockWebSocket.last).toBeDefined());
    act(() => MockWebSocket.last!.open());
    act(() => MockWebSocket.last!.close());

    await waitFor(() =>
      expect(screen.getByTestId('connection-status')).toHaveTextContent(/reconnecting/i),
    );
  });

  it('ignores a malformed frame without breaking the connection', async () => {
    stubFetch({ devices: [device], readings: [makeReading()], stats });
    await renderApp();
    await waitFor(() => expect(MockWebSocket.last).toBeDefined());
    const socket = MockWebSocket.last!;
    act(() => socket.open());
    // Wait for the open to flush before asserting it *stays* open - otherwise
    // the assertion races the state update rather than testing the frame.
    await waitFor(() =>
      expect(screen.getByTestId('connection-status')).toHaveTextContent(/live/i),
    );

    act(() => socket.onmessage?.({ data: 'not json{{{' }));

    expect(screen.getByTestId('connection-status')).toHaveTextContent(/live/i);
  });

  it('adds a device pushed over the websocket', async () => {
    stubFetch({ devices: [], readings: [], stats });
    await renderApp();
    await waitFor(() => expect(MockWebSocket.last).toBeDefined());
    act(() => MockWebSocket.last!.open());

    act(() => MockWebSocket.last!.emit('device', { ...device, id: 'esp32-new' }));

    expect(await screen.findByTestId('device-card-esp32-new')).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Device selection
// --------------------------------------------------------------------------

describe('device selection', () => {
  it('toggles selection on click', async () => {
    const user = userEvent.setup();
    stubFetch({ devices: [device], readings: [makeReading()], stats });
    await renderApp();

    const card = await screen.findByTestId('device-card-esp32-sim-01');
    expect(card).toHaveAttribute('aria-pressed', 'false');

    await user.click(card);
    expect(card).toHaveAttribute('aria-pressed', 'true');

    await user.click(card);
    expect(card).toHaveAttribute('aria-pressed', 'false');
  });
});

// --------------------------------------------------------------------------
// Risk gauge
// --------------------------------------------------------------------------

describe('RiskGauge', () => {
  it('renders an accessible label with the score', () => {
    render(<RiskGauge score={62} status="WARNING" />);
    expect(screen.getByRole('img')).toHaveAccessibleName(/62 out of 100/i);
  });

  it('omits the value arc at zero rather than drawing a degenerate path', () => {
    render(<RiskGauge score={0} status="SAFE" />);
    expect(screen.queryByTestId('risk-gauge-value')).not.toBeInTheDocument();
  });

  it('clamps an out-of-range score', () => {
    render(<RiskGauge score={999} status="FIRE" />);
    expect(screen.getByRole('img')).toHaveAccessibleName(/100 out of 100/i);
  });

  it('survives a NaN score without emitting an invalid path', () => {
    render(<RiskGauge score={Number.NaN} status="SAFE" />);
    expect(screen.getByRole('img')).toHaveAccessibleName(/0 out of 100/i);
  });
});

// --------------------------------------------------------------------------
// Alert history
// --------------------------------------------------------------------------

describe('AlertHistory', () => {
  it('renders an alert with its severity and message', () => {
    render(
      <AlertHistory alerts={[makeAlert()]} onAcknowledge={vi.fn()} onResolve={vi.fn()} />,
    );
    const item = screen.getByTestId('alert-10');
    expect(within(item).getByText('FIRE')).toBeInTheDocument();
    expect(within(item).getByText(/flame detected/i)).toBeInTheDocument();
  });

  it('acknowledges an alert', async () => {
    const user = userEvent.setup();
    const onAcknowledge = vi.fn().mockResolvedValue(undefined);
    render(
      <AlertHistory alerts={[makeAlert()]} onAcknowledge={onAcknowledge} onResolve={vi.fn()} />,
    );
    await user.click(screen.getByRole('button', { name: /acknowledge/i }));
    expect(onAcknowledge).toHaveBeenCalledWith(10);
  });

  it('resolves an alert', async () => {
    const user = userEvent.setup();
    const onResolve = vi.fn().mockResolvedValue(undefined);
    render(
      <AlertHistory alerts={[makeAlert()]} onAcknowledge={vi.fn()} onResolve={onResolve} />,
    );
    await user.click(screen.getByRole('button', { name: /resolve/i }));
    expect(onResolve).toHaveBeenCalledWith(10);
  });

  it('reports a failed action instead of failing silently', async () => {
    const user = userEvent.setup();
    const onAcknowledge = vi.fn().mockRejectedValue(new Error('server exploded'));
    render(
      <AlertHistory alerts={[makeAlert()]} onAcknowledge={onAcknowledge} onResolve={vi.fn()} />,
    );
    await user.click(screen.getByRole('button', { name: /acknowledge/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/server exploded/i);
  });

  it('hides the acknowledge button once acknowledged', () => {
    render(
      <AlertHistory
        alerts={[makeAlert({ acknowledged: true })]}
        onAcknowledge={vi.fn()}
        onResolve={vi.fn()}
      />,
    );
    expect(screen.queryByRole('button', { name: /acknowledge/i })).not.toBeInTheDocument();
  });

  it('marks a resolved alert and hides Resolve', () => {
    render(
      <AlertHistory
        alerts={[makeAlert({ resolved_at: new Date().toISOString() })]}
        onAcknowledge={vi.fn()}
        onResolve={vi.fn()}
      />,
    );
    expect(screen.getByText(/resolved/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^resolve$/i })).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Threshold config
// --------------------------------------------------------------------------

describe('ThresholdConfig', () => {
  it('loads and renders every threshold', async () => {
    stubFetch();
    render(<ThresholdConfig />);
    expect(await screen.findByTestId('threshold-config')).toBeInTheDocument();
    expect(screen.getByLabelText(/temperature fire/i)).toHaveValue(58);
  });

  it('disables Save until something changes', async () => {
    stubFetch();
    render(<ThresholdConfig />);
    await screen.findByTestId('threshold-config');
    expect(screen.getByRole('button', { name: /save thresholds/i })).toBeDisabled();
  });

  it('saves only the changed field', async () => {
    const user = userEvent.setup();
    const handler = stubFetch();
    render(<ThresholdConfig />);
    await screen.findByTestId('threshold-config');

    const input = screen.getByLabelText(/temperature fire/i);
    await user.clear(input);
    await user.type(input, '65');
    await user.click(screen.getByRole('button', { name: /save thresholds/i }));

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/saved/i));

    const putCall = handler.mock.calls.find(([, init]) => init?.method === 'PUT');
    expect(putCall).toBeDefined();
    const body = JSON.parse(String(putCall![1]!.body)) as {
      thresholds: Record<string, number>;
    };
    expect(body.thresholds).toEqual({ temperature_fire_c: 65 });
  });

  it('rejects a negative value client-side', async () => {
    const user = userEvent.setup();
    stubFetch();
    render(<ThresholdConfig />);
    await screen.findByTestId('threshold-config');

    const input = screen.getByLabelText(/temperature fire/i);
    await user.clear(input);
    await user.type(input, '-5');

    expect(await screen.findByText(/zero or greater/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save thresholds/i })).toBeDisabled();
  });

  it('rejects an empty value', async () => {
    const user = userEvent.setup();
    stubFetch();
    render(<ThresholdConfig />);
    await screen.findByTestId('threshold-config');

    await user.clear(screen.getByLabelText(/temperature fire/i));
    expect(await screen.findByText(/required/i)).toBeInTheDocument();
  });

  it('resets the draft back to loaded values', async () => {
    const user = userEvent.setup();
    stubFetch();
    render(<ThresholdConfig />);
    await screen.findByTestId('threshold-config');

    const input = screen.getByLabelText(/temperature fire/i);
    await user.clear(input);
    await user.type(input, '99');
    await user.click(screen.getByRole('button', { name: /reset/i }));

    expect(input).toHaveValue(58);
  });
});

// --------------------------------------------------------------------------
// Assets and accessibility
// --------------------------------------------------------------------------

describe('assets', () => {
  it('references only local asset paths, never an external URL', async () => {
    stubFetch({ devices: [], readings: [], stats });
    const { container } = renderWithAuth();
    await screen.findByTestId('devices-empty');

    for (const img of Array.from(container.querySelectorAll('img'))) {
      const src = img.getAttribute('src') ?? '';
      expect(src.startsWith('/assets/')).toBe(true);
      expect(src).not.toMatch(/^https?:/);
      expect(src).not.toMatch(/placehold|unsplash|picsum|pexels/i);
    }
  });

  it('gives every empty-state image explicit dimensions to prevent layout shift', () => {
    const { container } = render(
      <EmptyState image="/assets/empty-state-no-devices.webp" title="T" body="B" />,
    );
    const img = container.querySelector('img')!;
    expect(img).toHaveAttribute('width');
    expect(img).toHaveAttribute('height');
    expect(img).toHaveAttribute('loading', 'lazy');
  });

  it('marks decorative images as hidden from assistive technology', () => {
    const { container } = render(
      <EmptyState image="/assets/empty-state-no-alerts.webp" title="T" body="B" />,
    );
    const img = container.querySelector('img')!;
    expect(img).toHaveAttribute('alt', '');
    expect(img).toHaveAttribute('aria-hidden', 'true');
  });
});

describe('accessibility', () => {
  it('labels every landmark section', async () => {
    stubFetch({ devices: [device], readings: [makeReading()], stats });
    const { container } = renderWithAuth();
    await screen.findByTestId('device-card-esp32-sim-01');

    for (const section of Array.from(container.querySelectorAll('section'))) {
      const labelled =
        section.hasAttribute('aria-labelledby') || section.hasAttribute('aria-label');
      expect(labelled).toBe(true);
    }
  });

  it('exposes exactly one h1', async () => {
    stubFetch({ devices: [device], readings: [makeReading()], stats });
    await renderApp();
    await screen.findByTestId('device-card-esp32-sim-01');
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });

  it('announces connection state politely', async () => {
    stubFetch({ devices: [], readings: [], stats });
    await renderApp();
    const status = await screen.findByTestId('connection-status');
    expect(status).toHaveAttribute('aria-live', 'polite');
  });
});
