import { TextField } from '@server-guardian/ui';
import { CheckboxField } from '../../components/CheckboxField';
import { TextAreaField } from '../../components/TextAreaField';
import { Stack } from '../../components/layout';
import { BATCH_DEFAULTS, OPS_OPT_IN_TOOLS, OPS_TOOLS, lines } from '../../lib/project';
import type { ProjectData } from '../../types';
import { tr } from '../../i18n';

/** Form state of an ops project (targets and policy) as the dialog edits it. */
export interface OpsForm {
  all: boolean;
  groups: string;
  nodes: string;
  exclude: string;
  allowed: string[];
  includeHub: boolean;
  denyPaths: string;
  denyCommands: string;
  protectedPackages: string;
  autoRun: string[];
  maxAutoRuns: string;
  batchAuto: boolean;
  batchPlans: string;
  batchSteps: string;
  batchHosts: string;
  /** policy.batch.scripts === 'vote': scripts on the listed groups run on the critics' vote. */
  batchScriptsVote: boolean;
  scriptGroups: string;
  /** Policy keys the form does not edit (watch); sent back as they came so a save does not drop them. */
  extra: Record<string, unknown>;
}

export function opsFormOf(data: ProjectData): OpsForm {
  const t = data.targets ?? {};
  const p = data.policy ?? {};
  return {
    all: Boolean(t.all),
    groups: (t.groups ?? []).join('\n'),
    nodes: (t.nodes ?? []).join('\n'),
    exclude: (t.exclude ?? []).join('\n'),
    allowed: p.allowed_actions ?? OPS_TOOLS.map((tool) => tool.name),
    includeHub: Boolean(p.include_hub),
    denyPaths: (p.deny_paths ?? []).join('\n'),
    denyCommands: (p.deny_commands ?? []).join('\n'),
    protectedPackages: (p.protected_packages ?? []).join('\n'),
    autoRun: p.auto_run ?? [],
    maxAutoRuns: String(p.max_auto_runs_per_day ?? 0),
    batchAuto: p.batch?.auto === true,
    batchPlans: String(p.batch?.max_plans_per_day ?? BATCH_DEFAULTS.maxPlansPerDay),
    batchSteps: String(p.batch?.max_steps ?? BATCH_DEFAULTS.maxSteps),
    batchHosts: String(p.batch?.max_parallel_hosts ?? BATCH_DEFAULTS.maxParallelHosts),
    batchScriptsVote: p.batch?.scripts === 'vote',
    scriptGroups: (p.batch?.script_groups ?? []).join('\n'),
    extra: p.watch === undefined ? {} : { watch: p.watch },
  };
}

/** The targets and policy of a project file from the form. The hub validates them again (ids, tools, limits). */
export function opsDataOf(form: OpsForm): Pick<ProjectData, 'targets' | 'policy'> {
  const max = Number(form.maxAutoRuns);
  const count = (text: string, fallback: number) => (text.trim() !== '' && Number.isFinite(Number(text)) ? Number(text) : fallback);
  return {
    targets: { all: form.all, groups: lines(form.groups), nodes: lines(form.nodes), exclude: lines(form.exclude) },
    policy: {
      ...form.extra,
      allowed_actions: form.allowed,
      include_hub: form.includeHub,
      deny_paths: lines(form.denyPaths),
      deny_commands: lines(form.denyCommands),
      protected_packages: lines(form.protectedPackages),
      auto_run: form.autoRun.filter((t) => form.allowed.includes(t)),
      max_auto_runs_per_day: Number.isFinite(max) ? max : 0,
      batch: {
        auto: form.batchAuto,
        max_plans_per_day: count(form.batchPlans, BATCH_DEFAULTS.maxPlansPerDay),
        max_steps: count(form.batchSteps, BATCH_DEFAULTS.maxSteps),
        max_parallel_hosts: count(form.batchHosts, BATCH_DEFAULTS.maxParallelHosts),
        scripts: form.batchScriptsVote ? 'vote' : 'approve',
        script_groups: form.batchScriptsVote ? lines(form.scriptGroups) : [],
      },
    },
  };
}

function toggle(list: string[], name: string, on: boolean): string[] {
  return on ? [...list.filter((n) => n !== name), name] : list.filter((n) => n !== name);
}

