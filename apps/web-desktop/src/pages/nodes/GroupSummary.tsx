import { tr } from '../../i18n';
import { useEffect } from 'react';
import { Button, Panel, StatusChip } from '@server-guardian/ui';
import { fetchFleetSSHAudit } from '../../api';
import { Ago } from '../../components/Ago';
import { ErrorNotice } from '../../components/DataGate';
import { Row, Stack, muted } from '../../components/layout';
import { useRefreshToken } from '../../hooks/refresh';
import { useHubAction } from '../../hooks/useHubAction';
import { buildGroupDebateTask } from '../../lib/debateTask';
import { groupReachability, rulesText } from '../../lib/groups';
import { fleetHref, navigate } from '../../router';
import { setDebatePrefill } from '../../state/debatePrefill';
import type { DiagnosisState } from '../../state/hubStore';
import type { NodeGroup, FleetNode } from '../../types';

/**
 * Summary above the inventory when `?group=<id>` is set: definition, member count, reachability
 * from the shared fleet diagnosis (never probed here), the members' SSH audit (one cached call)
 * and the actions that start from a group.
 */
export function GroupSummary({ group, servers, diagnosis }: { group: NodeGroup; servers: FleetNode[]; diagnosis: DiagnosisState }) {
  const audit = useHubAction(fetchFleetSSHAudit);
  const refreshToken = useRefreshToken();
  const run = audit.run;
  const memberKey = group.members.join(',');
  // One non-forced audit of just these members (the hub caches per target set for 60 s).
  useEffect(() => {
    if (memberKey) void run(memberKey.split(','), false);
  }, [run, memberKey, refreshToken]);

  const r = groupReachability(group, servers, diagnosis.data);
  const summary = audit.data?.summary ?? null;
  const failing = audit.data ? audit.data.nodes.filter((n) => summary?.failing_nodes.includes(n.id)).map((n) => n.name) : [];

  const definition =
    [
      group.rules.length ? tr.fleet.groupSummary.rules(rulesText(group.rules)) : '',
      group.include.length ? tr.fleet.groupSummary.picked(group.include.length) : '',
      group.exclude.length ? tr.fleet.groupSummary.excluded(group.exclude.length) : '',
    ]
      .filter(Boolean)
      .join(' · ') || tr.fleet.groupSummary.noDefinition;

  const debate = () => {
    setDebatePrefill(
      buildGroupDebateTask(group, servers, diagnosis.data, audit.data ?? (audit.error !== null ? null : undefined)),
      'group',
      { group: group.id },
    );
    navigate('debates');
  };

  return (
    <Panel
      title={tr.fleet.groupSummary.title(group.name)}
      actions={
        <Row>
          <a href={fleetHref('commands', [], { target: `group:${group.id}` })}>{tr.fleet.groupSummary.runOnGroup}</a>
          <a href={fleetHref('groups', [], { edit: group.id })}>{tr.fleet.groupSummary.editGroup}</a>
          <Button size="sm" variant="primary" onClick={debate}>
            {tr.fleet.groupSummary.debateGroup}
          </Button>
        </Row>
      }
    >
      <Stack gap="var(--sg-s-5)">
        {group.description ? <div style={{ ...muted, fontSize: 13 }}>{group.description}</div> : null}
        <Row gap="var(--sg-s-6)" style={{ fontSize: 13 }}>
          <StatusChip tone={group.members.length ? 'info' : 'idle'}>{tr.fleet.groupSummary.members(group.members.length)}</StatusChip>
          <span style={{ ...muted, overflowWrap: 'anywhere' }}>{definition}</span>
        </Row>

        <div style={{ display: 'grid', gap: 'var(--sg-s-2)', fontSize: 13 }}>
          <strong>{tr.fleet.groupSummary.reachability}</strong>
          {r.probed ? (
            <>
              <div>
                {tr.fleet.groupSummary.online(r.online, r.total)}
                {r.notProbed ? tr.fleet.groupSummary.notProbed(r.notProbed) : ''}
                {diagnosis.measuredAt !== null ? (
                  <span style={muted}>
                    {' · '}
                    <Ago at={diagnosis.measuredAt} prefix={tr.fleet.groupSummary.probedPrefix} />
                  </span>
                ) : null}
              </div>
              {r.offline.length ? <div>{tr.fleet.groupSummary.offline(r.offline.join(', '))}</div> : null}
            </>
          ) : (
            <div style={muted}>{diagnosis.loading ? tr.fleet.groupSummary.probing : tr.fleet.groupSummary.notProbedRun}</div>
          )}
        </div>

        {group.members.length ? (
          <div style={{ display: 'grid', gap: 'var(--sg-s-2)', fontSize: 13 }}>
            <strong>{tr.fleet.groupSummary.sshAudit}</strong>
            {audit.error !== null ? (
              <ErrorNotice title={tr.fleet.groupSummary.sshAuditFailed} message={audit.error} onRetry={() => void run(group.members, true)} retrying={audit.running} />
            ) : summary ? (
              <div>
                {tr.fleet.groupSummary.summary(summary.audited, summary.nodes, summary.avg_score, summary.failing_nodes.length)}
                {failing.length ? `: ${failing.join(', ')}` : ''}
              </div>
            ) : (
              <div style={muted}>{audit.running ? tr.fleet.groupSummary.auditing : tr.fleet.groupSummary.notAudited}</div>
            )}
          </div>
        ) : null}
      </Stack>
    </Panel>
  );
}
