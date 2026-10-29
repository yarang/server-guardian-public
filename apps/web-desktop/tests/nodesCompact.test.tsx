import { cleanup, render, screen, within } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { diagnosis, groupList, metrics, servers } from './fixtures';

vi.mock('../src/api', async (importOriginal) => {
  const { makeApiMock } = await import('./apiMock');
  return makeApiMock(await importOriginal());
});
import * as api from '../src/api';
import { HubStoreProvider } from '../src/state/hubStore';
import { NodesPage } from '../src/pages/NodesPage';
import { ko } from '../src/i18n/ko';

const mocked = vi.mocked(api);
const serversSuffix = new RegExp(`${ko.fleet.nodes.nodesAria('').trim()}$`);
const selectSuffix = new RegExp(`${ko.fleet.nodes.selectAria('').trim()}$`);
const original = window.matchMedia;

function mockNarrow(narrow: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: narrow,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    onchange: null,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

const renderPage = () =>
  render(
    <GuardianRoot>
      <HubStoreProvider>
        <NodesPage query={{}} />
      </HubStoreProvider>
    </GuardianRoot>
  );

describe('NodesPage layouts', () => {
  beforeEach(() => {
    mocked.fetchMetrics.mockResolvedValue(metrics);
    mocked.fetchServers.mockResolvedValue(servers);
    mocked.diagnoseFleet.mockResolvedValue(diagnosis);
    mocked.fetchGroups.mockResolvedValue(groupList);
  });
  afterEach(() => {
    // Unmount while the mock is still installed: effects that run on unmount may still call matchMedia.
    cleanup();
    window.matchMedia = original;
  });

  it('renders compact rows with a muted summary line under the narrow breakpoint', async () => {
    mockNarrow(true);
    renderPage();
    const summaries = await screen.findAllByTestId('node-summary');
    expect(screen.queryByRole('table', { name: serversSuffix })).toBeNull();
    expect(screen.queryByRole('checkbox', { name: ko.fleet.nodes.detailColumns })).toBeNull();
    const first = summaries.find((el) => el.textContent?.startsWith('10.0.0.1'));
    expect(first?.textContent).toBe(`10.0.0.1 · Site A, ARM · ${ko.fleet.nodes.uptimeShort(ko.common.durationD(3))} · ${ko.fleet.nodes.loadShort('0.1')} · ${ko.fleet.nodes.memShort('4G/8G')}`);
    expect(first?.getAttribute('title')).toBe(first?.textContent);
    const list = screen.getAllByRole('list', { name: serversSuffix })[0];
    expect(within(list).getAllByRole('checkbox', { name: selectSuffix }).length).toBeGreaterThan(0);
    expect(within(list).getAllByRole('link', { name: 'site-a-arm1' }).length).toBeGreaterThan(0);
  });

  it('keeps the table on desktop with the short uptime and the raw text as tooltip', async () => {
    mockNarrow(false);
    renderPage();
    const cell = (await screen.findAllByText(ko.common.durationD(3)))[0];
    expect(cell.getAttribute('title')).toBe('up 3 days');
    expect(screen.getAllByRole('table', { name: serversSuffix }).length).toBeGreaterThan(0);
    expect(screen.queryByTestId('node-summary')).toBeNull();
    expect(screen.getByRole('checkbox', { name: ko.fleet.nodes.detailColumns })).toBeTruthy();
  });
});
