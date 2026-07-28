/** FireProtect dashboard shell. */

import { Suspense, lazy, useCallback, useMemo, useRef, useState } from 'react';

import { AlertHistory } from '@/components/AlertHistory';
import { DeviceGrid } from '@/components/DeviceGrid';
import { NavBar } from '@/components/NavBar';
import { RiskGauge } from '@/components/RiskGauge';
import { StatusHero } from '@/components/StatusHero';
import { ThresholdConfig } from '@/components/ThresholdConfig';
import { AmbientMonitor } from '@/lib/ambient';
import { useAuth } from '@/lib/useAuth';
import { useLiveData } from '@/lib/useLiveData';
import type { FireStatus, Reading } from '@/types';
import { STATUS_RANK } from '@/types';

/*
 * Recharts is ~525 kB minified - by far the largest dependency and roughly
 * 20x the rest of the app. The telemetry section sits below the fold, so the
 * charts are loaded on demand rather than blocking first paint.
 */
const SensorCharts = lazy(() => import('@/components/SensorCharts'));

/*
 * The camera panel pulls in getUserMedia and the frame analyser, neither of
 * which a visitor on a desktop with no interest in the feature should pay for
 * on first paint. Same reasoning as the charts below it.
 */
const CameraSensorPanel = lazy(() => import('@/components/CameraSensor'));
const EmergencyContact = lazy(() => import('@/components/EmergencyContact'));
const SignInPanel = lazy(() => import('@/components/SignInPanel'));
const LocalConditions = lazy(() => import('@/components/LocalConditions'));

const SECTIONS = [
  { id: 'devices', label: 'Devices' },
  { id: 'camera', label: 'This device' },
  { id: 'telemetry', label: 'Telemetry' },
  { id: 'alerts', label: 'Alerts' },
  { id: 'account', label: 'Account' },
  { id: 'thresholds', label: 'Thresholds' },
] as const;

/** Latest reading per device, used to derive the site-wide status. */
function latestPerDevice(readings: Reading[]): Reading[] {
  const latest = new Map<string, Reading>();
  for (const reading of readings) {
    latest.set(reading.device_id, reading);
  }
  return [...latest.values()];
}

