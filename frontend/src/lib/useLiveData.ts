/**
 * The dashboard's single source of live state.
 *
 * Loads an initial snapshot over REST, then keeps it current from the
 * WebSocket feed. The socket reconnects with exponential backoff and jitter;
 * on every successful reconnect the snapshot is re-fetched, because messages
 * missed while disconnected are gone and the UI must not silently show a
 * stale view.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, websocketUrl } from '@/lib/api';
import type {
  Alert,
  ConnectionState,
  Device,
  Reading,
  Stats,
  WsMessage,
} from '@/types';

/** How many readings to keep per device in memory for the live charts. */
const MAX_READINGS_PER_DEVICE = 240;

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

export interface LiveData {
  devices: Device[];
  readings: Reading[];
  alerts: Alert[];
  stats: Stats | null;
  connection: ConnectionState;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  acknowledgeAlert: (id: number) => Promise<void>;
  resolveAlert: (id: number) => Promise<void>;
}

function upsertDevice(devices: Device[], incoming: Device): Device[] {
  const index = devices.findIndex((d) => d.id === incoming.id);
  if (index === -1) {
    return [...devices, incoming].sort((a, b) => a.id.localeCompare(b.id));
  }
  const next = [...devices];
  next[index] = { ...devices[index]!, ...incoming };
  return next;
}

function upsertAlert(alerts: Alert[], incoming: Alert): Alert[] {
  const index = alerts.findIndex((a) => a.id === incoming.id);
  if (index === -1) {
    return [incoming, ...alerts].slice(0, 200);
  }
  const next = [...alerts];
  next[index] = { ...alerts[index]!, ...incoming };
  return next;
}

/**
 * Append a reading, capping history per device.
 *
 * The cap is per device, not global: with several devices reporting, a global
 * cap would let a chatty device evict a quiet one's entire history and blank
 * its chart.
 */
function appendReading(readings: Reading[], incoming: Reading): Reading[] {
  const next = [...readings, incoming];
  const perDevice = new Map<string, number>();
  const kept: Reading[] = [];
  for (let i = next.length - 1; i >= 0; i--) {
    const reading = next[i]!;
    const count = perDevice.get(reading.device_id) ?? 0;
    if (count < MAX_READINGS_PER_DEVICE) {
      perDevice.set(reading.device_id, count + 1);
      kept.push(reading);
    }
  }
  return kept.reverse();
}

export function useLiveData(): LiveData {
  const [devices, setDevices] = useState<Device[]>([]);
  const [readings, setReadings] = useState<Reading[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [connection, setConnection] = useState<ConnectionState>('connecting');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const backoffRef = useRef(RECONNECT_BASE_MS);
  const disposedRef = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const [deviceList, readingList, alertList, statsPayload] = await Promise.all([
        api.devices(),
        api.readings({ limit: 400 }),
        api.alerts({ limit: 100 }),
        api.stats(),
      ]);
      // REST returns newest-first; charts want oldest-first.
      setDevices(deviceList);
      setReadings([...readingList].reverse());
      setAlerts(alertList);
      setStats(statsPayload);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to load data');
    } finally {
      setLoading(false);
    }
  }, []);

  const handleMessage = useCallback((message: WsMessage) => {
    switch (message.type) {
      case 'reading':
        setReadings((current) => appendReading(current, message.payload as unknown as Reading));
        break;
      case 'alert':
        setAlerts((current) => upsertAlert(current, message.payload as unknown as Alert));
        break;
      case 'device':
        setDevices((current) => upsertDevice(current, message.payload as unknown as Device));
        break;
      case 'stats':
        setStats(message.payload as unknown as Stats);
        break;
      case 'hello':
        break;
    }
  }, []);

  const connect = useCallback(() => {
    if (disposedRef.current) return;

    let socket: WebSocket;
    try {
      socket = new WebSocket(websocketUrl());
    } catch {
      setConnection('closed');
      return;
    }
    socketRef.current = socket;

    socket.onopen = () => {
      if (disposedRef.current) return;
      setConnection('open');
      const wasReconnect = backoffRef.current > RECONNECT_BASE_MS;
      backoffRef.current = RECONNECT_BASE_MS;
      if (wasReconnect) {
        // Anything published while we were disconnected is unrecoverable from
        // the socket, so resynchronise from REST rather than showing stale data.
        void refresh();
      }
    };

    socket.onmessage = (event) => {
      if (disposedRef.current) return;
      try {
        handleMessage(JSON.parse(event.data as string) as WsMessage);
      } catch {
        // A malformed frame must not tear down a working connection.
      }
    };

    socket.onerror = () => {
      // onclose always follows; reconnection is handled there.
    };

    socket.onclose = () => {
      if (disposedRef.current) return;
      setConnection('reconnecting');
      // Full jitter, so many dashboards reconnecting after a backend restart
      // do not arrive in lockstep.
      const delay = Math.random() * backoffRef.current;
      backoffRef.current = Math.min(backoffRef.current * 2, RECONNECT_MAX_MS);
      reconnectTimer.current = setTimeout(connect, delay);
    };
  }, [handleMessage, refresh]);

  useEffect(() => {
    disposedRef.current = false;
    void refresh();
    connect();

    return () => {
      disposedRef.current = true;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      const socket = socketRef.current;
      if (socket) {
        // Detach handlers before closing so the teardown does not schedule a
        // reconnect for a component that is going away.
        socket.onclose = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onopen = null;
        if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
          socket.close();
        }
      }
    };
  }, [connect, refresh]);

  const acknowledgeAlert = useCallback(async (id: number) => {
    const updated = await api.acknowledgeAlert(id);
    setAlerts((current) => upsertAlert(current, updated));
  }, []);

  const resolveAlert = useCallback(async (id: number) => {
    const updated = await api.resolveAlert(id);
    setAlerts((current) => upsertAlert(current, updated));
  }, []);

  return useMemo(
    () => ({
      devices,
      readings,
      alerts,
      stats,
      connection,
      loading,
      error,
      refresh,
      acknowledgeAlert,
      resolveAlert,
    }),
    [
      devices,
      readings,
      alerts,
      stats,
      connection,
      loading,
      error,
      refresh,
      acknowledgeAlert,
      resolveAlert,
    ],
  );
}
