import { Button, TextField } from '@server-guardian/ui';
import { Caption } from '../../components/Card';
import { CheckboxField } from '../../components/CheckboxField';
import { SelectField } from '../../components/SelectField';
import { TextAreaField } from '../../components/TextAreaField';
import { Row, Stack, muted } from '../../components/layout';
import { CLOSE, JOBS, START, TEXT_ONE_LINE_MAX, characterFits, inputsOf, linkKey, textParamType, unitOf } from '../../lib/flowGraph';
import type { FlowData, FlowJob, FlowLink, FlowParamSpec, FlowSeat, PersonaConfig, TeamCatalog } from '../../types';
import { tr } from '../../i18n';

const t = () => tr.flows;
const e = () => tr.flows.editor;
const CATEGORIES = ['development', 'operations', 'design-docs', 'general'];
const PROJECT_KINDS = ['code', 'ops', 'design'] as const;
const INPUT_MODES = ['raw', 'condensed', 'structured', 'latest'] as const;
const MAX_PARAMS = 8;

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'grid', gap: 'var(--sg-s-2)' }}>
      <div style={{ color: 'var(--sg-text-secondary)', fontSize: 12, fontWeight: 600 }}>{label}</div>
      {children}
    </div>
  );
}

/** Ordered provider choice as checkboxes in the catalog's order (an empty list means the host default). */
function Providers({ value, catalog, onChange }: { value: string[]; catalog: TeamCatalog | null; onChange: (v: string[]) => void }) {
  const all = catalog?.providers ?? ['claude', 'codex', 'agy'];
  return (
    <Field label={e().providers}>
      <Row gap="var(--sg-s-4)" style={{ flexWrap: 'wrap' }}>
        {all.map((p) => (
          <CheckboxField key={p} checked={value.includes(p)} onChange={(ev) => onChange(ev.target.checked ? [...value, p] : value.filter((x) => x !== p))}>
            {p}
          </CheckboxField>
        ))}
      </Row>
    </Field>
  );
}

/**
 * Where a seat or the close runs (`host`: the hub, a placement label or a worker) and whether it reads code through the
 * read-only repository MCP (`code_tools: "mcp"`), as the team editor offered them (remote-agents.md W4b). The host field
 * stays out of the way unless the hub has a worker or the value is already set.
 */
function Placement({ host, codeTools, catalog, onChange }: {
  host: string | null | undefined;
  codeTools: string | null | undefined;
  catalog: TeamCatalog | null;
  onChange: (change: { host?: string; code_tools?: string }) => void;
}) {
  const labels = catalog?.host_labels ?? [];
  const workers = catalog?.workers ?? [];
  const known = [...labels, ...workers];
  const showHost = workers.length > 0 || !!host;
  return (
    <>
      {showHost ? (
        <Stack gap="var(--sg-s-2)">
          <SelectField
            label={e().host}
            value={host ?? ''}
            onChange={(ev) => onChange({ host: ev.target.value || undefined })}
            options={[
              { value: '', label: e().hostHub },
              ...labels.map((v) => ({ value: v, label: e().hostLabel(v) })),
              ...workers.map((v) => ({ value: v, label: v })),
              ...(host && !known.includes(host) ? [{ value: host, label: e().hostNotWorker(host) }] : []),
            ]}
          />
          <div style={{ ...muted, fontSize: 12 }}>{e().hostNote}</div>
        </Stack>
      ) : null}
      <Stack gap="var(--sg-s-2)">
        <CheckboxField checked={codeTools === 'mcp'} onChange={(ev) => onChange({ code_tools: ev.target.checked ? 'mcp' : undefined })}>
          {e().codeTools}
        </CheckboxField>
        {codeTools === 'mcp' ? <div style={{ ...muted, fontSize: 12 }}>{e().mcpNote}</div> : null}
      </Stack>
    </>
  );
}

