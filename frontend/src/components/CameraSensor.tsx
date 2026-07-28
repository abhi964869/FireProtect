/**
 * Turn the visitor's device into a live sensor node.
 *
 * This is what makes the deployed link do something on a phone. It asks for
 * the camera, analyses frames locally, and publishes three numbers to the
 * backend every couple of seconds — where they go through the same ingest,
 * alerting and WebSocket broadcast as an ESP32.
 *
 * Two things it is careful never to imply:
 *
 * 1. That it is measuring gas or room temperature. It is not, it cannot, and
 *    the copy says so plainly rather than leaving a gap for the user to fill
 *    with an optimistic assumption.
 * 2. That video is being uploaded. It is not; only the derived numbers leave
 *    the device, and the panel states that where the permission is requested,
 *    which is the moment someone actually wants to know.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { api } from '@/lib/api';
import {
  CameraSensor as CameraCapture,
  describeCameraError,
  type CameraAnalysisFrame,
} from '@/lib/cameraSensor';
import type { AmbientConditions, AmbientMonitor } from '@/lib/ambient';
import { useAuth } from '@/lib/useAuth';
import type { FireStatus, Reading } from '@/types';

/** Publish rate. Matches the ESP32's, so server-side rate features line up. */
const PUBLISH_INTERVAL_MS = 2000;

/** Device id, stable across reloads so one phone stays one device. */
const DEVICE_KEY = 'fireprotect.cameraDeviceId';

function stableDeviceId(): string {
  try {
    const existing = window.localStorage.getItem(DEVICE_KEY);
    if (existing !== null && existing.length > 0) return existing;
  } catch {
    /* storage unavailable; fall through to an ephemeral id */
  }
  // crypto.randomUUID is unavailable on older Safari and on plain http, and
  // this must not throw on the path that asks for camera permission.
  const suffix =
    typeof crypto?.randomUUID === 'function'
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(36).slice(2, 10);
  const id = `cam-${suffix}`;
  try {
    window.localStorage.setItem(DEVICE_KEY, id);
  } catch {
    /* ephemeral */
  }
  return id;
}

const STATUS_TEXT: Record<FireStatus, string> = {
  SAFE: 'No fire detected',
  WARNING: 'Something looks off',
  FIRE: 'Flame detected',
};

const STATUS_COLOUR: Record<FireStatus, string> = {
  SAFE: 'text-status-safe',
  WARNING: 'text-status-warning',
  FIRE: 'text-status-fire',
};

interface CameraSensorPanelProps {
  /** Shared with LocalConditions so one permission serves both. */
  monitor: AmbientMonitor;
}

