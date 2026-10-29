import { useState } from 'react';
import { fetchPersonas, fetchTeams } from '../../api';
import { DataGate } from '../../components/DataGate';
import { Notice, NoticeList } from '../../components/Notice';
import { Stack } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { parsePersonaRoute, personasHref } from '../../lib/personas';
import { hrefFor } from '../../router';
import { PersonaEditor } from './PersonaEditor';
import { PersonaList } from './PersonaList';
import { tr } from '../../i18n';

interface Banner {
  text: string;
  warnings: string[];
}

/**
 * The persona page: `#/debates/personas` (list), `#/debates/personas/new` (blank form) and
 * `#/debates/personas/<id>` (edit). `params` are the path segments after `personas`. The team list is
 * fetched for "used by" and the tool names; if it fails the editor still works without them.
 */
export function PersonasTab({ params }: { params: string[] }) {
  const route = parsePersonaRoute(params);
  const personas = useHubData(fetchPersonas);
  const teams = useHubData(fetchTeams);
  const [banner, setBanner] = useState<Banner | null>(null);
  const all = personas.data?.personas ?? [];

  const backToList = (b: Banner | null) => {
    setBanner(b);
    personas.reload();
    teams.reload();
    window.location.hash = personasHref();
  };

  return (
    <Stack>
      <div>
        <a href={hrefFor('debates')}>{tr.personas.backToDebates}</a>
      </div>
      {banner ? (
        <Stack gap="var(--sg-s-3)">
          <Notice tone="ok">{banner.text}</Notice>
          <NoticeList tone="warn" items={banner.warnings} />
        </Stack>
      ) : null}
      {route.view === 'list' ? (
        <PersonaList state={personas} />
      ) : (
        <DataGate state={personas} what={tr.personas.what}>
          {(data) => {
            const found = route.view === 'edit' ? data.personas.find((p) => p.id === route.id) : undefined;
            if (route.view === 'edit' && !found) return <Notice tone="warn">{tr.personas.notFound(route.id)}</Notice>;
            return (
              <PersonaEditor
                key={route.view === 'edit' ? route.id : 'new'}
                persona={found ?? null}
                personas={all}
                teams={teams.data}
                teamsFailed={teams.error !== null}
                onSaved={(res) => backToList({ text: tr.personas.saved(res.id), warnings: res.warnings })}
                onDeleted={(id) => backToList({ text: tr.personas.deleted(id), warnings: [] })}
                onCancel={() => backToList(null)}
              />
            );
          }}
        </DataGate>
      )}
    </Stack>
  );
}