export default function App() {
  const { user, loading: authLoading, signOut } = useAuth();
  const {
    devices,
    readings,
    alerts,
    stats,
    connection,
    loading,
    error,
    acknowledgeAlert,
    resolveAlert,
  } = useLiveData();

  const [selectedDevice, setSelectedDevice] = useState<string | null>(null);

  /*
   * One monitor, shared by the camera node and the outdoor-air panel. Two
   * instances would mean two geolocation prompts and two lookups for the same
   * coordinates, which is both wasteful and confusing to be asked twice.
   */
  const ambientRef = useRef<AmbientMonitor>(new AmbientMonitor());

  // Worst status across all devices — one burning room makes the site FIRE.
  const overallStatus: FireStatus = useMemo(() => {
    let worst: FireStatus = 'SAFE';
    for (const reading of latestPerDevice(readings)) {
      if (STATUS_RANK[reading.server_status] > STATUS_RANK[worst]) {
        worst = reading.server_status;
      }
    }
    return worst;
  }, [readings]);

  const riskScore = useMemo(() => {
    const relevant = selectedDevice
      ? readings.filter((r) => r.device_id === selectedDevice)
      : latestPerDevice(readings);
    if (relevant.length === 0) return 0;
    return Math.max(...relevant.slice(-20).map((r) => r.risk_score ?? 0));
  }, [readings, selectedDevice]);

  const gaugeStatus: FireStatus = selectedDevice
    ? (readings.filter((r) => r.device_id === selectedDevice).at(-1)?.server_status ?? 'SAFE')
    : overallStatus;

  const navigate = useCallback((id: string) => {
    if (id === 'top') {
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  return (
    <div id="top" className="min-h-screen bg-canvas-night text-on-primary">
      <NavBar
        connection={connection}
        sections={SECTIONS}
        onNavigate={navigate}
        userEmail={user?.email ?? null}
        onSignOut={signOut}
      />

      <main>
        <StatusHero
          status={overallStatus}
          stats={stats}
          onViewAlerts={() => navigate('alerts')}
        />

        {error ? (
          <div className="reading-column py-xl">
            <p
              className="panel p-xl text-body-md text-status-warning"
              role="alert"
              data-testid="load-error"
            >
              {error}
            </p>
          </div>
        ) : null}

        {/* Devices — sensor-mesh backdrop, lazy via CSS background. */}
        <section
          id="devices"
          className="band-photo band-mesh border-t-hairline border-hairline-on-dark py-24"
          aria-labelledby="devices-heading"
        >
          <div className="reading-column">
            <p className="eyebrow">Fleet</p>
            <h2 id="devices-heading" className="mt-xs text-display-lg">
              Devices
            </h2>
            <p className="mt-md max-w-[60ch] text-body-lg text-on-primary-mute">
              {selectedDevice
                ? 'Showing one device. Select it again to return to the whole fleet.'
                : 'Select a device to filter the telemetry charts below.'}
            </p>

            <div className="mt-xxl">
              {loading && devices.length === 0 ? (
                <div className="panel p-xxl" data-testid="devices-loading">
                  <p className="eyebrow">Loading devices…</p>
                </div>
              ) : (
                <DeviceGrid
                  devices={devices}
                  readings={readings}
                  selectedId={selectedDevice}
                  onSelect={setSelectedDevice}
                />
              )}
            </div>
          </div>
        </section>

        {/* Camera node — the reason the deployed link does anything on a phone. */}
        <section
          id="camera"
          className="border-t-hairline border-hairline-on-dark bg-canvas-night py-24"
          aria-labelledby="camera-heading"
        >
          <div className="reading-column">
            <p className="eyebrow">Live sensor</p>
            <h2 id="camera-heading" className="mt-xs text-display-lg">
              This device
            </h2>
            <div className="mt-xxl">
              <Suspense
                fallback={
                  <div className="panel p-xxl" data-testid="camera-loading">
                    <p className="eyebrow">Loading camera sensor…</p>
                  </div>
                }
              >
                <div className="flex flex-col gap-xl">
                  <CameraSensorPanel monitor={ambientRef.current} />
                  <LocalConditions monitor={ambientRef.current} />
                </div>
              </Suspense>
            </div>
          </div>
        </section>

        {/* Telemetry */}
        <section
          id="telemetry"
          className="border-t-hairline border-hairline-on-dark bg-canvas-night py-24"
          aria-labelledby="telemetry-heading"
        >
          <div className="reading-column">
            <p className="eyebrow">Live sensors</p>
            <h2 id="telemetry-heading" className="mt-xs text-display-lg">
              Telemetry
            </h2>

            <div className="mt-xxl grid grid-cols-1 items-start gap-huge laptop:grid-cols-[1fr_260px]">
              <Suspense
                fallback={
                  <div className="panel p-xxl" data-testid="charts-loading">
                    <p className="eyebrow">Loading charts…</p>
                  </div>
                }
              >
                <SensorCharts readings={readings} deviceId={selectedDevice} />
              </Suspense>
              <div className="panel flex justify-center p-xl">
                <RiskGauge score={riskScore} status={gaugeStatus} />
              </div>
            </div>
          </div>
        </section>

        {/* Alerts — ember backdrop */}
        <section
          id="alerts"
          className="band-photo band-ember border-t-hairline border-hairline-on-dark py-24"
          aria-labelledby="alerts-heading"
        >
          <div className="reading-column">
            <p className="eyebrow">Incidents</p>
            <h2 id="alerts-heading" className="mt-xs text-display-lg">
              Alert history
            </h2>
            <div className="mt-xxl">
              <AlertHistory
                alerts={alerts}
                onAcknowledge={acknowledgeAlert}
                onResolve={resolveAlert}
              />
            </div>
          </div>
        </section>

        {/* Account — emergency contact and permanent temperature history. */}
        <section
          id="account"
          className="border-t-hairline border-hairline-on-dark bg-canvas-night py-24"
          aria-labelledby="account-heading"
        >
          <div className="reading-column">
            <p className="eyebrow">Escalation</p>
            <h2 id="account-heading" className="mt-xs text-display-lg">
              Account
            </h2>
            <p className="mt-md max-w-[60ch] text-body-lg text-on-primary-mute">
              {user === null
                ? 'Sign in to set an emergency contact and keep a permanent record of every high-temperature event.'
                : `Signed in as ${user.email}.`}
            </p>
            <div className="mt-xxl">
              <Suspense
                fallback={
                  <div className="panel p-xxl" data-testid="account-loading">
                    <p className="eyebrow">Loading…</p>
                  </div>
                }
              >
                {authLoading ? (
                  <div className="panel p-xxl">
                    <p className="eyebrow">Checking your session…</p>
                  </div>
                ) : user === null ? (
                  <SignInPanel />
                ) : (
                  <EmergencyContact />
                )}
              </Suspense>
            </div>
          </div>
        </section>

        {/* Thresholds — circuit backdrop */}
        <section
          id="thresholds"
          className="band-photo band-circuit border-t-hairline border-hairline-on-dark py-24"
          aria-labelledby="thresholds-heading"
        >
          <div className="reading-column">
            <p className="eyebrow">Configuration</p>
            <h2 id="thresholds-heading" className="mt-xs text-display-lg">
              Thresholds
            </h2>
            <div className="mt-xxl">
              <ThresholdConfig />
            </div>
          </div>
        </section>
      </main>

      {/* footer-dark */}
      <footer className="border-t-hairline border-hairline-on-dark bg-canvas-night px-xl py-xxl">
        <div className="reading-column flex flex-wrap items-center justify-between gap-md">
          <p className="text-caption text-on-primary-mute">
            FireProtect — IoT fire detection. Not a certified life-safety device.
          </p>
          <p className="text-caption text-on-primary-mute">
            {stats ? `${stats.total_readings.toLocaleString()} readings recorded` : ''}
          </p>
        </div>
      </footer>
    </div>
  );
}
