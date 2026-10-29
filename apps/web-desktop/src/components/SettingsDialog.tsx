import { useState } from 'react';
import type { FormEvent } from 'react';
import { Button, TextField } from '@server-guardian/ui';
import { getAgentUrl, logout, setAgentUrl } from '../api';
import { tr } from '../i18n';
import { Modal } from './Modal';

/** Hub connection (URL) and sign-out. */
export function SettingsDialog({
  onClose,
  onSaved,
  onSignedOut,
}: {
  onClose: () => void;
  onSaved: () => void;
  onSignedOut: () => void;
}) {
  const [url, setUrl] = useState(getAgentUrl());
  const [error, setError] = useState('');

  const save = (e: FormEvent) => {
    e.preventDefault();
    const value = url.trim();
    try {
      const parsed = new URL(value);
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') throw new Error('protocol');
    } catch {
      setError(tr.shell.settingsDialog.invalidUrl);
      return;
    }
    setAgentUrl(value);
    onSaved();
  };

  const signOut = async () => {
    await logout();
    onSignedOut();
  };

  return (
    <Modal title={tr.shell.settingsDialog.title} onClose={onClose}>
      <form onSubmit={save} style={{ display: 'grid', gap: 'var(--sg-s-6)' }}>
        <TextField
          label={tr.shell.settingsDialog.urlLabel}
          mono
          value={url}
          onChange={(e) => {
            setUrl(e.target.value);
            setError('');
          }}
          error={error || undefined}
        />
        <div style={{ display: 'flex', gap: 'var(--sg-s-4)', flexWrap: 'wrap' }}>
          <Button variant="ghost" onClick={signOut} style={{ marginRight: 'auto' }}>
            {tr.shell.settingsDialog.signOut}
          </Button>
          <Button onClick={onClose}>{tr.common.cancel}</Button>
          <Button type="submit" variant="primary">
            {tr.shell.settingsDialog.saveAndReconnect}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
