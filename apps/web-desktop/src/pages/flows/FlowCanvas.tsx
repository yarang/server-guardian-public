import { useEffect, useMemo, useState } from 'react';
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
} from '@xyflow/react';
import type { Connection, Edge, Node, NodeChange, NodeProps } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { BUNDLE_PAD, CLOSE, END, SEAT_H, SEAT_W, START, autoLayout, bundleHeight, linkKey, unitOf } from '../../lib/flowGraph';
import type { FlowData } from '../../types';
import { tr } from '../../i18n';

const t = () => tr.flows;

/** What a click selects on the canvas: an element (seat, bundle, close, start, end) or a link, or nothing (the flow). */
export type CanvasSelection = { kind: 'seat' | 'bundle' | 'close' | 'start' | 'end'; id: string } | { kind: 'link'; id: string } | null;

/** Run state of a seat in the read view. */
export type SeatState = 'ran' | 'failed';

interface CardData extends Record<string, unknown> {
  title: string;
  subtitle?: string;
  kind: 'seat' | 'bundle' | 'close' | 'terminal';
  marked?: boolean;
  selected?: boolean;
  state?: SeatState;
  source?: boolean;
  target?: boolean;
}

function border(data: CardData): string {
  if (data.marked) return '2px solid var(--sg-crit)';
  if (data.selected) return '2px solid var(--sg-accent)';
  if (data.state === 'ran') return '2px solid var(--sg-ok)';
  if (data.state === 'failed') return '2px solid var(--sg-warn, var(--sg-crit))';
  return '1px solid var(--sg-border-strong)';
}

function SeatCard({ data }: NodeProps<Node<CardData>>) {
  return (
    <div
      style={{
        width: SEAT_W,
        minHeight: SEAT_H - 12,
        padding: '8px 10px',
        borderRadius: 'var(--sg-r-md)',
        border: border(data),
        background: data.state === 'ran' ? 'var(--sg-ok-bg)' : 'var(--sg-surface-raised)',
        color: 'var(--sg-text)',
        fontSize: 12,
        boxShadow: 'var(--sg-shadow-1)',
      }}
    >
      {data.target !== false ? <Handle type="target" position={Position.Left} /> : null}
      <div style={{ fontWeight: 600, overflowWrap: 'anywhere' }}>{data.title}</div>
      {data.subtitle ? <div style={{ color: 'var(--sg-text-secondary)', overflowWrap: 'anywhere' }}>{data.subtitle}</div> : null}
      {data.source !== false ? <Handle type="source" position={Position.Right} /> : null}
    </div>
  );
}

function BundleFrame({ data }: NodeProps<Node<CardData>>) {
  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        borderRadius: 'var(--sg-r-panel)',
        border: data.marked ? '2px dashed var(--sg-crit)' : data.selected ? '2px dashed var(--sg-accent)' : '1px dashed var(--sg-border-strong)',
        background: 'var(--sg-surface-sunken)',
        fontSize: 11,
        color: 'var(--sg-text-secondary)',
      }}
    >
      <Handle type="target" position={Position.Left} />
      <div style={{ padding: '6px 10px', fontWeight: 600 }}>{data.title}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function CloseCard({ data }: NodeProps<Node<CardData>>) {
  return (
    <div
      style={{
        width: SEAT_W,
        padding: '8px 10px',
        borderRadius: 'var(--sg-r-md)',
        border: border(data),
        background: 'var(--sg-info-bg)',
        color: 'var(--sg-text)',
        fontSize: 12,
        position: 'relative',
      }}
    >
      <Handle type="target" position={Position.Left} />
      <div style={{ fontWeight: 600 }}>{data.title}</div>
      {data.subtitle ? <div style={{ color: 'var(--sg-text-secondary)', overflowWrap: 'anywhere' }}>{data.subtitle}</div> : null}
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, fontSize: 11 }}>
        <span style={{ color: 'var(--sg-crit)' }}>{t().exit.rejected}</span>
        <span style={{ color: 'var(--sg-ok)' }}>{t().exit.approved}</span>
      </div>
      <Handle id="rejected" type="source" position={Position.Bottom} style={{ left: '20%', background: 'var(--sg-crit)' }} />
      <Handle id="approved" type="source" position={Position.Right} style={{ background: 'var(--sg-ok)' }} />
    </div>
  );
}

