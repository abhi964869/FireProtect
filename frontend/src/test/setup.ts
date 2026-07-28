import '@testing-library/jest-dom/vitest';
import { Children, cloneElement } from 'react';
import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
import type * as Recharts from 'recharts';

/** Alias so the mock factory below avoids an inline `import()` type. */
type Rechartsheet = typeof Recharts;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/**
 * jsdom implements neither of these, and Recharts' ResponsiveContainer plus the
 * nav's smooth scrolling both call them. Stubbing here keeps every test file
 * from repeating the same boilerplate.
 */
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;

/**
 * Replace Recharts' ResponsiveContainer with a fixed-size passthrough.
 *
 * jsdom performs no layout, so the real container measures 0x0 and logs
 * "width(0) and height(0) of chart should be greater than 0" for every chart
 * on every render. Driving it through a stubbed ResizeObserver instead just
 * trades that warning for an act() warning, because the resulting setState
 * lands outside React's act scope.
 *
 * Cloning the child chart with explicit dimensions sidesteps both: the charts
 * render with real geometry, and the output stays clean so a genuine warning
 * is never buried in noise. Layout behaviour of the container itself is a
 * browser concern and is not what these tests are asserting.
 */
vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<Rechartsheet>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactElement }) =>
      cloneElement(Children.only(children), { width: 800, height: 400 }),
  };
});

/**
 * jsdom performs no layout, so every element reports 0x0 and Recharts'
 * ResponsiveContainer logs "width(0) and height(0) of chart should be greater
 * than 0" for each chart. Giving the offset/client dimensions a non-zero
 * default makes the charts believe they have room and keeps the test output
 * clean, so a real warning is not lost in the noise.
 */
for (const prop of ['offsetWidth', 'clientWidth'] as const) {
  Object.defineProperty(HTMLElement.prototype, prop, {
    configurable: true,
    value: 800,
  });
}
for (const prop of ['offsetHeight', 'clientHeight'] as const) {
  Object.defineProperty(HTMLElement.prototype, prop, {
    configurable: true,
    value: 400,
  });
}

window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;
Element.prototype.scrollIntoView = vi.fn();

/**
 * A WebSocket test double. Real sockets in jsdom would leave open handles and
 * make tests hang; this records instances so tests can drive messages, opens,
 * and closes deterministically.
 */
export class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;

  constructor(readonly url: string) {
    MockWebSocket.instances.push(this);
  }

  open() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.({});
  }

  emit(type: string, payload: unknown) {
    this.onmessage?.({ data: JSON.stringify({ type, payload }) });
  }

  close() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({});
  }

  send() {}

  static reset() {
    MockWebSocket.instances = [];
  }

  static get last(): MockWebSocket | undefined {
    return MockWebSocket.instances[MockWebSocket.instances.length - 1];
  }
}

globalThis.WebSocket = MockWebSocket as unknown as typeof WebSocket;
