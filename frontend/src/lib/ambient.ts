/**
 * Real ambient conditions for wherever the visitor is.
 *
 * ## What this is, and what it is not
 *
 * A phone has no hygrometer, no gas sensor and no particulate counter. Those
 * are not missing browser permissions — the hardware is not in the device, so
 * no API could expose them. Anything claiming to read room humidity or smoke
 * ppm from a phone is inventing numbers.
 *
 * What *is* real and obtainable: the visitor's coarse location, and from that,
 * genuine measurements taken by public weather and air-quality monitoring
 * networks near them. Temperature, humidity, PM2.5, PM10 and AQI are all real
 * readings from real instruments — just instruments outdoors rather than in
 * the room.
 *
 * PM2.5 deserves special mention because it is the honest analogue of "smoke":
 * airborne particulate matter measured in µg/m³, and the quantity that spikes
 * during a wildfire. It is genuinely informative. It is still not a substitute
 * for a detector in the room, and the UI says so.
 *
 * ## Why Open-Meteo
 *
 * No API key. That matters for a client-side call: any key shipped in a bundle
 * is a public key, so a service requiring one is a service whose quota belongs
 * to whoever reads your JavaScript.
 *
 * ## Failure behaviour
 *
 * Everything degrades to `null`. Permission denied, offline, service down,
 * malformed response — all produce "unknown", never a fabricated value. That
 * is the whole point: a blank is honest, a plausible number is not.
 */

const WEATHER_ENDPOINT = 'https://api.open-meteo.com/v1/forecast';
const AIR_ENDPOINT = 'https://air-quality-api.open-meteo.com/v1/air-quality';

const GEOLOCATION_TIMEOUT_MS = 10_000;
const FETCH_TIMEOUT_MS = 8_000;

/**
 * Conditions move on the scale of tens of minutes, and air-quality stations
 * publish hourly. Re-fetching every two seconds would be pure waste and would
 * rate-limit a free shared endpoint for everyone.
 */
const REFRESH_INTERVAL_MS = 10 * 60 * 1000;

export interface AmbientConditions {
  /** Outdoor air temperature, °C. */
  temperature_c: number | null;
  /** Outdoor relative humidity, %. */
  humidity_pct: number | null;
  /** Fine particulate, µg/m³ — the real-world analogue of smoke. */
  pm2_5_ugm3: number | null;
  /** Coarse particulate, µg/m³. */
  pm10_ugm3: number | null;
  /** US EPA Air Quality Index, 0-500. */
  us_aqi: number | null;
  /** When it was fetched, so staleness can be shown honestly. */
  fetchedAt: number;
}

export const EMPTY_CONDITIONS: AmbientConditions = {
  temperature_c: null,
  humidity_pct: null,
  pm2_5_ugm3: null,
  pm10_ugm3: null,
  us_aqi: null,
  fetchedAt: 0,
};

/** Coarse coordinates. Held in memory only; never sent to the backend. */
export interface Coordinates {
  latitude: number;
  longitude: number;
}

function num(value: unknown, min: number, max: number): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  // Out-of-envelope values are treated as unknown rather than forwarded. The
  // backend would reject them anyway; a quiet "unknown" beats a failed POST.
  if (value < min || value > max) return null;
  return value;
}

/** EPA AQI bands. Used for the label, not for any alerting decision. */
export function aqiBand(aqi: number | null): {
  label: string;
  detail: string;
} {
  if (aqi === null) return { label: 'Unknown', detail: 'No reading available' };
  if (aqi <= 50) return { label: 'Good', detail: 'Air quality poses little risk' };
  if (aqi <= 100)
    return { label: 'Moderate', detail: 'Acceptable, but unusually sensitive people may react' };
  if (aqi <= 150)
    return {
      label: 'Unhealthy for sensitive groups',
      detail: 'People with lung or heart conditions should limit exertion outdoors',
    };
  if (aqi <= 200)
    return { label: 'Unhealthy', detail: 'Everyone may begin to feel effects' };
  if (aqi <= 300)
    return { label: 'Very unhealthy', detail: 'Health warnings; avoid outdoor exertion' };
  return { label: 'Hazardous', detail: 'Emergency conditions; stay indoors' };
}