const NODE_TYPES = { seat: SeatCard, bundle: BundleFrame, close: CloseCard, terminal: SeatCard };

/** The nodes and edges of a flow for xyflow: bundles are parent nodes, their seats sit inside in order. */
function toCanvas(
  flow: FlowData,
  positions: Record<string, [number, number]>,
  names: Record<string, string>,
  marked: Set<string>,
  selection: CanvasSelection,
  states: Record<string, SeatState>,
): { nodes: Node<CardData>[]; edges: Edge[] } {
  const sel = selection && selection.kind !== 'link' ? selection.id : null;
  const nodes: Node<CardData>[] = [];
  const at = (id: string) => ({ x: positions[id]?.[0] ?? 0, y: positions[id]?.[1] ?? 0 });
  nodes.push({ id: START, type: 'terminal', position: at(START), data: { kind: 'terminal', title: t().fixed.start, target: false, selected: sel === START } });
  for (const bundle of flow.bundles ?? []) {
    nodes.push({
      id: bundle.id,
      type: 'bundle',
      position: at(bundle.id),
      style: { width: SEAT_W + BUNDLE_PAD, height: bundleHeight(bundle.seats.length) },
      data: { kind: 'bundle', title: `${tr.flows.editor.bundle}: ${bundle.id}${bundle.exchanges ? ` · ×${bundle.exchanges}` : ''}`, marked: marked.has(bundle.id), selected: sel === bundle.id },
    });
  }
  for (const seat of flow.seats) {
    const bundle = unitOf(flow, seat.id);
    const index = bundle !== seat.id ? (flow.bundles ?? []).find((b) => b.id === bundle)!.seats.indexOf(seat.id) : -1;
    const who = seat.job === 'summarise' ? (seat.character ? names[seat.character] ?? seat.character : t().job.summarise) : seat.character ? names[seat.character] ?? seat.character : t().editor.characterNone;
    const extra = seat.job === 'critique' && seat.mode === 'checklist' ? ` · ${t().critiqueMode.checklist}` : seat.input && seat.input !== 'raw' ? ` · ${t().inputMode[seat.input]}` : '';
    nodes.push({
      id: seat.id,
      type: 'seat',
      position: index >= 0 ? { x: BUNDLE_PAD / 2, y: 28 + index * (SEAT_H + 12) } : at(seat.id),
      ...(index >= 0 ? { parentId: bundle, extent: 'parent' as const, draggable: false } : {}),
      data: { kind: 'seat', title: `${t().job[seat.job] ?? seat.job} · ${seat.id}`, subtitle: who + extra, marked: marked.has(seat.id), selected: sel === seat.id, state: states[seat.id] },
    });
  }
  const close = flow.close.character ? names[flow.close.character] ?? flow.close.character : t().editor.characterNone;
  nodes.push({ id: CLOSE, type: 'close', position: at(CLOSE), data: { kind: 'close', title: t().fixed.close, subtitle: close + (flow.close.input && flow.close.input !== 'raw' ? ` · ${t().inputMode[flow.close.input]}` : ''), marked: marked.has(CLOSE), selected: sel === CLOSE, state: states[CLOSE] } });
  nodes.push({ id: END, type: 'terminal', position: at(END), data: { kind: 'terminal', title: t().fixed.end, source: false, selected: sel === END } });

  const edges: Edge[] = flow.links.map((l) => {
    const key = linkKey(l);
    const chosen = selection?.kind === 'link' && selection.id === key;
    const color = l.exit === 'rejected' ? 'var(--sg-crit)' : l.exit === 'approved' ? 'var(--sg-ok)' : chosen ? 'var(--sg-accent)' : 'var(--sg-text-secondary)';
    return {
      id: key,
      source: l.from,
      target: l.to,
      ...(l.exit ? { sourceHandle: l.exit } : {}),
      label: l.exit === 'rejected' && l.max_rounds ? `≤ ${l.max_rounds}` : l.look_back ? '↺' : undefined,
      style: { stroke: color, strokeWidth: chosen ? 2.5 : 1.5, strokeDasharray: l.look_back || l.blocked ? '6 4' : undefined, opacity: l.blocked ? 0.5 : 1 },
      markerEnd: { type: MarkerType.ArrowClosed, color },
      type: l.look_back || l.exit === 'rejected' ? 'smoothstep' : 'default',
    };
  });
  return { nodes, edges };
}

