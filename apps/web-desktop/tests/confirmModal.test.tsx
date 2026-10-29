import { fireEvent, render, screen } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { describe, expect, it, vi } from 'vitest';
import { ConfirmModal } from '../src/components/ConfirmModal';
import { ko } from '../src/i18n/ko';

function show(props: Partial<Parameters<typeof ConfirmModal>[0]> = {}) {
  const onConfirm = vi.fn();
  render(
    <GuardianRoot>
      <ConfirmModal title="Do it" message="Sure?" confirmLabel="Go" onConfirm={onConfirm} onCancel={() => undefined} {...props} />
    </GuardianRoot>
  );
  return onConfirm;
}

describe('ConfirmModal', () => {
  it('dismisses with the Korean default cancel label', () => {
    show();
    expect(screen.getByRole('button', { name: ko.shell.confirm.cancel })).toBeTruthy();
  });

  it('uses the danger tone by default', () => {
    show();
    expect(screen.getByRole('button', { name: 'Go' }).className).toContain('sg-btn--danger');
  });

  it('uses the primary tone for reversible steps', () => {
    show({ tone: 'primary' });
    const button = screen.getByRole('button', { name: 'Go' });
    expect(button.className).toContain('sg-btn--primary');
    expect(button.className).not.toContain('sg-btn--danger');
  });

  it('keeps Confirm disabled while confirmDisabled is set', () => {
    const onConfirm = show({ confirmDisabled: true });
    const button = screen.getByRole('button', { name: 'Go' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
