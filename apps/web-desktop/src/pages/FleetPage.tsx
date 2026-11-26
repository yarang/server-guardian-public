import { tr } from '../i18n';
import { SegmentedControl } from '@server-guardian/ui';
import { fetchCommandSets } from '../api';
import { useHubData } from '../hooks/useHubData';
import { useHubStore } from '../state/hubStore';
import { NodeGroupsSection } from './fleet/NodeGroupsSection';
import { CommandSetsSection } from './debate/CommandSetsSection';
import { Stack } from '../components/layout';
import { FLEET_TABS, fleetHref, fleetTab } from '../router';
import type { ParsedRoute } from '../router';
import { NodeDetailPage } from './NodeDetailPage';
import { NodeRegisterPage } from './NodeRegisterPage';
import { NodesPage } from './NodesPage';
import { RunPage } from './RunPage';
import { SshPage } from './SshPage';

/**
 * Fleet: the fleet's nodes in one place, split into tabs that live in the URL (the summary moved to the
 * Overview destination). `#/fleet/nodes` inventory, `#/fleet/nodes/<id>` node page, `#/fleet/nodes/new` registration,
 * `#/fleet/groups` node group definitions (`?edit=<id>` opens one), `#/fleet/ssh` SSH audit, `#/fleet/commands` run
 * commands and history (`?target=` preselects the target), `#/fleet/commandSets` the command set editor.
 * The node page and the registration wizard keep the Nodes tab highlighted.
 */
function GroupsTab({ editId }: { editId?: string }) {
  const { groups, servers } = useHubStore();
  return <NodeGroupsSection state={groups} servers={servers.data ?? []} onChanged={groups.reload} editId={editId} />;
}

function CommandSetsTab() {
  const sets = useHubData(fetchCommandSets);
  return <CommandSetsSection state={sets} onChanged={sets.reload} />;
}

export function FleetPage({ params, query }: { params: ParsedRoute['params']; query: ParsedRoute['query'] }) {
  const tab = fleetTab(params[0]);
  const rest = params.slice(1);
  return (
    <Stack>
      <SegmentedControl
        aria-label={tr.fleet.nodes.fleetSections}
        options={[...FLEET_TABS]}
        value={tab}
        onChange={(v) => {
          window.location.hash = fleetHref(fleetTab(v));
        }}
      />
      {tab === 'nodes' ? (
        // The id "new" is refused by the wizard, so it never shadows a node page.
        rest[0] === 'new' ? <NodeRegisterPage /> : rest[0] ? <NodeDetailPage id={rest[0]} /> : <NodesPage query={query} />
      ) : null}
      {tab === 'groups' ? <GroupsTab editId={query.edit} /> : null}
      {tab === 'ssh' ? <SshPage /> : null}
      {tab === 'commands' ? <RunPage target={query.target} /> : null}
      {tab === 'commandSets' ? <CommandSetsTab /> : null}
    </Stack>
  );
}
