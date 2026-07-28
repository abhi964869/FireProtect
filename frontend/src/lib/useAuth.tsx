/*
 * react-refresh/only-export-components is disabled for this file. The rule
 * exists to keep hot reload from losing component state, and it fires because
 * this module exports a provider *and* its hook. Splitting them would put the
 * context in one file and its only consumer in another purely to satisfy a
 * dev-server ergonomics rule — the provider/hook pair is the standard React
 * idiom and belongs together.
 */
/* eslint-disable react-refresh/only-export-components */

/**
 * Session state: who is signed in, and how to change that.
 *
 * A context rather than prop drilling because the answer is needed in three
 * unrelated places (nav bar, camera panel, settings) and none of them are
 * near each other in the tree.
 *
 * The dashboard itself stays readable when signed out. Signing in is what
 * unlocks the things that are inherently personal — an emergency contact and
 * a private history — rather than a wall placed in front of the whole app.
 * A fire dashboard that hides the fire behind a login form is worse than
 * useless in the moment it matters.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import { ApiError, api, getToken, setToken } from '@/lib/api';
import type { User } from '@/types';

interface AuthValue {
  user: User | null;
  /** True until the stored token has been checked against the server. */
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (email: string, password: string, displayName: string) => Promise<void>;
  signOut: () => void;
  /** Replace the cached user after a settings change. */
  setUser: (user: User) => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUserState] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // Validate any stored token once on boot. A token that survived in
  // localStorage may have expired, or been signed with a secret the server no
  // longer has — either way the only way to know is to ask.
  useEffect(() => {
    let cancelled = false;
    if (getToken() === null) {
      setLoading(false);
      return;
    }
    api
      .me()
      .then((me) => {
        if (!cancelled) setUserState(me);
      })
      .catch(() => {
        if (!cancelled) {
          setToken(null);
          setUserState(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // The API client dispatches this when any request comes back 401, so a
  // token that expires mid-session clears the UI instead of leaving a signed-in
  // shell whose every action silently fails.
  useEffect(() => {
    const onSignedOut = () => {
      setUserState(null);
    };
    window.addEventListener('fireprotect:signed-out', onSignedOut);
    return () => {
      window.removeEventListener('fireprotect:signed-out', onSignedOut);
    };
  }, []);

  const signIn = useCallback(async (email: string, password: string) => {
    const response = await api.login(email, password);
    setToken(response.access_token);
    setUserState(response.user);
  }, []);

  const signUp = useCallback(
    async (email: string, password: string, displayName: string) => {
      const response = await api.register(email, password, displayName);
      setToken(response.access_token);
      setUserState(response.user);
    },
    [],
  );

  const signOut = useCallback(() => {
    setToken(null);
    setUserState(null);
  }, []);

  const value = useMemo<AuthValue>(
    () => ({ user, loading, signIn, signUp, signOut, setUser: setUserState }),
    [user, loading, signIn, signUp, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error('useAuth must be used inside <AuthProvider>');
  }
  return value;
}

/** Turn any thrown value into something worth showing a person. */
export function authErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 0) {
      return 'Cannot reach the server. Check your connection and try again.';
    }
    return error.message;
  }
  return 'Something went wrong. Please try again.';
}