export function CameraSensorPanel({ monitor }: CameraSensorPanelProps) {
  const { user } = useAuth();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const captureRef = useRef<CameraCapture | null>(null);
  const deviceIdRef = useRef<string>('');
  // Held in a ref, not state: the publish loop reads it every tick and putting
  // it in state would re-create the interval on every single frame.
  const latestFrameRef = useRef<CameraAnalysisFrame | null>(null);

  const [running, setRunning] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<CameraAnalysisFrame | null>(null);
  const [reading, setReading] = useState<Reading | null>(null);
  const [publishError, setPublishError] = useState<string | null>(null);
  const [shareWeather, setShareWeather] = useState(false);
  const [weatherNote, setWeatherNote] = useState<string | null>(null);

  const stop = useCallback(() => {
    captureRef.current?.stop();
    captureRef.current = null;
    latestFrameRef.current = null;
    setRunning(false);
    setMetrics(null);
  }, []);

  // Release the camera on unmount. Without this the indicator light stays on
  // after navigating away, which reads as spyware and is a fair reading.
  useEffect(() => () => {
    captureRef.current?.stop();
    captureRef.current = null;
  }, []);

  const start = useCallback(async () => {
    setError(null);
    setPublishError(null);
    setStarting(true);
    const video = videoRef.current;
    if (video === null) {
      setStarting(false);
      return;
    }
    const capture = new CameraCapture();
    try {
      await capture.start(video);
      captureRef.current = capture;
      deviceIdRef.current = deviceIdRef.current || stableDeviceId();
      setRunning(true);
    } catch (cause) {
      capture.stop();
      setError(describeCameraError(cause));
    } finally {
      setStarting(false);
    }
  }, []);

  // Sampling loop. Faster than the publish rate so the on-screen numbers feel
  // live, and so the server sees an average rather than one arbitrary frame.
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => {
      const video = videoRef.current;
      const capture = captureRef.current;
      if (video === null || capture === null) return;
      const frame = capture.sample(video);
      if (frame !== null) {
        latestFrameRef.current = frame;
        setMetrics(frame);
      }
    }, 250);
    return () => {
      window.clearInterval(timer);
    };
  }, [running]);

  // Publish loop.
  useEffect(() => {
    if (!running) return;
    let cancelled = false;

    const publish = async () => {
      const frame = latestFrameRef.current;
      if (frame === null) return;

      // Real outdoor measurements, attached only if the visitor opted in.
      // Absent stays absent — the backend stores NULL, and the UI shows a dash
      // rather than a zero that would read as "clean air".
      let ambient: AmbientConditions | null = null;
      if (shareWeather) {
        ambient = await monitor.refresh();
      }

      try {
        const result = await api.cameraTelemetry({
          device_id: deviceIdRef.current,
          flame_ratio: frame.flame_ratio,
          luminance: frame.luminance,
          haze_index: frame.haze_index,
          location: 'This device',
          temperature_c: ambient?.temperature_c ?? null,
          humidity_pct: ambient?.humidity_pct ?? null,
          pm2_5_ugm3: ambient?.pm2_5_ugm3 ?? null,
          pm10_ugm3: ambient?.pm10_ugm3 ?? null,
          us_aqi: ambient?.us_aqi ?? null,
        });
        if (cancelled) return;
        setReading(result);
        setPublishError(null);
      } catch (cause) {
        if (cancelled) return;
        // Keep sampling: a dropped connection should recover on its own when
        // the network returns, not require the user to re-grant the camera.
        setPublishError(
          cause instanceof Error ? cause.message : 'Could not reach the server.',
        );
      }
    };

    void publish();
    const timer = window.setInterval(() => {
      void publish();
    }, PUBLISH_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [running, shareWeather, monitor]);

  // Claim the node for the signed-in account, so its emergencies reach their
  // contact. Fire-and-forget: a 409 just means it is already claimed.
  useEffect(() => {
    if (!running || user === null || reading === null) return;
    void api.claimDevice(deviceIdRef.current).catch(() => undefined);
  }, [running, user, reading]);

  const enableWeather = useCallback(async () => {
    setShareWeather(true);
    setWeatherNote('Looking up local conditions…');
    const conditions = await monitor.refresh();
    setWeatherNote(
      conditions === null
        ? 'Location unavailable, so temperature stays unmeasured.'
        : null,
    );
  }, [monitor]);

  const status = reading?.server_status ?? 'SAFE';

  return (
    <div className="panel p-xl" data-testid="camera-sensor">
      <div className="flex flex-wrap items-start justify-between gap-md">
        <div>
          <p className="eyebrow">This device</p>
          <h3 className="mt-xs text-display-sm">Use your camera as a sensor</h3>
        </div>
        {running ? (
          <button className="btn-ghost" type="button" onClick={stop}>
            Stop
          </button>
        ) : null}
      </div>

      <p className="mt-md max-w-[62ch] text-body-md text-on-primary-mute">
        Your camera can see fire. It cannot smell smoke or measure the
        temperature of the room — no phone or laptop can, those need the
        physical gas and thermal sensors. So this node reports what it genuinely
        observes: how much of the frame looks like flame, how bright the scene
        is, and how hazy it has become.
      </p>

      <div className="mt-xl grid grid-cols-1 gap-xl laptop:grid-cols-[minmax(0,320px)_1fr]">
        <div>
          <div className="relative overflow-hidden rounded-xs border-hairline border-hairline-on-dark bg-canvas-night">
            {/* No <track>: the stream is video-only with no audio to caption,
                and it is decorative preview of what the sensor is looking at.
                The measurements are announced in text below. */}
            <video
              ref={videoRef}
              className="aspect-[4/3] w-full object-cover"
              playsInline
              muted
              aria-label="Live camera preview"
              data-testid="camera-preview"
            />
            {!running ? (
              <div className="absolute inset-0 flex items-center justify-center bg-canvas-night">
                <p className="text-caption text-on-primary-mute">Camera off</p>
              </div>
            ) : null}
          </div>

          {!running ? (
            <button
              className="btn-ghost mt-md w-full"
              type="button"
              onClick={() => {
                void start();
              }}
              disabled={starting}
            >
              {starting ? 'Starting…' : 'Start camera'}
            </button>
          ) : null}

          <p className="mt-sm text-caption text-on-primary-mute">
            Video never leaves this device. Only three numbers are sent.
          </p>
        </div>

        <div>
          {error !== null ? (
            <p className="text-body-md text-status-fire" role="alert">
              {error}
            </p>
          ) : null}

          {running ? (
            <>
              <p
                className={`text-display-sm ${STATUS_COLOUR[status]}`}
                data-testid="camera-status"
                role="status"
              >
                {STATUS_TEXT[status]}
              </p>

              <dl className="mt-lg grid grid-cols-2 gap-md">
                <Metric
                  label="Flame in frame"
                  value={
                    metrics === null
                      ? '—'
                      : `${(metrics.flame_ratio * 100).toFixed(2)}%`
                  }
                  hint="Pixels matching fire's colour signature"
                />
                <Metric
                  label="Flicker"
                  value={
                    reading === null || reading.flicker === null
                      ? '—'
                      : reading.flicker.toFixed(2)
                  }
                  hint="Fire moves; a red object does not"
                />
                <Metric
                  label="Brightness"
                  value={
                    metrics === null ? '—' : `${(metrics.luminance * 100).toFixed(0)}%`
                  }
                  hint="Scene context only"
                />
                <Metric
                  label="Haze"
                  value={
                    metrics === null ? '—' : `${(metrics.haze_index * 100).toFixed(0)}%`
                  }
                  hint="Loss of detail, as smoke would cause"
                />
              </dl>

              <div className="mt-lg border-t-hairline border-hairline-on-dark pt-lg">
                {shareWeather ? (
                  <p className="text-body-sm text-on-primary-mute">
                    {reading === null || reading.temperature_c === null
                      ? (weatherNote ?? 'Outdoor conditions unmeasured.')
                      : `Outdoor air near you: ${reading.temperature_c.toFixed(1)} °C, ${reading.humidity_pct?.toFixed(0) ?? '—'}% RH, PM2.5 ${reading.pm2_5_ugm3?.toFixed(1) ?? '—'} µg/m³, AQI ${reading.us_aqi?.toFixed(0) ?? '—'}. Measured outdoors, not in this room.`}
                  </p>
                ) : (
                  <>
                    <button
                      className="text-body-sm underline"
                      type="button"
                      onClick={() => {
                        void enableWeather();
                      }}
                    >
                      Add real humidity, PM2.5 and AQI from your location
                    </button>
                    <p className="mt-xs text-caption text-on-primary-mute">
                      Real readings from nearby monitoring stations — your phone
                      has no humidity or gas sensor of its own. Also enables
                      temperature-based emergency alerts.
                    </p>
                  </>
                )}
              </div>

              {publishError !== null ? (
                <p className="mt-md text-body-sm text-status-warning" role="alert">
                  {publishError}
                </p>
              ) : null}
            </>
          ) : (
            <p className="text-body-md text-on-primary-mute">
              Start the camera to begin publishing readings. They appear in the
              device list and charts above, alongside any hardware nodes.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div>
      <dt className="eyebrow">{label}</dt>
      <dd className="mt-xxs font-mono text-body-lg tabular-nums">{value}</dd>
      <p className="mt-xxs text-caption text-on-primary-mute">{hint}</p>
    </div>
  );
}

export default CameraSensorPanel;
