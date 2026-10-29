import { fireEvent, render, screen } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { keys, security, sshConfig } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { HubStoreProvider } from '../src/state/hubStore';
import { SshPage } from '../src/pages/SshPage';
import { ko } from '../src/i18n/ko';

const mocked = vi.mocked(api);
const renderPage = () =>
  render(
    <GuardianRoot>
      <HubStoreProvider>
        <SshPage />
      </HubStoreProvider>
    </GuardianRoot>
  );

describe('SshPage', () => {
  beforeEach(() => {
    mocked.fetchSSHSecurity.mockResolvedValue(security);
    mocked.fetchSSHConfig.mockResolvedValue(sshConfig);
    mocked.fetchAuthorizedKeys.mockResolvedValue(keys);
  });

  it('renders the score, config, rules and keys from the hub', async () => {
    renderPage();
    expect(await screen.findByText('72')).toBeTruthy();
    expect(screen.getByText(ko.fleet.ssh.passed(5))).toBeTruthy();
    expect(screen.getByText(ko.fleet.ssh.failed(1))).toBeTruthy();
    expect(await screen.findByText(ko.fleet.ssh.sshdConfig('/etc/ssh/sshd_config'))).toBeTruthy();
    expect(screen.getByText('2222')).toBeTruthy();
    expect(screen.getByText('Disable root login')).toBeTruthy();
    expect(screen.getByText(ko.fleet.ssh.ruleStatus.FAIL)).toBeTruthy();
    expect(screen.getByText(`${ko.fleet.ssh.remediation}: Set PermitRootLogin no`)).toBeTruthy();
    expect(screen.getByText('william@laptop')).toBeTruthy();
  });

  it('shows each failing source with its message and keeps the others', async () => {
    mocked.fetchSSHConfig.mockRejectedValue(new Error('sshd_config unreadable'));
    mocked.fetchAuthorizedKeys.mockRejectedValue(new api.HubError('keys forbidden', 403));
    renderPage();
    expect(await screen.findByText('sshd_config unreadable')).toBeTruthy();
    expect(await screen.findByText('keys forbidden')).toBeTruthy();
    expect(screen.getByText('72')).toBeTruthy();
    expect(screen.queryByText('william@laptop')).toBeNull();
    expect(screen.getAllByRole('button', { name: ko.common.retry })).toHaveLength(2);
  });

  it('shows the security error and no score when the audit fails, then recovers on retry', async () => {
    mocked.fetchSSHSecurity.mockRejectedValueOnce(new Error('audit failed'));
    renderPage();
    const messages = await screen.findAllByText('audit failed');
    expect(messages.length).toBeGreaterThan(0);
    expect(screen.queryByText('72')).toBeNull();
    fireEvent.click(screen.getAllByRole('button', { name: ko.common.retry })[0]);
    expect(await screen.findByText('72')).toBeTruthy();
  });

  it('says the syntax was not checked instead of calling it invalid without root', async () => {
    mocked.fetchSSHConfig.mockResolvedValue({
      ...sshConfig,
      is_valid: false,
      syntax_checked: false,
      source: 'config files',
      syntax_message: 'Not checked: sshd needs root to test the config.',
    });
    renderPage();
    expect(await screen.findByText(ko.fleet.ssh.config.notChecked)).toBeTruthy();
    expect(screen.queryByText(ko.fleet.ssh.config.invalid)).toBeNull();
    expect(screen.getByText('config files')).toBeTruthy();
  });

  it('counts only FAIL rules as failed and lists warnings and info separately', async () => {
    mocked.fetchSSHSecurity.mockResolvedValue({ ...security, failed_count: 2, warn_count: 1, info_count: 1, pass_count: 1 });
    renderPage();
    expect(await screen.findByText(ko.fleet.ssh.failed(2))).toBeTruthy();
    expect(screen.getByText(ko.fleet.ssh.warnings(1))).toBeTruthy();
    expect(screen.getByText(ko.fleet.ssh.info(1))).toBeTruthy();
  });
});
