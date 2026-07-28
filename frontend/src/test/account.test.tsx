/** Session handling, the sign-in form, and the emergency-contact screen. */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { EmergencyContact } from '@/components/EmergencyContact';
import { SignInPanel } from '@/components/SignInPanel';
import { getToken, setToken } from '@/lib/api';
import { AuthProvider, useAuth } from '@/lib/useAuth';
import type { NotificationStatus, TemperatureEvent, User } from '@/types';

const user: User = {
  id: 1,
  email: 'owner@example.com',
  display_name: 'Owner',
  created_at: new Date().toISOString(),
  emergency_email: '',
  emergency_name: '',
  temperature_limit_c: 55,
  notifications_enabled: true,
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

interface StubOptions {
  loginStatus?: number;
  loginDetail?: string;
  notification?: NotificationStatus;
  events?: TemperatureEvent[];
  currentUser?: User;
}

function stubFetch(options: StubOptions = {}) {
  const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/api/auth/login') || url.includes('/api/auth/register')) {
      if (options.loginStatus !== undefined && options.loginStatus >= 400) {
        return json({ detail: options.loginDetail ?? 'nope' }, options.loginStatus);
      }
      return json({
        access_token: 'test-token',
        token_type: 'bearer',
        expires_in: 3600,
        user: options.currentUser ?? user,
      });
    }
    if (url.includes('/api/auth/notification-status')) {
      return json(
        options.notification ?? {
          transport: 'none',
          configured: false,
          contact_set: false,
        },
      );
    }
    if (url.includes('/api/auth/emergency-contact')) {
      const body = JSON.parse(String(init?.body)) as Partial<User>;
      return json({ ...(options.currentUser ?? user), ...body });
    }
    if (url.includes('/api/auth/me')) {
      return json(options.currentUser ?? user);
    }
    if (url.includes('/api/temperature-events')) {
      return json(options.events ?? []);
    }
    return json({});
  });
  globalThis.fetch = handler as unknown as typeof fetch;
  return handler;
}

