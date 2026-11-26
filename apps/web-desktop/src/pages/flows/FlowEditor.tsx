import { useCallback, useMemo, useRef, useState } from 'react';
import { Button } from '@server-guardian/ui';
import { AuthRequiredError, saveFlow } from '../../api';
import { Modal } from '../../components/Modal';
import { Notice, NoticeList } from '../../components/Notice';
import { Row, Stack, muted } from '../../components/layout';
import { errorText } from '../../lib/format';
import {
  CLOSE,
  END,
  START,
  addLink,
  addSeat,
  blankFlow,
  callsPerRound,
  checkFlow,
  forSave,
  linkKey,
  removeElement,
  setBundle,
  updateLink,
  updateSeat,
} from '../../lib/flowGraph';
import type { FlowIssue } from '../../lib/flowGraph';
import type { FlowData, FlowEntry, FlowJob, PersonaConfig, SaveResult, TeamCatalog } from '../../types';
import { FlowCanvas } from './FlowCanvas';
import type { CanvasSelection } from './FlowCanvas';
import { BundlePanel, ClosePanel, FlowPanel, LinkPanel, SeatPanel } from './FlowPanels';
import { tr } from '../../i18n';

const t = () => tr.flows;

/** The sentence for a canvas problem (the lib returns a code, the words live in the catalogs). */
export function issueText(issue: FlowIssue): string {
  const m = t().issue;
  switch (issue.code) {
    case 'badId': return m.badId;
    case 'noName': return m.noName;
    case 'tooManySeats': return m.tooManySeats(issue.max);
    case 'noWriter': return m.noWriter;
    case 'noCharacter': return m.noCharacter;
    case 'unknownCharacter': return m.unknownCharacter(issue.character);
    case 'characterUnfit': return m.characterUnfit(issue.character);
    case 'noExit': return m.noExit(t().exit[issue.exit]);
    case 'neverRuns': return m.neverRuns;
    case 'deadEnd': return m.deadEnd;
    case 'noPath': return m.noPath;
    case 'summariserFeeds': return m.summariserFeeds;
    case 'condensedNeedsSummariser': return m.condensedNeedsSummariser;
    case 'rebutPair': return m.rebutPair;
    case 'noStance': return m.noStance;
  }
}

function useNarrow(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 767px)').matches;
}

/**
 * The flow editor (docs/design/flow-graph.md, G4): the canvas with a palette, the panel of the selected element and
 * the flow's own fields. Editing a builtin flow, a team or a template saves a custom flow under the same id, which
 * shadows it. The hub validates again on save.
 */
