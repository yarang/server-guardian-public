import { tr } from '../i18n';
import { Card } from '../components/Card';
import { Notice } from '../components/Notice';
import { ProjectSwitcher } from '../components/ProjectSwitcher';
import { Stack } from '../components/layout';
import { navigate } from '../router';
import { useApprovalsStore } from '../state/approvalsStore';
import { useProjectStore } from '../state/projectStore';
import { ActionDialog } from './debate/ActionDialog';
import { ActionsPanel } from './debate/ActionsPanel';
import { PlanDialog } from './debate/PlanDialog';
import { PlansPanel } from './debate/PlansPanel';
import { PLAN_ID_RE } from '../lib/debate';

/** `#/approvals/:id` param as an action id, or null when it is not a positive integer. */
function parseActionId(param: string | undefined): number | null {
  if (param === undefined || !/^\d+$/.test(param)) return null;
  const n = Number(param);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

/**
 * Approvals: the project's proposed actions, awaiting decision first. `#/approvals/:id` renders the
 * action dialog (or the plan dialog for `plan_...`) over the list (closing returns to `#/approvals`), so the execute / reject confirmations
 * and the lock while running are exactly the dialog's.
 */
export function ApprovalsPage({ actionId }: { actionId?: string }) {
  const { selected } = useProjectStore();
  const { version, refresh } = useApprovalsStore();
  const planId = actionId !== undefined && PLAN_ID_RE.test(actionId) ? actionId : null;
  const id = planId === null ? parseActionId(actionId) : null;
  return (
    <Stack>
      <Card>
        <ProjectSwitcher />
      </Card>
      {actionId !== undefined && id === null && planId === null ? <Notice tone="warn">{tr.ops.approvals.invalidId(actionId)}</Notice> : null}
      <PlansPanel version={version} project={selected} onOpen={(p) => navigate('approvals', [p])} />
      <ActionsPanel version={version} project={selected} onOpen={(n) => navigate('approvals', [String(n)])} />
      {planId !== null ? <PlanDialog key={planId} id={planId} onClose={() => navigate('approvals')} onChanged={refresh} /> : null}
      {id !== null ? <ActionDialog key={id} id={id} onClose={() => navigate('approvals')} onChanged={refresh} /> : null}
    </Stack>
  );
}
