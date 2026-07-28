/** Sign in or create an account. One form, two modes. */

import { useState, type FormEvent } from 'react';

import { authErrorMessage, useAuth } from '@/lib/useAuth';

type Mode = 'signin' | 'signup';

const MIN_PASSWORD_LENGTH = 8;

export function SignInPanel() {
  const { signIn, signUp } = useAuth();
  const [mode, setMode] = useState<Mode>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);

    // Checked here as well as on the server so the user is told immediately
    // rather than after a round trip that will certainly fail.
    if (mode === 'signup' && password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }

    setBusy(true);
    try {
      if (mode === 'signin') await signIn(email, password);
      else await signUp(email, password, displayName);
    } catch (cause) {
      setError(authErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  const switchMode = () => {
    setMode(mode === 'signin' ? 'signup' : 'signin');
    setError(null);
  };

  return (
    <div className="panel mx-auto max-w-[440px] p-xxl" data-testid="sign-in-panel">
      <p className="eyebrow">{mode === 'signin' ? 'Account' : 'Get started'}</p>
      <h3 className="mt-xs text-display-sm">
        {mode === 'signin' ? 'Sign in' : 'Create an account'}
      </h3>
      <p className="mt-md text-body-md text-on-primary-mute">
        {mode === 'signin'
          ? 'Access your emergency contact and temperature history.'
          : 'Store an emergency contact and keep a permanent record of every high-temperature event.'}
      </p>

      <form className="mt-xl flex flex-col gap-md" onSubmit={submit} noValidate>
        {mode === 'signup' ? (
          <label className="flex flex-col gap-xs">
            <span className="text-caption text-on-primary-mute">Name (optional)</span>
            <input
              className="text-input"
              type="text"
              value={displayName}
              autoComplete="name"
              maxLength={128}
              onChange={(event) => {
                setDisplayName(event.target.value);
              }}
            />
          </label>
        ) : null}

        <label className="flex flex-col gap-xs">
          <span className="text-caption text-on-primary-mute">Email</span>
          <input
            className="text-input"
            type="email"
            required
            value={email}
            autoComplete="email"
            inputMode="email"
            onChange={(event) => {
              setEmail(event.target.value);
            }}
          />
        </label>

        <label className="flex flex-col gap-xs">
          <span className="text-caption text-on-primary-mute">Password</span>
          <input
            className="text-input"
            type="password"
            required
            value={password}
            // Tells a password manager whether to offer a saved password or
            // generate a new one; getting this wrong is a common reason
            // sign-up flows fight with 1Password and iCloud Keychain.
            autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
            minLength={mode === 'signup' ? MIN_PASSWORD_LENGTH : undefined}
            onChange={(event) => {
              setPassword(event.target.value);
            }}
          />
          {mode === 'signup' ? (
            <span className="text-caption text-on-primary-mute">
              At least {MIN_PASSWORD_LENGTH} characters. A short phrase beats a
              short jumble.
            </span>
          ) : null}
        </label>

        {error !== null ? (
          <p className="text-body-sm text-status-fire" role="alert">
            {error}
          </p>
        ) : null}

        <button className="btn-ghost mt-sm" type="submit" disabled={busy}>
          {busy ? 'Working…' : mode === 'signin' ? 'Sign in' : 'Create account'}
        </button>
      </form>

      <button className="mt-lg text-body-sm underline" type="button" onClick={switchMode}>
        {mode === 'signin'
          ? 'No account yet? Create one'
          : 'Already have an account? Sign in'}
      </button>
    </div>
  );
}

export default SignInPanel;
