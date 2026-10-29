import { useMemo, useState } from 'react';
import { Button, Panel, StatusChip, TextField } from '@server-guardian/ui';
import { Card } from '../../components/Card';
import { DataGate } from '../../components/DataGate';
import { SelectField } from '../../components/SelectField';
import { Grid, Row, Stack, mono, muted } from '../../components/layout';
import type { HubDataState } from '../../hooks/useHubData';
import { roleTone } from '../../lib/debate';
import { NEW_PERSONA_PARAM, matchesPersona, personasHref, roleOptions } from '../../lib/personas';
import type { PersonaConfig, PersonaList as PersonaListData } from '../../types';
import { SourceChip } from '../debate/SourceChip';
import { tr } from '../../i18n';

const P = tr.personas;

/** All personas as cards: search over name / id / domain, a role filter, builtin / custom chip, edit link. */
export function PersonaList({ state }: { state: HubDataState<PersonaListData> }) {
  const [query, setQuery] = useState('');
  const [role, setRole] = useState('');
  const all: PersonaConfig[] = state.data?.personas ?? [];
  const roles = useMemo(() => roleOptions(all).filter((r) => all.some((p) => p.role === r)), [all]);
  const shown = all.filter((p) => (role === '' || p.role === role) && matchesPersona(p, query));

  return (
    <Panel
      title={state.data ? P.listTitle(shown.length, all.length) : P.title}
      actions={
        <Row>
          <Button size="sm" variant="outline" loading={state.loading} onClick={state.reload}>
            {tr.common.refresh}
          </Button>
          <Button size="sm" variant="primary" onClick={() => (window.location.hash = personasHref(NEW_PERSONA_PARAM))}>
            {P.newButton}
          </Button>
        </Row>
      }
    >
      <Stack gap="var(--sg-s-6)">
        <div style={{ ...muted, fontSize: 13, lineHeight: 1.5 }}>{P.intro}</div>
        <DataGate state={state} what={P.what}>
          {() => (
            <Stack gap="var(--sg-s-6)">
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-6)', alignItems: 'flex-end' }}>
                <div style={{ flex: '2 1 240px', minWidth: 0 }}>
                  <TextField label={P.search} type="search" value={query} onChange={(e) => setQuery(e.target.value)} />
                </div>
                <div style={{ flex: '1 1 220px', minWidth: 0 }}>
                  <SelectField
                    label={P.roleFilter}
                    value={role}
                    options={[{ value: '', label: P.allRoles }, ...roles.map((r) => ({ value: r, label: r }))]}
                    onChange={(e) => setRole(e.target.value)}
                  />
                </div>
              </div>
              {all.length === 0 ? (
                <div style={{ ...muted, textAlign: 'center' }}>{P.empty}</div>
              ) : shown.length === 0 ? (
                <div style={{ ...muted, textAlign: 'center' }}>{P.noMatch}</div>
              ) : (
                <Grid min={260} gap="var(--sg-s-6)">
                  {shown.map((p) => (
                    <Card key={p.id}>
                      <Row style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
                        <strong style={{ overflowWrap: 'anywhere' }}>{p.name}</strong>
                        <SourceChip source={p.source} />
                      </Row>
                      <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13, overflowWrap: 'anywhere' }}>{p.domain}</div>
                      <Row gap="var(--sg-s-3)">
                        <StatusChip tone={roleTone(p.role || '')}>{p.role}</StatusChip>
                        <span style={{ ...mono, ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>{p.id}</span>
                      </Row>
                      <div>
                        <Button size="sm" variant="outline" aria-label={P.editAria(p.name)} onClick={() => (window.location.hash = personasHref(p.id))}>
                          {P.edit}
                        </Button>
                      </div>
                    </Card>
                  ))}
                </Grid>
              )}
            </Stack>
          )}
        </DataGate>
      </Stack>
    </Panel>
  );
}