/** The character select of a job: every persona, the ones that cannot take the job marked and disabled. */
function CharacterSelect({ value, job, mode, personas, onChange }: { value: string; job: FlowJob | 'close'; mode?: string; personas: PersonaConfig[]; onChange: (v: string) => void }) {
  return (
    <SelectField
      label={e().character}
      value={value}
      onChange={(ev) => onChange(ev.target.value)}
      options={[
        { value: '', label: e().characterNone },
        ...personas.map((p) => {
          const fits = characterFits(p, job, mode);
          return { value: p.id, label: fits ? `${p.name || p.id} (${p.id})` : e().characterUnfit(p.name || p.id), disabled: !fits && p.id !== value };
        }),
      ]}
    />
  );
}

function Receives({ flow, id, names }: { flow: FlowData; id: string; names: (id: string) => string }) {
  const inputs = inputsOf(flow, id);
  const element = id === CLOSE ? flow.close : flow.seats.find((s) => s.id === id);
  const mode = t().inputMode[element?.input ?? 'raw'];
  return (
    <Field label={e().receives}>
      {inputs.length === 0 ? (
        <div style={{ ...muted, fontSize: 12 }}>{e().receivesNone}</div>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
          {inputs.map((i) => (
            <li key={`${i.from}${i.lookBack}`}>
              {e().receivesItem(names(i.from), mode)}
              {i.lookBack ? e().lookBackSuffix : ''}
              {i.blocked ? e().blockedSuffix : ''}
            </li>
          ))}
        </ul>
      )}
    </Field>
  );
}

export function SeatPanel({ flow, seat, personas, catalog, names, onChange, onBundle, onRemove }: {
  flow: FlowData;
  seat: FlowSeat;
  personas: PersonaConfig[];
  catalog: TeamCatalog | null;
  names: (id: string) => string;
  onChange: (change: Partial<FlowSeat>) => void;
  onBundle: (target: string | null | 'new') => void;
  onRemove: () => void;
}) {
  const bundle = unitOf(flow, seat.id);
  return (
    <Stack gap="var(--sg-s-5)">
      <Caption>{e().seatPanel(seat.id)}</Caption>
      <SelectField
        label={e().jobLabel}
        value={seat.job}
        onChange={(ev) => onChange({ job: ev.target.value as FlowJob })}
        options={JOBS.map((j) => ({ value: j, label: `${t().job[j]} — ${t().jobHint[j]}` }))}
      />
      {seat.job === 'critique' ? (
        <SelectField label={e().mode} value={seat.mode ?? 'object'} onChange={(ev) => onChange({ mode: ev.target.value })} options={['object', 'checklist'].map((m) => ({ value: m, label: t().critiqueMode[m] }))} />
      ) : null}
      {seat.job === 'summarise' ? null : (
        <CharacterSelect value={seat.character ?? ''} job={seat.job} mode={seat.mode} personas={personas} onChange={(v) => onChange({ character: v })} />
      )}
      {seat.job === 'rebut' ? <TextField label={e().stance} value={seat.stance ?? ''} onChange={(ev) => onChange({ stance: ev.target.value })} /> : null}
      {seat.job === 'summarise' ? (
        <TextAreaField label={e().instruction} rows={3} value={seat.instruction ?? ''} onChange={(ev) => onChange({ instruction: ev.target.value })} />
      ) : null}
      <Providers value={seat.providers ?? []} catalog={catalog} onChange={(v) => onChange({ providers: v })} />
      {seat.job === 'summarise' ? null : <Placement host={seat.host} codeTools={seat.code_tools} catalog={catalog} onChange={onChange} />}
      {seat.job === 'summarise' ? null : (
        <SelectField
          label={e().bundle}
          value={bundle === seat.id ? '' : bundle}
          onChange={(ev) => onBundle(ev.target.value === '' ? null : ev.target.value)}
          options={[
            { value: '', label: e().bundleNone },
            ...(flow.bundles ?? []).map((b) => ({ value: b.id, label: b.id })),
            { value: 'new', label: e().bundleNew },
          ]}
        />
      )}
      <SelectField label={e().input} value={seat.input ?? 'raw'} onChange={(ev) => onChange({ input: ev.target.value as FlowSeat['input'] })} options={INPUT_MODES.map((m) => ({ value: m, label: t().inputMode[m] }))} />
      <div style={{ ...muted, fontSize: 12 }}>{e().inputNote}</div>
      <Receives flow={flow} id={seat.id} names={names} />
      <div>
        <Button size="sm" variant="danger" onClick={onRemove}>{e().remove}</Button>
      </div>
    </Stack>
  );
}

