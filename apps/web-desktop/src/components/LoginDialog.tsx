import { useState } from 'react';
import type { FormEvent } from 'react';
import { Button, TextField } from '@server-guardian/ui';
import { getAgentUrl, login } from '../api';
import { tr } from '../i18n';
import { Modal } from './Modal';

/**
 * Token sign-in to the current hub. The hub answers every /api call with 401 until a session
 * exists; `onChangeAgent` lets the user leave for Settings when this is the wrong hub.
 */
export function LoginDialog({ onSuccess, onChangeAgent }: { onSuccess: () => void; onChangeAgent?: () => void }) {
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const value = token.trim();
    if (!value) return;
    setBusy(true);
    setError('');
    let ok = false;
    try {
      ok = await login(value);
    } catch {
      ok = false;
    }
    setBusy(false);
    if (!ok) {
      setError(tr.shell.login.invalid);
      return;
    }
    setToken('');
    onSuccess();
  };

  return (
    <Modal title={tr.shell.login.title}>
      <form onSubmit={submit} style={{ display: 'grid', gap: 'var(--sg-s-6)' }}>
        <p style={{ margin: 0, color: 'var(--sg-text-secondary)', fontSize: 13 }}>
          {tr.shell.login.hint}
        </p>
        <p style={{ margin: 0, fontSize: 13 }}>
          {tr.shell.login.signingInTo}: <span style={{ fontFamily: 'var(--sg-font-mono)', overflowWrap: 'anywhere' }}>{getAgentUrl()}</span>
        </p>
        <TextField
          label={tr.shell.login.tokenLabel}
          type="password"
          mono
          autoComplete="current-password"
          required
          value={token}
          onChange={(e) => setToken(e.target.value)}
          error={error || undefined}
        />
        <div style={{ display: 'flex', gap: 'var(--sg-s-4)', justifyContent: 'flex-end' }}>
          {onChangeAgent ? (
            <Button variant="ghost" onClick={onChangeAgent} style={{ marginRight: 'auto' }}>
              {tr.shell.login.changeHub}
            </Button>
          ) : null}
          <Button type="submit" variant="primary" loading={busy}>
            {tr.shell.login.submit}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
