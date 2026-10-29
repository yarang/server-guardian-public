import { Button, Panel, StatusChip } from '@server-guardian/ui';
import { listPlans } from '../../api';
import { DataTable } from '../../components/DataTable';
import type { Column } from '../../components/DataTable';
import { Notice } from '../../components/Notice';
import { Stack, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { planState, planStateTone, riskLabel, riskTone } from '../../lib/debate';
import type { PlanItem } from '../../types';
import { tr } from '../../i18n';

/**
 * Plans of the project (package installs and removals ordered over nodes), newest first. Shown only when
 * the project has plans, above nothing else: the approvals list keeps working without it.
 */
export function PlansPanel({ version, project, onOpen }: { version: number; project: string; onOpen: (id: string) => void }) {
  return <PlansList key={`${version}:${project}`} project={project} onOpen={onOpen} />;
}

function PlansList({ project, onOpen }: { project: string; onOpen: (id: string) => void }) {
  const state = useHubData(() => listPlans(undefined, project || undefined));
  if (state.error) return <Notice tone="warn">{tr.debate.plans.loadFailed(tr.debate.plans.what, state.error)}</Notice>;
  const plans = state.data ?? [];
  if (plans.length === 0) return null;
  const columns: Column<PlanItem>[] = [
    {
      header: tr.debate.plans.colPlan,
      mono: true,
      render: (p) => (
        <Button size="sm" variant="outline" aria-label={tr.debate.plans.dialogTitle(p.id)} onClick={() => onOpen(p.id)}>
          {p.id}
        </Button>
      ),
    },
    {
      header: tr.debate.plans.colGoal,
      render: (p) => (
        <span style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }} title={p.goal ?? ''}>
          {p.goal}
        </span>
      ),
    },
    { header: tr.debate.plans.colNodes, mono: true, render: (p) => p.hosts.map((h) => h.host).join(', ') },
    { header: tr.debate.plans.colSteps, align: 'right', render: (p) => String(p.steps.length) },
    { header: tr.debate.plans.colRisk, render: (p) => <StatusChip tone={riskTone(p.risk)} title={p.risk}>{riskLabel(p.risk)}</StatusChip> },
    {
      header: tr.debate.plans.colStatus,
      render: (p) => {
        const s = planState(p);
        return <StatusChip tone={planStateTone(s)}>{tr.debate.plans.state[s]}</StatusChip>;
      },
    },
  ];
  return (
    <Panel title={tr.debate.plans.title}>
      <Stack gap="var(--sg-s-3)">
        <div style={{ ...muted, fontSize: 12 }}>{tr.debate.plans.count(plans.length)}</div>
        <DataTable label={tr.debate.plans.title} columns={columns} rows={plans} getKey={(p) => p.id} empty={tr.debate.plans.empty} />
      </Stack>
    </Panel>
  );
}
