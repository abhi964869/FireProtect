/**
 * Outdoor conditions lookup.
 *
 * The property under test throughout: a missing or nonsensical reading becomes
 * `null`, never a plausible-looking number. For a fire product, a fabricated
 * "12 µg/m³" is worse than a blank, because a blank cannot be trusted by
 * mistake.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AmbientMonitor, aqiBand, fetchConditions, requestPosition } from '@/lib/ambient';

const COORDS = { latitude: 28.61, longitude: 77.21 };

function jsonResponse(body: unknown, ok = true) {
  return new Response(JSON.stringify(body), {
    status: ok ? 200 : 500,
    headers: { 'Content-Type': 'application/json' },
  });
}

const GOOD_WEATHER = {
  current: { temperature_2m: 31.4, relative_humidity_2m: 62 },
};
const GOOD_AIR = {
  current: { pm2_5: 48.2, pm10: 96.5, us_aqi: 132 },
};

/** Route by URL so weather and air quality can fail independently. */
function stub(weather: unknown, air: unknown, opts: { weatherOk?: boolean; airOk?: boolean } = {}) {
  const handler = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('air-quality')) return jsonResponse(air, opts.airOk ?? true);
    return jsonResponse(weather, opts.weatherOk ?? true);
  });
  globalThis.fetch = handler as unknown as typeof fetch;
  return handler;
}

beforeEach(() => {
  vi.stubGlobal('navigator', {
    ...navigator,
    geolocation: {
      getCurrentPosition: (success: PositionCallback) => {
        success({
          coords: { latitude: COORDS.latitude, longitude: COORDS.longitude },
        } as GeolocationPosition);
      },
    },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// --------------------------------------------------------------------------
// AQI bands
// --------------------------------------------------------------------------

describe('aqiBand', () => {
  it('maps the EPA breakpoints', () => {
    expect(aqiBand(20).label).toBe('Good');
    expect(aqiBand(75).label).toBe('Moderate');
    expect(aqiBand(120).label).toMatch(/sensitive/i);
    expect(aqiBand(180).label).toBe('Unhealthy');
    expect(aqiBand(250).label).toBe('Very unhealthy');
    expect(aqiBand(400).label).toBe('Hazardous');
  });

  it('reports unknown rather than guessing a band', () => {
    expect(aqiBand(null).label).toBe('Unknown');
  });

  it('uses inclusive upper bounds', () => {
    expect(aqiBand(50).label).toBe('Good');
    expect(aqiBand(51).label).toBe('Moderate');
  });
});

// --------------------------------------------------------------------------
// fetchConditions
// --------------------------------------------------------------------------

describe('fetchConditions', () => {
  it('returns every real measurement', async () => {
    stub(GOOD_WEATHER, GOOD_AIR);
    const result = await fetchConditions(COORDS);
    expect(result).toMatchObject({
      temperature_c: 31.4,
      humidity_pct: 62,
      pm2_5_ugm3: 48.2,
      pm10_ugm3: 96.5,
      us_aqi: 132,
    });
  });

  it('keeps weather when air quality fails', async () => {
    // Two independent services; a partial answer is genuinely useful.
    stub(GOOD_WEATHER, {}, { airOk: false });
    const result = await fetchConditions(COORDS);
    expect(result?.temperature_c).toBe(31.4);
    expect(result?.pm2_5_ugm3).toBeNull();
  });

  it('keeps air quality when weather fails', async () => {
    stub({}, GOOD_AIR, { weatherOk: false });
    const result = await fetchConditions(COORDS);
    expect(result?.us_aqi).toBe(132);
    expect(result?.temperature_c).toBeNull();
  });

  it('returns null when both fail', async () => {
    stub({}, {}, { weatherOk: false, airOk: false });
    expect(await fetchConditions(COORDS)).toBeNull();
  });

  it('returns null when the response has no usable field', async () => {
    // A 200 with an unexpected shape must not render as a row of dashes
    // presented as though it were data.
    stub({ current: {} }, { current: {} });
    expect(await fetchConditions(COORDS)).toBeNull();
  });

  it('discards physically impossible values instead of forwarding them', async () => {
    stub(
      { current: { temperature_2m: 900, relative_humidity_2m: -5 } },
      { current: { pm2_5: -3, pm10: 1e9, us_aqi: 132 } },
    );
    const result = await fetchConditions(COORDS);
    expect(result?.temperature_c).toBeNull();
    expect(result?.humidity_pct).toBeNull();
    expect(result?.pm2_5_ugm3).toBeNull();
    expect(result?.pm10_ugm3).toBeNull();
    expect(result?.us_aqi).toBe(132);
  });

  it('discards non-numeric values', async () => {
    stub(
      { current: { temperature_2m: 'warm', relative_humidity_2m: null } },
      GOOD_AIR,
    );
    const result = await fetchConditions(COORDS);
    expect(result?.temperature_c).toBeNull();
    expect(result?.humidity_pct).toBeNull();
  });

  it('survives a network throw', async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new TypeError('offline');
    }) as unknown as typeof fetch;
    expect(await fetchConditions(COORDS)).toBeNull();
  });

  it('survives a malformed body', async () => {
    globalThis.fetch = vi.fn(
      async () => new Response('not json', { status: 200 }),
    ) as unknown as typeof fetch;
    expect(await fetchConditions(COORDS)).toBeNull();
  });

  it('queries both services in parallel', async () => {
    const handler = stub(GOOD_WEATHER, GOOD_AIR);
    await fetchConditions(COORDS);
    const urls = handler.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.includes('air-quality'))).toBe(true);
    expect(urls.some((url) => url.includes('forecast'))).toBe(true);
  });

  it('sends coarse coordinates only', async () => {
    const handler = stub(GOOD_WEATHER, GOOD_AIR);
    await fetchConditions({ latitude: 28.6139391, longitude: 77.2090212 });
    const url = String(handler.mock.calls[0]?.[0]);
    // Three decimals is ~100 m. Enough for a weather station lookup, and not
    // a precise record of where somebody lives.
    expect(url).toContain('latitude=28.614');
    expect(url).not.toContain('28.6139391');
  });
});