export function FlowEditor({
  entry,
  personas,
  catalog,
  onClose,
  onSaved,
}: {
  /** null: a new flow. */
  entry: FlowEntry | null;
  personas: PersonaConfig[];
  catalog: TeamCatalog | null;
  onClose: () => void;
  onSaved: (result: SaveResult) => void;
}) {
  const isNew = entry === null;
  const custom = entry?.source === 'custom';
  const [flow, setFlow] = useState<FlowData>(() => (entry ? JSON.parse(JSON.stringify(entry.data)) as FlowData : blankFlow()));
  const [selection, setSelection] = useState<CanvasSelection>(null);
  const [relayout, setRelayout] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const positions = useRef<Record<string, [number, number]>>({});
  const onPositions = useCallback((p: Record<string, [number, number]>) => {
    positions.current = p;
  }, []);
  const narrow = useNarrow();

  const names = useMemo(() => Object.fromEntries(personas.map((p) => [p.id, p.name || p.id])), [personas]);
  const label = (id: string) => (id === START || id === CLOSE || id === END ? t().fixed[id] : id);
  const issues = useMemo(() => checkFlow(flow, personas), [flow, personas]);
  const marked = useMemo(() => new Set<string>(issues.map((i) => i.target).filter((x) => x !== 'flow')), [issues]);

  const edit = (next: FlowData | null) => {
    if (next) setFlow(next);
    setError(null);
  };
  const add = (job: FlowJob) => {
    const { flow: next, id } = addSeat(flow, job);
    edit(next);
    setSelection({ kind: 'seat', id });
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await saveFlow(forSave(flow, positions.current));
      setWarnings(result.warnings);
      onSaved(result);
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const seat = selection?.kind === 'seat' ? flow.seats.find((s) => s.id === selection.id) : undefined;
  const link = selection?.kind === 'link' ? flow.links.find((l) => linkKey(l) === selection.id) : undefined;
  const title = isNew ? t().editor.newTitle : custom ? t().editor.editTitle(flow.id) : t().editor.copyTitle(flow.id);
  const panel = seat ? (
    <SeatPanel
      flow={flow}
      seat={seat}
      personas={personas}
      catalog={catalog}
      names={label}
      onChange={(c) => edit(updateSeat(flow, seat.id, c))}
      onBundle={(target) => edit(setBundle(flow, seat.id, target))}
      onRemove={() => {
        edit(removeElement(flow, seat.id));
        setSelection(null);
      }}
    />
  ) : selection?.kind === 'bundle' ? (
    <BundlePanel
      flow={flow}
      id={selection.id}
      onChange={(exchanges) => edit({ ...flow, bundles: (flow.bundles ?? []).map((b) => (b.id === selection.id ? { ...b, exchanges } : b)) })}
      onRemove={() => {
        edit(removeElement(flow, selection.id));
        setSelection(null);
      }}
    />
  ) : selection?.kind === 'close' ? (
    <ClosePanel flow={flow} personas={personas} catalog={catalog} names={label} onChange={(c) => edit({ ...flow, close: { ...flow.close, ...c } })} />
  ) : link ? (
    <LinkPanel
      link={link}
      names={label}
      onChange={(c) => edit(updateLink(flow, linkKey(link), c))}
      onRemove={() => {
        edit(updateLink(flow, linkKey(link), null));
        setSelection(null);
      }}
    />
  ) : (
    <FlowPanel flow={flow} idEditable={isNew} onChange={(c) => edit({ ...flow, ...c })} />
  );

  return (
    <Modal title={title} width={1240} onClose={busy ? undefined : onClose}>
      <Stack gap="var(--sg-s-5)">
        {entry && !custom ? <div style={{ ...muted, fontSize: 12 }}>{t().editor.subBuiltin}</div> : null}
        {custom ? <div style={{ ...muted, fontSize: 12 }}>{t().editor.subCustom}</div> : null}
        {narrow ? (
          <Stack gap="var(--sg-s-4)">
            <Notice tone="info">{t().editor.narrow}</Notice>
            <div style={{ fontSize: 13 }}>
              <strong>{t().editor.listSeats}</strong>
              <ul style={{ margin: '4px 0', paddingLeft: 18 }}>
                {flow.seats.map((s) => (
                  <li key={s.id}>{`${s.id} · ${t().job[s.job]} · ${s.character ? names[s.character] ?? s.character : '-'}`}</li>
                ))}
              </ul>
              <strong>{t().editor.listLinks}</strong>
              <ul style={{ margin: '4px 0', paddingLeft: 18 }}>
                {flow.links.map((l) => (
                  <li key={linkKey(l)}>{`${label(l.from)} → ${label(l.to)}${l.exit ? ` (${t().exit[l.exit]})` : l.look_back ? ' ↺' : ''}`}</li>
                ))}
              </ul>
            </div>
            {panel}
          </Stack>
        ) : (
          <>
            <Row gap="var(--sg-s-3)" style={{ flexWrap: 'wrap', alignItems: 'center' }}>
              <span style={{ ...muted, fontSize: 12, fontWeight: 600 }}>{t().editor.palette}</span>
              {(['write', 'critique', 'rewrite', 'rebut'] as const).map((job) => (
                <Button key={job} size="sm" variant="outline" onClick={() => add(job)}>
                  {t().editor.addSeat(t().job[job])}
                </Button>
              ))}
              <Button size="sm" variant="outline" onClick={() => add('summarise')}>{t().editor.addSummariser}</Button>
              <Button size="sm" variant="ghost" onClick={() => setRelayout((n) => n + 1)}>{t().editor.relayout}</Button>
            </Row>
            <div style={{ ...muted, fontSize: 12 }}>{t().editor.connectHint}</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 340px', gap: 'var(--sg-s-5)', alignItems: 'start' }}>
              <FlowCanvas
                flow={flow}
                names={names}
                editable
                selection={selection}
                marked={marked}
                relayout={relayout}
                height="62vh"
                onSelect={setSelection}
                onConnect={(from, to, exit) => edit(addLink(flow, from, to, exit))}
                onPositions={onPositions}
              />
              <div style={{ maxHeight: '62vh', overflowY: 'auto', paddingRight: 4 }}>{panel}</div>
            </div>
          </>
        )}
        <Row style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 'var(--sg-s-3)' }}>
          <span style={{ ...muted, fontSize: 12 }}>{t().editor.callsPerRound(callsPerRound(flow))}</span>
          {issues.length ? <span style={{ fontSize: 12, color: 'var(--sg-crit)' }}>{t().editor.clientIssues(issues.length)}</span> : null}
        </Row>
        <NoticeList tone="warn" items={issues.map((i) => (i.target === 'flow' ? issueText(i) : `${label(i.target)}: ${issueText(i)}`))} />
        <NoticeList tone="warn" items={warnings} />
        {error ? <Notice tone="crit">{error}</Notice> : null}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--sg-s-4)' }}>
          <Button variant="ghost" disabled={busy} onClick={onClose}>{tr.common.cancel}</Button>
          <Button variant="primary" loading={busy} onClick={() => void save()}>
            {entry && !custom ? t().editor.saveCopy : t().editor.save}
          </Button>
        </div>
      </Stack>
    </Modal>
  );
}
