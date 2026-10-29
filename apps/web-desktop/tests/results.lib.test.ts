import { describe, expect, it } from 'vitest';
import { parseResultsRoute, resultsParams } from '../src/lib/results';

describe('results route', () => {
  it('parses a thread id and ignores anything that is not one', () => {
    expect(parseResultsRoute([])).toEqual({});
    expect(parseResultsRoute(['thread_20261020_ab1'])).toEqual({ thread: 'thread_20261020_ab1' });
    expect(parseResultsRoute(['../etc/passwd'])).toEqual({});
    expect(parseResultsRoute(['thread_x.assets'])).toEqual({});
  });

  it('builds the params back', () => {
    expect(resultsParams({})).toEqual(['results']);
    expect(resultsParams({ thread: 'thread_a' })).toEqual(['results', 'thread_a']);
  });
});
