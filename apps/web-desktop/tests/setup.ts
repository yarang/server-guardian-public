import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';
import '../src/i18n/uiMessages';
import { clearDebatePrefill } from '../src/state/debatePrefill';

afterEach(() => {
  cleanup();
  clearDebatePrefill();
  window.location.hash = '';
  window.localStorage.clear();
});