/** Targets (groups, nodes, exclusions or every node) and the policy (tools, hub, denied paths, auto_run). */
export function OpsProjectFields({ form, onChange }: { form: OpsForm; onChange: (form: OpsForm) => void }) {
  const P = tr.debate.project.ops;
  const set = (patch: Partial<OpsForm>) => onChange({ ...form, ...patch });
  return (
    <Stack gap="var(--sg-s-6)">
      <fieldset style={{ border: 0, padding: 0, margin: 0, display: 'grid', gap: 'var(--sg-s-4)' }}>
        <legend style={{ fontWeight: 600, marginBottom: 'var(--sg-s-3)' }}>{P.targets}</legend>
        <CheckboxField checked={form.all} onChange={(e) => set({ all: e.target.checked })}>
          {P.all}
        </CheckboxField>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 'var(--sg-s-4)' }}>
          <TextAreaField label={P.groups} mono rows={3} value={form.groups} onChange={(e) => set({ groups: e.target.value })} />
          <TextAreaField label={P.nodes} mono rows={3} value={form.nodes} onChange={(e) => set({ nodes: e.target.value })} />
          <TextAreaField label={P.exclude} mono rows={3} value={form.exclude} onChange={(e) => set({ exclude: e.target.value })} />
        </div>
        <CheckboxField checked={form.includeHub} onChange={(e) => set({ includeHub: e.target.checked })}>
          {P.includeHub}
        </CheckboxField>
      </fieldset>
      <fieldset style={{ border: 0, padding: 0, margin: 0, display: 'grid', gap: 'var(--sg-s-3)' }}>
        <legend style={{ fontWeight: 600, marginBottom: 'var(--sg-s-3)' }}>{P.allowed}</legend>
        {[...OPS_TOOLS, ...OPS_OPT_IN_TOOLS].map((tool) => (
          <CheckboxField
            key={tool.name}
            checked={form.allowed.includes(tool.name)}
            onChange={(e) => set({ allowed: toggle(form.allowed, tool.name, e.target.checked) })}
          >
            <span style={{ fontFamily: 'var(--sg-font-mono)' }}>{tool.name}</span>
          </CheckboxField>
        ))}
      </fieldset>
      <TextAreaField
        label={P.denyPaths}
        hint={P.denyHint}
        mono
        rows={3}
        placeholder={'/srv/data/*'}
        value={form.denyPaths}
        onChange={(e) => set({ denyPaths: e.target.value })}
      />
      <TextAreaField
        label={P.denyCommands}
        hint={P.denyCommandsHint}
        mono
        rows={3}
        placeholder={'docker system prune*'}
        value={form.denyCommands}
        onChange={(e) => set({ denyCommands: e.target.value })}
      />
      <TextAreaField
        label={P.protectedPackages}
        hint={P.protectedPackagesHint}
        mono
        rows={3}
        placeholder={'nginx*'}
        value={form.protectedPackages}
        onChange={(e) => set({ protectedPackages: e.target.value })}
      />
      <fieldset style={{ border: 0, padding: 0, margin: 0, display: 'grid', gap: 'var(--sg-s-3)' }}>
        <legend style={{ fontWeight: 600, marginBottom: 'var(--sg-s-3)' }}>{P.autoRun}</legend>
        <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13, lineHeight: 1.5 }}>{P.autoRunHint}</div>
        {OPS_TOOLS.filter((tool) => tool.auto).map((tool) => (
          <CheckboxField
            key={tool.name}
            checked={form.autoRun.includes(tool.name)}
            disabled={!form.allowed.includes(tool.name)}
            onChange={(e) => set({ autoRun: toggle(form.autoRun, tool.name, e.target.checked) })}
          >
            <span style={{ fontFamily: 'var(--sg-font-mono)' }}>{tool.name}</span>
          </CheckboxField>
        ))}
        <TextField
          label={P.maxAutoRuns}
          type="number"
          min={0}
          max={100}
          step={1}
          value={form.maxAutoRuns}
          onChange={(e) => set({ maxAutoRuns: e.target.value })}
        />
      </fieldset>
      <fieldset style={{ border: 0, padding: 0, margin: 0, display: 'grid', gap: 'var(--sg-s-3)' }}>
        <legend style={{ fontWeight: 600, marginBottom: 'var(--sg-s-3)' }}>{P.batch}</legend>
        <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13, lineHeight: 1.5 }}>{P.batchHint}</div>
        <CheckboxField checked={form.batchAuto} onChange={(e) => set({ batchAuto: e.target.checked, ...(e.target.checked && Number(form.batchPlans) < 1 ? { batchPlans: '1' } : {}) })}>
          {P.batchAuto}
        </CheckboxField>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 'var(--sg-s-4)' }}>
          <TextField label={P.batchPlans} type="number" min={0} max={100} step={1} value={form.batchPlans} onChange={(e) => set({ batchPlans: e.target.value })} />
          <TextField label={P.batchSteps} type="number" min={1} max={100} step={1} value={form.batchSteps} onChange={(e) => set({ batchSteps: e.target.value })} />
          <TextField label={P.batchHosts} type="number" min={1} max={8} step={1} value={form.batchHosts} onChange={(e) => set({ batchHosts: e.target.value })} />
        </div>
        <CheckboxField checked={form.batchScriptsVote} disabled={!form.batchAuto} onChange={(e) => set({ batchScriptsVote: e.target.checked })}>
          {P.batchScriptsVote}
        </CheckboxField>
        {form.batchScriptsVote && (
          <TextAreaField label={P.scriptGroups} hint={P.scriptGroupsHint} mono rows={2} value={form.scriptGroups} onChange={(e) => set({ scriptGroups: e.target.value })} />
        )}
      </fieldset>
    </Stack>
  );
}