export function BundlePanel({ flow, id, onChange, onRemove }: { flow: FlowData; id: string; onChange: (exchanges: number) => void; onRemove: () => void }) {
  const bundle = (flow.bundles ?? []).find((b) => b.id === id);
  if (!bundle) return null;
  const rebut = bundle.seats.some((s) => flow.seats.find((x) => x.id === s)?.job === 'rebut');
  return (
    <Stack gap="var(--sg-s-5)">
      <Caption>{e().bundlePanel(id)}</Caption>
      <div style={{ fontSize: 12 }}>{bundle.seats.join(', ')}</div>
      {rebut ? (
        <SelectField label={e().exchanges} value={String(bundle.exchanges ?? 1)} onChange={(ev) => onChange(Number(ev.target.value))} options={[{ value: '1', label: '1' }, { value: '2', label: '2' }]} />
      ) : null}
      <div>
        <Button size="sm" variant="danger" onClick={onRemove}>{e().remove}</Button>
      </div>
    </Stack>
  );
}

export function ClosePanel({ flow, personas, catalog, names, onChange }: {
  flow: FlowData;
  personas: PersonaConfig[];
  catalog: TeamCatalog | null;
  names: (id: string) => string;
  onChange: (change: Partial<FlowData['close']>) => void;
}) {
  const close = flow.close;
  const tools = catalog?.tools ?? [];
  const chosen = close.allowed_tools;
  return (
    <Stack gap="var(--sg-s-5)">
      <Caption>{e().closePanel}</Caption>
      <CharacterSelect value={close.character} job="close" personas={personas} onChange={(v) => onChange({ character: v })} />
      <Providers value={close.providers ?? []} catalog={catalog} onChange={(v) => onChange({ providers: v })} />
      <Placement host={close.host} codeTools={close.code_tools} catalog={catalog} onChange={onChange} />
      <SelectField label={e().input} value={close.input ?? 'raw'} onChange={(ev) => onChange({ input: ev.target.value as FlowSeat['input'] })} options={INPUT_MODES.filter((m) => m !== 'latest').map((m) => ({ value: m, label: t().inputMode[m] }))} />
      <Field label={e().tools}>
        <Stack gap="var(--sg-s-2)">
          {tools.map((tool) => (
            <CheckboxField
              key={tool.name}
              checked={(chosen ?? []).includes(tool.name)}
              onChange={(ev) => {
                const next = ev.target.checked ? [...(chosen ?? []), tool.name] : (chosen ?? []).filter((x) => x !== tool.name);
                onChange({ allowed_tools: next.length ? next : undefined });
              }}
            >
              <span style={{ fontSize: 12 }}>{tool.name}</span>
            </CheckboxField>
          ))}
        </Stack>
      </Field>
      <Receives flow={flow} id={CLOSE} names={names} />
    </Stack>
  );
}

