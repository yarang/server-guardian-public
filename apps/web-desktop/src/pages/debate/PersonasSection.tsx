import { useState } from 'react';
import { Button, Panel, StatusChip } from '@server-guardian/ui';
import { Card } from '../../components/Card';
import { DataGate } from '../../components/DataGate';
import { Notice, NoticeList } from '../../components/Notice';
import { Grid, Row, Stack, mono, muted } from '../../components/layout';
import type { HubDataState } from '../../hooks/useHubData';
import { roleTone } from '../../lib/debate';
import { personasHref } from '../../lib/personas';
import type { PersonaConfig, PersonaList } from '../../types';
import { NEW_PERSONA, PersonaDialog } from './PersonaDialog';
import { SourceChip } from './SourceChip';
import { tr } from '../../i18n';

interface Banner {
  text: string;
  warnings: string[];
}

/** Persona repository: cards with builtin/custom badge, editor dialog, save / delete notices. */
export function PersonasSection({ state, onReload, onChanged }: { state: HubDataState<PersonaList>; onReload: () => void; onChanged: () => void }) {
  const [editing, setEditing] = useState<{ persona: PersonaConfig; isNew: boolean } | null>(null);
  const [banner, setBanner] = useState<Banner | null>(null);
  const count = state.data?.personas?.length;

  return (
    <Panel
      title={count !== undefined ? tr.debate.persona.sectionCount(count) : tr.debate.persona.section}
      actions={
        <Row>
          {/* The full editor (all fields, templates, "used by") lives on its own page; this dialog stays for quick edits. */}
          <a href={personasHref()}>{tr.personas.openPage}</a>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setBanner(null);
              onReload();
            }}
          >
            {tr.common.refresh}
          </Button>
          <Button size="sm" variant="secondary" onClick={() => setEditing({ persona: NEW_PERSONA, isNew: true })}>
            {tr.debate.persona.newButton}
          </Button>
        </Row>
      }
    >
      <Stack gap="var(--sg-s-6)">
        {banner ? (
          <Stack gap="var(--sg-s-3)">
            <Notice tone="ok">{banner.text}</Notice>
            <NoticeList tone="warn" items={banner.warnings} />
          </Stack>
        ) : null}
        <DataGate state={state} what={tr.debate.persona.what}>
          {(data) => (
            <Grid min={260} gap="var(--sg-s-6)">
              {(data.personas || []).map((p) => (
                <Card key={p.id}>
                  <Row style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
                    <strong style={{ overflowWrap: 'anywhere' }}>{p.name}</strong>
                    <SourceChip source={p.source} />
                  </Row>
                  <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>{p.domain}</div>
                  <Row gap="var(--sg-s-3)">
                    <StatusChip tone={roleTone(p.role || '')}>{p.role}</StatusChip>
                    <span style={{ ...mono, ...muted, fontSize: 12 }}>{p.id}</span>
                  </Row>
                  <div>
                    <Button size="sm" variant="outline" aria-label={tr.debate.persona.editAria(p.name)} onClick={() => setEditing({ persona: p, isNew: false })}>
                      {tr.common.edit}
                    </Button>
                  </div>
                </Card>
              ))}
            </Grid>
          )}
        </DataGate>
      </Stack>
      {editing ? (
        <PersonaDialog
          persona={editing.persona}
          isNew={editing.isNew}
          onClose={() => setEditing(null)}
          onSaved={(res) => {
            setBanner({ text: tr.debate.persona.saved(res.id), warnings: res.warnings });
            setEditing(null);
            onChanged();
          }}
          onDeleted={(id) => {
            setBanner({ text: tr.debate.persona.deleted(id), warnings: [] });
            setEditing(null);
            onChanged();
          }}
        />
      ) : null}
    </Panel>
  );
}
