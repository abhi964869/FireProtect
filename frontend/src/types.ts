/** Shared API types. These mirror the Pydantic schemas in backend/app/schemas.py. */

export type FireStatus = 'SAFE' | 'WARNING' | 'FIRE';

export const FIRE_STATUSES: readonly FireStatus[] = ['SAFE', 'WARNING', 'FIRE'];

/** Severity ordering, used to pick the worst status across devices. */
export const STATUS_RANK: Record<FireStatus, number> = {
  SAFE: 0,
  WARNING: 1,
  FIRE: 2,
};

/**
 * Which family of sensor produced a reading. Branch on this before reading any
 * channel: a camera has no gas sensor and an ESP32 has no flicker score, and
 * the corresponding fields are `null`, not zero.
 */
export type SensorKind = 'hardware' | 'camera';

export interface Reading {
  id: number;
  device_id: string;
  recorded_at: string;
  sensor_kind: SensorKind;

  /** Gas and thermal channels. `null` on a camera node. */
  temperature_c: number | null;
  humidity_pct: number | null;
  smoke_ppm: number | null;
  air_quality_ppm: number | null;
  flame_analog_volts: number | null;
  flame_detected: number;

  /** Camera channels, all 0..1. `null` on a hardware node. */
  flame_ratio: number | null;
  luminance: number | null;
  haze_index: number | null;
  flicker: number | null;

  /**
   * Outdoor air near the device, from public monitoring networks. Real
   * instrument readings, but of outdoor air — never of the room.
   */
  pm2_5_ugm3: number | null;
  pm10_ugm3: number | null;
  us_aqi: number | null;

  temp_rate_c_per_min: number;
  smoke_rate_ppm_per_min: number;
  heat_index_c: number;
  device_status: FireStatus;
  server_status: FireStatus;
  fire_probability: number;
  risk_score: number;
}

export interface Device {
  id: string;
  name: string;
  location: string;
  kind: SensorKind;
  firmware_version: string;
  first_seen: string;
  last_seen: string;
  online: boolean;
}

export interface User {
  id: number;
  email: string;
  display_name: string;
  created_at: string;
  emergency_email: string;
  emergency_name: string;
  temperature_limit_c: number;
  notifications_enabled: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: 'bearer';
  expires_in: number;
  user: User;
}

export interface TemperatureEvent {
  id: number;
  device_id: string;
  started_at: string;
  ended_at: string | null;
  trigger_temperature_c: number;
  peak_temperature_c: number;
  threshold_c: number;
  sensor_kind: SensorKind;
  notified: boolean;
  notify_error: string;
}

export interface NotificationStatus {
  transport: 'resend' | 'smtp' | 'none';
  configured: boolean;
  contact_set: boolean;
}

/** What the browser measures from one video frame. All dimensionless, 0..1. */
export interface CameraFrameMetrics {
  flame_ratio: number;
  luminance: number;
  haze_index: number;
}

export interface Alert {
  id: number;
  device_id: string;
  severity: FireStatus;
  message: string;
  triggered_at: string;
  resolved_at: string | null;
  acknowledged: boolean;
  temperature_c: number;
  smoke_ppm: number;
  fire_probability: number;
}

export interface Threshold {
  key: string;
  value: number;
  updated_at: string;
}

export interface Stats {
  total_devices: number;
  online_devices: number;
  total_readings: number;
  open_alerts: number;
  alerts_24h: number;
  current_status: FireStatus;
  max_fire_probability: number;
}

export interface Health {
  status: 'ok' | 'degraded';
  database: boolean;
  mqtt_connected: boolean;
  model_loaded: boolean;
  thingspeak_pending: number;
  version: string;
}

export type WsMessageType = 'reading' | 'alert' | 'device' | 'stats' | 'hello';

export interface WsMessage {
  type: WsMessageType;
  payload: Record<string, unknown>;
}

export type ConnectionState = 'connecting' | 'open' | 'closed' | 'reconnecting';
