import { describe, expect, it } from 'vitest';
import { shortUptime } from '../src/lib/format';

describe('shortUptime', () => {
  it.each([
    ['up 8 days, 15:45', '8일 15시간'],
    ['up 6 days, 12:42', '6일 12시간'],
    ['up 3:12', '3시간 12분'],
    ['up 45 min', '45분'],
    ['up 3 days', '3일'],
    ['up 1 day, 2 min', '1일'],
    ['up 2 hours, 5 minutes', '2시간 5분'],
    ['up 1 week, 2 days, 3:00', '9일 3시간'],
    [' 10:00:01 up 8 days, 15:45,  2 users,  load average: 0.01, 0.02, 0.00', '8일 15시간'],
    ['12:42  up 6 days, 12:42, 3 users, load averages: 1.20 1.10 1.05', '6일 12시간'],
  ])('%s -> %s', (text, expected) => {
    expect(shortUptime(text)).toBe(expected);
  });

  it.each([[''], ['garbage'], [undefined], [null], ['up']])('falls back to a dash for %s', (text) => {
    expect(shortUptime(text)).toBe('—');
  });
});