/**
 * The diagram of a flow. Editable: drag units, drag a handle onto another element to link (`onConnect`), click to
 * select; positions go back through `onPositions`. Read-only (the run view): no dragging or linking, seats coloured by
 * `states`.
 */
export function FlowCanvas({
  flow,
  names,
  editable = false,
  selection = null,
  marked = new Set<string>(),
  states = {},
  height = 520,
  relayout = 0,
  onSelect,
  onConnect,
  onPositions,
}: {
  flow: FlowData;
  /** Character id -> display name. */
  names: Record<string, string>;
  editable?: boolean;
  selection?: CanvasSelection;
  /** Elements with a problem (seat, bundle or `close` ids). */
  marked?: Set<string>;
  states?: Record<string, SeatState>;
  height?: number | string;
  /** Changing this number lays the units out again by rank, dropping the dragged positions. */
  relayout?: number;
  onSelect?: (selection: CanvasSelection) => void;
  onConnect?: (from: string, to: string, exit?: 'approved' | 'rejected') => void;
  onPositions?: (positions: Record<string, [number, number]>) => void;
}) {
  const [positions, setPositions] = useState<Record<string, [number, number]>>(() => autoLayout(flow));
  // New elements get a position by rank, moved down until they overlap nothing; existing ones keep theirs.
  useEffect(() => {
    setPositions((old) => {
      const auto = { ...autoLayout({ ...flow, layout: undefined }), ...flow.layout };
      const next = { ...old };
      for (const [id, point] of Object.entries(auto)) {
        if (next[id]) continue;
        let [x, y] = point;
        const taken = () => Object.values(next).some(([ox, oy]) => Math.abs(ox - x) < SEAT_W && Math.abs(oy - y) < SEAT_H + 20);
        while (taken()) y += SEAT_H + 40;
        next[id] = [x, y];
      }
      return next;
    });
  }, [flow]);
  useEffect(() => {
    if (relayout) setPositions(autoLayout({ ...flow, layout: undefined }));
    // only an explicit request lays out again
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [relayout]);
  useEffect(() => {
    onPositions?.(positions);
  }, [positions, onPositions]);

  const { nodes, edges } = useMemo(() => toCanvas(flow, positions, names, marked, selection, states), [flow, positions, names, marked, selection, states]);

  // Only a drag moves a unit. Measuring (xyflow reports node sizes after a render) must not write positions back:
  // it would carry the render's stale position of an element added in the same render.
  const onNodesChange = (changes: NodeChange<Node<CardData>>[]) => {
    if (!editable) return;
    const moves = changes.filter((c): c is NodeChange<Node<CardData>> & { type: 'position'; id: string; position: { x: number; y: number } } =>
      c.type === 'position' && !!c.position && unitOf(flow, c.id) === c.id);
    if (!moves.length) return;
    setPositions((old) => {
      const next = { ...old };
      for (const c of moves) next[c.id] = [Math.round(c.position.x), Math.round(c.position.y)];
      return next;
    });
  };

  const connect = (c: Connection) => {
    if (!c.source || !c.target) return;
    const exit = c.source === CLOSE ? ((c.sourceHandle as 'approved' | 'rejected' | null) ?? undefined) : undefined;
    onConnect?.(c.source, c.target, exit);
  };

  return (
    <div style={{ height, border: '1px solid var(--sg-border-subtle)', borderRadius: 'var(--sg-r-md)', background: 'var(--sg-bg)' }} aria-label={t().editor.canvasAria} role="group">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodesChange={onNodesChange}
        onConnect={editable ? connect : undefined}
        onNodeClick={(_, node) => onSelect?.({ kind: node.id === CLOSE ? 'close' : node.id === START ? 'start' : node.id === END ? 'end' : node.type === 'bundle' ? 'bundle' : 'seat', id: node.id })}
        onEdgeClick={(_, edge) => onSelect?.({ kind: 'link', id: edge.id })}
        onPaneClick={() => onSelect?.(null)}
        nodesDraggable={editable}
        nodesConnectable={editable}
        elementsSelectable
        deleteKeyCode={null}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        minZoom={0.3}
      >
        <Background gap={16} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