export function LinkPanel({ link, names, onChange, onRemove }: { link: FlowLink; names: (id: string) => string; onChange: (change: Partial<FlowLink>) => void; onRemove: () => void }) {
  return (
    <Stack gap="var(--sg-s-5)">
      <Caption>{e().linkPanel}</Caption>
      <div style={{ fontSize: 12 }}>
        {names(link.from)} → {names(link.to)}
        {link.exit ? ` · ${t().exit[link.exit]}` : ''}
      </div>
      {link.exit === 'rejected' ? (
        <TextField
          label={e().maxRounds}
          type="number"
          min={1}
          max={10}
          value={link.max_rounds === undefined ? '' : String(link.max_rounds)}
          onChange={(ev) => onChange({ max_rounds: ev.target.value === '' ? undefined : Number(ev.target.value) })}
        />
      ) : null}
      {link.exit || link.from === START ? null : (
        <>
          <CheckboxField checked={!!link.look_back} onChange={(ev) => onChange({ look_back: ev.target.checked || undefined })}>{e().lookBack}</CheckboxField>
          <CheckboxField checked={!!link.blocked} onChange={(ev) => onChange({ blocked: ev.target.checked || undefined })}>{e().blocked}</CheckboxField>
        </>
      )}
      {link.exit ? null : (
        <div>
          <Button size="sm" variant="danger" onClick={onRemove}>{e().remove}</Button>
        </div>
      )}
    </Stack>
  );
}

type ParamKind = 'text' | 'int' | 'enum';

function kindOf(spec: FlowParamSpec): ParamKind {
  return spec.type === 'int' ? 'int' : spec.type === 'enum' ? 'enum' : 'text';
}

/** One parameter (D8): text (the box follows the maximum length), number, or a choice. */
function ParamRow({ name, spec, onRename, onChange, onRemove }: { name: string; spec: FlowParamSpec; onRename: (n: string) => void; onChange: (s: FlowParamSpec) => void; onRemove: () => void }) {
  const kind = kindOf(spec);
  const setKind = (k: ParamKind) => {
    const base = { label: spec.label };
    onChange(k === 'text' ? { ...base, type: 'string', max: TEXT_ONE_LINE_MAX } : k === 'int' ? { ...base, type: 'int' } : { ...base, type: 'enum', values: [] });
  };
  return (
    <div style={{ border: '1px solid var(--sg-border-subtle)', borderRadius: 'var(--sg-r-md)', padding: 'var(--sg-s-4)' }}>
      <Stack gap="var(--sg-s-3)">
        <Row gap="var(--sg-s-4)" style={{ flexWrap: 'wrap' }}>
          <TextField label={e().paramName} mono value={name} onChange={(ev) => onRename(ev.target.value.toUpperCase())} />
          <SelectField label={e().paramType} value={kind} onChange={(ev) => setKind(ev.target.value as ParamKind)} options={(['text', 'int', 'enum'] as const).map((k) => ({ value: k, label: e().paramTypeOf[k] }))} />
        </Row>
        <TextField label={e().paramLabel} value={spec.label ?? ''} onChange={(ev) => onChange({ ...spec, label: ev.target.value || undefined })} />
        {kind === 'text' ? (
          <TextField
            label={e().paramMax}
            type="number"
            min={1}
            max={4000}
            hint={e().paramMaxHint}
            value={String(spec.max ?? TEXT_ONE_LINE_MAX)}
            onChange={(ev) => {
              const max = Math.max(1, Math.min(4000, Number(ev.target.value) || 1));
              onChange({ ...spec, type: textParamType(max), max });
            }}
          />
        ) : null}
        {kind === 'int' ? (
          <Row gap="var(--sg-s-4)">
            <TextField label={e().paramMin} type="number" value={spec.min === undefined ? '' : String(spec.min)} onChange={(ev) => onChange({ ...spec, min: ev.target.value === '' ? undefined : Number(ev.target.value) })} />
            <TextField label={e().paramMaxInt} type="number" value={spec.max === undefined ? '' : String(spec.max)} onChange={(ev) => onChange({ ...spec, max: ev.target.value === '' ? undefined : Number(ev.target.value) })} />
          </Row>
        ) : null}
        {kind === 'enum' ? (
          <TextAreaField label={e().paramValues} rows={3} value={(spec.values ?? []).join('\n')} onChange={(ev) => onChange({ ...spec, values: ev.target.value.split('\n').map((v) => v.trim()).filter(Boolean) })} />
        ) : null}
        <TextField label={e().paramDefault} value={spec.default === undefined ? '' : String(spec.default)} onChange={(ev) => onChange({ ...spec, default: ev.target.value === '' ? undefined : kind === 'int' ? Number(ev.target.value) : ev.target.value })} />
        <div>
          <Button size="sm" variant="ghost" aria-label={e().removeParam(name)} onClick={onRemove}>{e().remove}</Button>
        </div>
      </Stack>
    </div>
  );
}