/** Ask the browser for a position. Resolves to null rather than throwing. */
export async function requestPosition(): Promise<Coordinates | null> {
  if (typeof navigator.geolocation?.getCurrentPosition !== 'function') {
    return null;
  }
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value: Coordinates | null) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    // Belt and braces: some browsers invoke neither callback when a permission
    // prompt is dismissed rather than answered, which would otherwise leave the
    // caller awaiting forever.
    const timer = window.setTimeout(() => {
      finish(null);
    }, GEOLOCATION_TIMEOUT_MS);

    navigator.geolocation.getCurrentPosition(
      (position) => {
        window.clearTimeout(timer);
        finish({
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
        });
      },
      () => {
        window.clearTimeout(timer);
        finish(null);
      },
      {
        // Low accuracy deliberately. Weather and air quality do not vary over
        // a few hundred metres, so requesting GPS precision would cost battery
        // and ask the user for more than the feature needs.
        enableHighAccuracy: false,
        timeout: GEOLOCATION_TIMEOUT_MS,
        maximumAge: REFRESH_INTERVAL_MS,
      },
    );
  });
}

async function getJson(url: string): Promise<Record<string, unknown> | null> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => {
    controller.abort();
  }, FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) return null;
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return null;
  } finally {
    window.clearTimeout(timer);
  }
}

/**
 * Fetch weather and air quality together.
 *
 * The two are independent requests issued in parallel, and either may fail on
 * its own. A partial result is genuinely useful — temperature with no AQI still
 * beats nothing — so this returns whatever came back rather than insisting on
 * all-or-nothing.
 */
export async function fetchConditions(
  coordinates: Coordinates,
): Promise<AmbientConditions | null> {
  const lat = coordinates.latitude.toFixed(3);
  const lon = coordinates.longitude.toFixed(3);

  const [weather, air] = await Promise.all([
    getJson(
      `${WEATHER_ENDPOINT}?latitude=${lat}&longitude=${lon}` +
        '&current=temperature_2m,relative_humidity_2m',
    ),
    getJson(
      `${AIR_ENDPOINT}?latitude=${lat}&longitude=${lon}` +
        '&current=pm2_5,pm10,us_aqi',
    ),
  ]);

  if (weather === null && air === null) return null;

  const w = (weather?.current ?? {}) as Record<string, unknown>;
  const a = (air?.current ?? {}) as Record<string, unknown>;

  const conditions: AmbientConditions = {
    temperature_c: num(w.temperature_2m, -40, 60),
    humidity_pct: num(w.relative_humidity_2m, 0, 100),
    pm2_5_ugm3: num(a.pm2_5, 0, 2000),
    pm10_ugm3: num(a.pm10, 0, 5000),
    us_aqi: num(a.us_aqi, 0, 1000),
    fetchedAt: Date.now(),
  };

  // Every field unusable means the responses were shaped unexpectedly. Report
  // that as "unknown" rather than surfacing a row of dashes as if it were data.
  const anyValue = [
    conditions.temperature_c,
    conditions.humidity_pct,
    conditions.pm2_5_ugm3,
    conditions.pm10_ugm3,
    conditions.us_aqi,
  ].some((value) => value !== null);
  return anyValue ? conditions : null;
}

/**
 * Caches one lookup and refreshes it on a slow timer.
 *
 * Coordinates stay in memory and are never sent to the FireProtect backend —
 * only the resulting measurements are, and only if the visitor allowed the
 * lookup in the first place.
 */
export class AmbientMonitor {
  private conditions: AmbientConditions | null = null;
  private coordinates: Coordinates | null = null;
  private inFlight: Promise<AmbientConditions | null> | null = null;
  /** Distinguishes "not asked yet" from "asked and denied". */
  private denied = false;

  get current(): AmbientConditions | null {
    return this.conditions;
  }

  get permissionDenied(): boolean {
    return this.denied;
  }

  get available(): boolean {
    return this.conditions !== null;
  }

  /** Fetch if empty or stale. Concurrent callers share one request. */
  async refresh(): Promise<AmbientConditions | null> {
    const fresh =
      this.conditions !== null &&
      Date.now() - this.conditions.fetchedAt < REFRESH_INTERVAL_MS;
    if (fresh) return this.conditions;
    if (this.inFlight !== null) return this.inFlight;

    this.inFlight = this.load().finally(() => {
      this.inFlight = null;
    });
    return this.inFlight;
  }

  private async load(): Promise<AmbientConditions | null> {
    if (this.coordinates === null) {
      const position = await requestPosition();
      if (position === null) {
        this.denied = true;
        return null;
      }
      this.denied = false;
      this.coordinates = position;
    }
    const conditions = await fetchConditions(this.coordinates);
    if (conditions !== null) this.conditions = conditions;
    return this.conditions;
  }

  reset(): void {
    this.conditions = null;
    this.coordinates = null;
    this.denied = false;
  }
}
