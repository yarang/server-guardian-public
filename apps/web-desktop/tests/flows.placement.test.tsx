import { fireEvent, render, screen } from '@testing-library/react';
import { GuardianRoot } from '@server-guardian/ui';
import { describe, expect, it, vi } from 'vitest';
import { ClosePanel, SeatPanel } from '../src/pages/flows/FlowPanels';
import type { FlowData, TeamCatalog } from '../src/types';
import { ko } from '../src/i18n/ko';

const E = ko.flows.editor;
const catalog: TeamCatalog = {
  providers: ['claude', 'codex'], installed: ['claude'], tools: [], max_critics: 3, max_rounds: 6,
  host_labels: ['worker:any', 'worker:other'], workers: ['node-1'],
};
const flow: FlowData = {
  id: 'f', name: 'F', seats: [{ id: 'writer', job: 'write', character: 'expert' }], close: { character: 'judge' }, links: [],
};
const names = (id: string) => id;

// The team editor's placement fields moved here before it is removed (docs/design/ui-verification-review.md, X1).
describe('flow editor placement', () => {
  it('sets a seat host and repository tools, and clears them again', () => {
    const onChange = vi.fn();
    render(<GuardianRoot><SeatPanel flow={flow} seat={flow.seats[0]} personas={[]} catalog={catalog} names={names}
      onChange={onChange} onBundle={() => undefined} onRemove={() => undefined} /></GuardianRoot>);
    const host = screen.getByLabelText(E.host) as HTMLSelectElement;
    expect([...host.options].map((o) => o.value)).toEqual(['', 'worker:any', 'worker:other', 'node-1']);
    fireEvent.change(host, { target: { value: 'node-1' } });
    expect(onChange).toHaveBeenLastCalledWith({ host: 'node-1' });
    fireEvent.change(host, { target: { value: '' } });
    expect(onChange).toHaveBeenLastCalledWith({ host: undefined });
    fireEvent.click(screen.getByLabelText(E.codeTools));
    expect(onChange).toHaveBeenLastCalledWith({ code_tools: 'mcp' });
  });

  it('hides the host field without workers unless the close already names one', () => {
    const quiet = { ...catalog, workers: [] };
    const { unmount } = render(<GuardianRoot><ClosePanel flow={flow} personas={[]} catalog={quiet} names={names} onChange={() => undefined} /></GuardianRoot>);
    expect(screen.queryByLabelText(E.host)).toBeNull();
    unmount();
    const stored = { ...flow, close: { ...flow.close, host: 'gone-node', code_tools: 'mcp' } };
    render(<GuardianRoot><ClosePanel flow={stored} personas={[]} catalog={quiet} names={names} onChange={() => undefined} /></GuardianRoot>);
    const host = screen.getByLabelText(E.host) as HTMLSelectElement;
    expect(host.value).toBe('gone-node');
    expect(screen.getByText(E.hostNotWorker('gone-node'))).toBeTruthy();
    expect((screen.getByLabelText(E.codeTools) as HTMLInputElement).checked).toBe(true);
  });
});