export function FlowPanel({ flow, idEditable, onChange }: { flow: FlowData; idEditable: boolean; onChange: (change: Partial<FlowData>) => void }) {
  const params = Object.entries(flow.params ?? {});
  const setParams = (entries: Array<[string, FlowParamSpec]>) => onChange({ params: Object.fromEntries(entries) });
  const rejected = flow.links.find((l) => l.from === CLOSE && l.exit === 'rejected');
  const projects = flow.projects ?? [];
  return (
    <Stack gap="var(--sg-s-5)">
      <Caption>{e().flowPanel}</Caption>
      <TextField label={e().id} mono value={flow.id} readOnly={!idEditable} onChange={(ev) => onChange({ id: ev.target.value })} />
      <TextField label={e().name} value={flow.name} onChange={(ev) => onChange({ name: ev.target.value })} />
      <SelectField label={e().category} value={flow.category ?? 'general'} onChange={(ev) => onChange({ category: ev.target.value })} options={CATEGORIES.map((c) => ({ value: c, label: t().category[c] }))} />
      <Field label={e().projects}>
        <Row gap="var(--sg-s-4)">
          {PROJECT_KINDS.map((k) => (
            <CheckboxField key={k} checked={projects.includes(k)} onChange={(ev) => onChange({ projects: ev.target.checked ? [...projects, k] : projects.filter((x) => x !== k) })}>
              {t().projectKind[k]}
            </CheckboxField>
          ))}
        </Row>
      </Field>
      {rejected ? (
        <TextField
          label={e().maxRounds}
          type="number"
          min={1}
          max={10}
          value={rejected.max_rounds === undefined ? '' : String(rejected.max_rounds)}
          onChange={(ev) => onChange({ links: flow.links.map((l) => (linkKey(l) === linkKey(rejected) ? { ...l, max_rounds: ev.target.value === '' ? undefined : Number(ev.target.value) } : l)) })}
        />
      ) : null}
      <TextAreaField label={e().task} rows={4} hint={e().taskHint} value={flow.task ?? ''} onChange={(ev) => onChange({ task: ev.target.value })} />
      <Row style={{ justifyContent: 'space-between' }}>
        <Caption>{e().params(params.length, MAX_PARAMS)}</Caption>
        <Button
          size="sm"
          variant="outline"
          disabled={params.length >= MAX_PARAMS}
          onClick={() => {
            let n = params.length + 1;
            while (flow.params?.[`PARAM${n}`]) n++;
            setParams([...params, [`PARAM${n}`, { type: 'string', max: TEXT_ONE_LINE_MAX }]]);
          }}
        >
          {e().addParam}
        </Button>
      </Row>
      {params.map(([name, spec], i) => (
        <ParamRow
          key={i}
          name={name}
          spec={spec}
          onRename={(n) => setParams(params.map((p, j) => (j === i ? [n, p[1]] : p)))}
          onChange={(s) => setParams(params.map((p, j) => (j === i ? [p[0], s] : p)))}
          onRemove={() => setParams(params.filter((_, j) => j !== i))}
        />
      ))}
    </Stack>
  );
}