// --------------------------------------------------------------------------
// requestPosition
// --------------------------------------------------------------------------

describe('requestPosition', () => {
  it('resolves coordinates when granted', async () => {
    expect(await requestPosition()).toEqual(COORDS);
  });

  it('resolves null when denied, rather than rejecting', async () => {
    vi.stubGlobal('navigator', {
      ...navigator,
      geolocation: {
        getCurrentPosition: (_ok: PositionCallback, fail: PositionErrorCallback) => {
          fail({ code: 1, message: 'denied' } as GeolocationPositionError);
        },
      },
    });
    expect(await requestPosition()).toBeNull();
  });

  it('resolves null when geolocation is unavailable', async () => {
    vi.stubGlobal('navigator', { ...navigator, geolocation: undefined });
    expect(await requestPosition()).toBeNull();
  });
});

// --------------------------------------------------------------------------
// AmbientMonitor
// --------------------------------------------------------------------------

describe('AmbientMonitor', () => {
  it('caches so it does not refetch on every publish tick', async () => {
    const handler = stub(GOOD_WEATHER, GOOD_AIR);
    const monitor = new AmbientMonitor();
    await monitor.refresh();
    await monitor.refresh();
    await monitor.refresh();
    // Two calls total: one weather, one air quality. Not six.
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('shares one request between concurrent callers', async () => {
    const handler = stub(GOOD_WEATHER, GOOD_AIR);
    const monitor = new AmbientMonitor();
    await Promise.all([monitor.refresh(), monitor.refresh(), monitor.refresh()]);
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('records that permission was denied', async () => {
    vi.stubGlobal('navigator', {
      ...navigator,
      geolocation: {
        getCurrentPosition: (_ok: PositionCallback, fail: PositionErrorCallback) => {
          fail({ code: 1, message: 'denied' } as GeolocationPositionError);
        },
      },
    });
    stub(GOOD_WEATHER, GOOD_AIR);
    const monitor = new AmbientMonitor();
    expect(await monitor.refresh()).toBeNull();
    expect(monitor.permissionDenied).toBe(true);
    expect(monitor.available).toBe(false);
  });

  it('never asks for location twice once granted', async () => {
    const getCurrentPosition = vi.fn((success: PositionCallback) => {
      success({ coords: COORDS } as GeolocationPosition);
    });
    vi.stubGlobal('navigator', { ...navigator, geolocation: { getCurrentPosition } });
    stub(GOOD_WEATHER, GOOD_AIR);
    const monitor = new AmbientMonitor();
    await monitor.refresh();
    monitor.reset();
    await monitor.refresh();
    expect(getCurrentPosition).toHaveBeenCalledTimes(2); // reset clears consent
  });

  it('starts empty', () => {
    const monitor = new AmbientMonitor();
    expect(monitor.current).toBeNull();
    expect(monitor.available).toBe(false);
    expect(monitor.permissionDenied).toBe(false);
  });
});
