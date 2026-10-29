import { expect, it } from 'vitest';
import { act } from '@testing-library/react';
import { mount } from '../src/mount';
import { GuardianRoot, StatusChip } from '../src';

it('mounts a node into an element and unmounts cleanly', () => {
  const el = document.createElement('div');
  document.body.appendChild(el);
  let unmount!: () => void;
  act(() => {
    unmount = mount(el, (
      <GuardianRoot>
        <StatusChip tone="ok">APPROVED</StatusChip>
      </GuardianRoot>
    ));
  });
  expect(el.textContent).toContain('APPROVED');
  act(() => unmount());
  expect(el.textContent).toBe('');
  el.remove();
});
