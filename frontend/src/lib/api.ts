/** Typed REST client. Every network call in the app goes through here. */

import type {
  Alert,
  CameraFrameMetrics,
  Device,
  Health,
  NotificationStatus,
  Reading,
  Stats,
  TemperatureEvent,
  Threshold,
  TokenResponse,
  User,
} from '@/types';

/**
 * Session token, held in a module variable and mirrored to localStorage.
 *
 * In memory so every request reads it without touching storage; mirrored so a
 * refresh does not sign the user out. localStorage rather than a cookie
 * because the token is sent as an `Authorization` header, which is immune to
 * CSRF by construction — a cross-site form post cannot set that header.
 */
const TOKEN_KEY = 'fireprotect.token';

let authToken: string | null = null;

function readStoredToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    // Private-browsing modes and blocked third-party storage throw here.
    // Losing persistence is acceptable; crashing the app is not.
    return null;
  }
}

export function getToken(): string | null {
  if (authToken === null) authToken = readStoredToken();
  return authToken;
}

export function setToken(token: string | null): void {
  authToken = token;
  try {
    if (token === null) window.localStorage.removeItem(TOKEN_KEY);
    else window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* memory-only session */
  }
}

/**
 * Base URL. Empty by default so requests are same-origin and picked up by the
 * Vite dev proxy; override with VITE_API_BASE when the dashboard is served
 * from a different host than the API.
 */
export const API_BASE = (import.meta.env?.VITE_API_BASE as string | undefined) ?? '';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly url: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;
  const token = getToken();
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        ...(token !== null ? { Authorization: `Bearer ${token}` } : {}),
        ...init?.headers,
      },
    });
  } catch (cause) {
    // A network-level failure (backend down, DNS, CORS) throws before we get a
    // Response. Surfacing it as ApiError means callers only handle one type.
    throw new ApiError(
      `Cannot reach the FireProtect API at ${url}. Is the backend running?`,
      0,
      url,
    );
  }

  if (!response.ok) {
    // An expired or revoked token should log the user out rather than leave
    // the UI in a state where every request silently 401s.
    if (response.status === 401 && getToken() !== null) {
      setToken(null);
      window.dispatchEvent(new CustomEvent('fireprotect:signed-out'));
    }
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') {
        detail = body.detail;
      } else if (Array.isArray(body.detail)) {
        // FastAPI validation errors: surface the first message rather than a
        // wall of JSON the user cannot act on.
        const first = body.detail[0] as { msg?: unknown } | undefined;
        detail = typeof first?.msg === 'string' ? first.msg : 'Invalid input.';
      } else if (body.detail) {
        detail = JSON.stringify(body.detail);
      }
    } catch {
      // Body was not JSON; the status text is the best we have.
    }
    throw new ApiError(detail, response.status, url);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<Health>('/api/health'),
  stats: () => request<Stats>('/api/stats'),

  devices: () => request<Device[]>('/api/devices'),

  readings: (params?: { deviceId?: string; limit?: number; sinceMinutes?: number }) => {
    const query = new URLSearchParams();
    if (params?.deviceId) query.set('device_id', params.deviceId);
    if (params?.limit) query.set('limit', String(params.limit));
    if (params?.sinceMinutes) query.set('since_minutes', String(params.sinceMinutes));
    const suffix = query.toString() ? `?${query}` : '';
    return request<Reading[]>(`/api/readings${suffix}`);
  },

  alerts: (params?: { deviceId?: string; onlyOpen?: boolean; limit?: number }) => {
    const query = new URLSearchParams();
    if (params?.deviceId) query.set('device_id', params.deviceId);
    if (params?.onlyOpen) query.set('only_open', 'true');
    if (params?.limit) query.set('limit', String(params.limit));
    const suffix = query.toString() ? `?${query}` : '';
    return request<Alert[]>(`/api/alerts${suffix}`);
  },

  acknowledgeAlert: (id: number) =>
    request<Alert>(`/api/alerts/${id}/acknowledge`, { method: 'POST' }),

  resolveAlert: (id: number) =>
    request<Alert>(`/api/alerts/${id}/resolve`, { method: 'POST' }),

  thresholds: () => request<Threshold[]>('/api/thresholds'),

  updateThresholds: (thresholds: Record<string, number>) =>
    request<Threshold[]>('/api/thresholds', {
      method: 'PUT',
      body: JSON.stringify({ thresholds }),
    }),

  // --- camera node ------------------------------------------------------

  /**
   * Publish one analysed frame. Three floats plus optional weather — never
   * image data.
   */
  cameraTelemetry: (payload: {
    device_id: string;
    location?: string;
    temperature_c?: number | null;
    humidity_pct?: number | null;
    pm2_5_ugm3?: number | null;
    pm10_ugm3?: number | null;
    us_aqi?: number | null;
  } & CameraFrameMetrics) => request<Reading>('/api/camera/telemetry', {
    method: 'POST',
    body: JSON.stringify(payload),
  }),

  // --- accounts ---------------------------------------------------------

  register: (email: string, password: string, displayName: string) =>
    request<TokenResponse>('/api/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password, display_name: displayName }),
    }),

  login: (email: string, password: string) =>
    request<TokenResponse>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),

  me: () => request<User>('/api/auth/me'),

  updateEmergencyContact: (payload: {
    emergency_email?: string;
    emergency_name?: string;
    temperature_limit_c?: number;
    notifications_enabled?: boolean;
  }) =>
    request<User>('/api/auth/emergency-contact', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  notificationStatus: () =>
    request<NotificationStatus>('/api/auth/notification-status'),

  temperatureEvents: (limit = 100) =>
    request<TemperatureEvent[]>(`/api/temperature-events?limit=${limit}`),

  claimDevice: (deviceId: string) =>
    request<Device>('/api/devices/claim', {
      method: 'POST',
      body: JSON.stringify({ device_id: deviceId }),
    }),
};

/**
 * WebSocket URL, derived from the page origin so it works behind the dev
 * proxy, in Docker, and over TLS without configuration.
 */
export function websocketUrl(): string {
  if (API_BASE) {
    return `${API_BASE.replace(/^http/, 'ws')}/ws`;
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
}