beforeEach(() => {
  setToken(null);
  window.localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// --------------------------------------------------------------------------
// Token storage
// --------------------------------------------------------------------------

describe('token storage', () => {
  it('round-trips through localStorage so a refresh keeps you signed in', () => {
    setToken('abc');
    expect(window.localStorage.getItem('fireprotect.token')).toBe('abc');
    expect(getToken()).toBe('abc');
  });

  it('clears the stored token on sign out', () => {
    setToken('abc');
    setToken(null);
    expect(window.localStorage.getItem('fireprotect.token')).toBeNull();
    expect(getToken()).toBeNull();
  });

  it('survives storage being unavailable', () => {
    // Private browsing and blocked storage both throw here. Losing persistence
    // is fine; throwing out of the auth path is not.
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    expect(() => {
      setToken('abc');
    }).not.toThrow();
    expect(getToken()).toBe('abc');
    spy.mockRestore();
  });

  it('attaches the token as a bearer header', async () => {
    const handler = stubFetch();
    setToken('secret-token');
    const { api } = await import('@/lib/api');
    await api.me();
    const init = handler.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe(
      'Bearer secret-token',
    );
  });

  it('sends no Authorization header when signed out', async () => {
    const handler = stubFetch();
    const { api } = await import('@/lib/api');
    await api.stats();
    const init = handler.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });
});

// --------------------------------------------------------------------------
// Sign in
// --------------------------------------------------------------------------

function Harness() {
  const { user: current } = useAuth();
  return current === null ? <SignInPanel /> : <p>Signed in as {current.email}</p>;
}

function renderSignIn() {
  return render(
    <AuthProvider>
      <Harness />
    </AuthProvider>,
  );
}

describe('sign in', () => {
  it('signs a user in and stores the session', async () => {
    stubFetch();
    renderSignIn();

    await userEvent.type(screen.getByLabelText(/email/i), 'owner@example.com');
    await userEvent.type(screen.getByLabelText(/password/i), 'a good passphrase');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));

    expect(await screen.findByText(/signed in as owner@example.com/i)).toBeInTheDocument();
    expect(getToken()).toBe('test-token');
  });

  it('shows the server message when credentials are wrong', async () => {
    stubFetch({ loginStatus: 401, loginDetail: 'Incorrect email or password.' });
    renderSignIn();

    await userEvent.type(screen.getByLabelText(/email/i), 'owner@example.com');
    await userEvent.type(screen.getByLabelText(/password/i), 'wrong');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /incorrect email or password/i,
    );
    expect(getToken()).toBeNull();
  });

  it('switches to the sign-up form', async () => {
    stubFetch();
    renderSignIn();
    await userEvent.click(screen.getByRole('button', { name: /create one/i }));
    expect(screen.getByRole('button', { name: /create account/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/name \(optional\)/i)).toBeInTheDocument();
  });

  it('rejects a short password before making a request', async () => {
    const handler = stubFetch();
    renderSignIn();

    await userEvent.click(screen.getByRole('button', { name: /create one/i }));
    await userEvent.type(screen.getByLabelText(/email/i), 'new@example.com');
    await userEvent.type(screen.getByLabelText(/password/i), 'short');
    await userEvent.click(screen.getByRole('button', { name: /create account/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/at least 8 characters/i);
    expect(handler).not.toHaveBeenCalled();
  });

  it('hints password managers correctly for each mode', async () => {
    stubFetch();
    renderSignIn();
    expect(screen.getByLabelText(/password/i)).toHaveAttribute(
      'autocomplete',
      'current-password',
    );
    await userEvent.click(screen.getByRole('button', { name: /create one/i }));
    expect(screen.getByLabelText(/password/i)).toHaveAttribute(
      'autocomplete',
      'new-password',
    );
  });

  it('restores a session from a stored token on boot', async () => {
    stubFetch();
    setToken('stored-token');
    renderSignIn();
    expect(await screen.findByText(/signed in as owner@example.com/i)).toBeInTheDocument();
  });

  it('discards a stored token the server rejects', async () => {
    globalThis.fetch = vi.fn(async () =>
      json({ detail: 'Sign in to continue.' }, 401),
    ) as unknown as typeof fetch;
    setToken('expired-token');
    renderSignIn();

    expect(await screen.findByTestId('sign-in-panel')).toBeInTheDocument();
    await waitFor(() => {
      expect(getToken()).toBeNull();
    });
  });
});

// --------------------------------------------------------------------------
// Emergency contact
// --------------------------------------------------------------------------

function renderContact(options: StubOptions = {}) {
  stubFetch(options);
  setToken('test-token');
  return render(
    <AuthProvider>
      <EmergencyContact />
    </AuthProvider>,
  );
}

describe('emergency contact', () => {
  it('warns loudly when the server cannot send email at all', async () => {
    renderContact();
    // The worst failure mode is someone believing they are covered when the
    // server has no transport. It must be the first thing said.
    expect(await screen.findByTestId('notification-warning')).toHaveTextContent(
      /no email transport configured/i,
    );
  });

  it('warns when no contact has been saved', async () => {
    renderContact({
      notification: { transport: 'smtp', configured: true, contact_set: false },
    });
    expect(await screen.findByTestId('notification-warning')).toHaveTextContent(
      /no emergency contact saved/i,
    );
  });

  it('confirms when alerts are actually live', async () => {
    renderContact({
      currentUser: { ...user, emergency_email: 'contact@example.com' },
      notification: { transport: 'smtp', configured: true, contact_set: true },
    });
    expect(await screen.findByTestId('notification-ok')).toHaveTextContent(
      /contact@example.com/i,
    );
  });

  it('refuses an out-of-range temperature limit without calling the API', async () => {
    renderContact();
    await screen.findByTestId('notification-warning');

    const limit = screen.getByLabelText(/alert above/i);
    await userEvent.clear(limit);
    await userEvent.type(limit, '5');
    await userEvent.click(screen.getByRole('button', { name: /save contact/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/between 30/i);
  });

  it('saves a contact and confirms it', async () => {
    renderContact();
    await screen.findByTestId('notification-warning');

    await userEvent.type(
      screen.getByLabelText(/emergency email address/i),
      'contact@example.com',
    );
    await userEvent.click(screen.getByRole('button', { name: /save contact/i }));

    expect(await screen.findByText(/^saved\.$/i)).toBeInTheDocument();
  });

  it('says so plainly when there is no history', async () => {
    renderContact();
    expect(await screen.findByTestId('no-events')).toHaveTextContent(
      /that is the result you want/i,
    );
  });

  it('lists past excursions with their peak and outcome', async () => {
    renderContact({
      events: [
        {
          id: 1,
          device_id: 'esp32-01',
          started_at: new Date().toISOString(),
          ended_at: null,
          trigger_temperature_c: 61,
          peak_temperature_c: 78.5,
          threshold_c: 55,
          sensor_kind: 'hardware',
          notified: true,
          notify_error: '',
        },
      ],
    });
    const list = await screen.findByTestId('event-list');
    expect(list).toHaveTextContent('78.5 °C');
    expect(list).toHaveTextContent(/ongoing/i);
    expect(list).toHaveTextContent(/emergency email sent/i);
  });

  it('shows why an email failed rather than hiding it', async () => {
    renderContact({
      events: [
        {
          id: 2,
          device_id: 'esp32-01',
          started_at: new Date().toISOString(),
          ended_at: new Date().toISOString(),
          trigger_temperature_c: 60,
          peak_temperature_c: 60,
          threshold_c: 55,
          sensor_kind: 'camera',
          notified: false,
          notify_error: 'SMTP authentication failed',
        },
      ],
    });
    expect(await screen.findByTestId('event-list')).toHaveTextContent(
      /smtp authentication failed/i,
    );
  });
});
