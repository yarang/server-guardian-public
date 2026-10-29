import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// Vitest globals are off, so register RTL cleanup explicitly.
afterEach(() => cleanup());
