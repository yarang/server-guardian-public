import { useCallback, useState } from 'react';
import { Button, Panel, SegmentedControl } from '@server-guardian/ui';
import { fetchCommandSets, fetchPersonas, fetchTeams, fetchWorkers } from '../api';
import { LANGUAGES, isLanguage, language, setLanguage, tr } from '../i18n';
import type { Language } from '../i18n';
import { Notice } from '../components/Notice';
import { SelectField } from '../components/SelectField';
import { Row, Stack, mono, muted } from '../components/layout';
import { useHubData } from '../hooks/useHubData';
import { inDesktopShell } from '../lib/exportFolder';
import { fleetHref, navigate } from '../router';
import { useProjectStore } from '../state/projectStore';
import { ExportFolderSection } from './ExportFolderSection';
import { CommandSetsSection } from './debate/CommandSetsSection';
import { PersonasSection } from './debate/PersonasSection';
import { ProjectsSection } from './debate/ProjectsSection';
import { TeamsSection } from './debate/TeamsSection';
import { WorkersSection } from './debate/WorkersSection';

export const SETTINGS_SECTIONS = [
  { value: 'endpoint', label: tr.shell.settingsPage.sections.endpoint },
  { value: 'projects', label: tr.shell.settingsPage.sections.projects },
  { value: 'personas', label: tr.shell.settingsPage.sections.personas },
  { value: 'teams', label: tr.shell.settingsPage.sections.teams },
  { value: 'commandSets', label: tr.shell.settingsPage.sections.commandSets },
  { value: 'workers', label: tr.shell.settingsPage.sections.workers },
] as const;

export type SettingsSection = (typeof SETTINGS_SECTIONS)[number]['value'];

/** `#/settings/:section` param as a section; missing or unknown values show the hub connection. */
export function parseSection(param: string | undefined): SettingsSection {
  return SETTINGS_SECTIONS.find((s) => s.value === param)?.value ?? 'endpoint';
}

/** The dashboard language (i18n/index.ts): saved per browser, applied by reloading the page. */
function LanguageSection() {
  const [failed, setFailed] = useState(false);
  return (
    <Panel title={tr.shell.language.label}>
      <Stack gap="var(--sg-s-3)">
        <SelectField
          label={tr.shell.language.label}
          value={language}
          options={(Object.keys(LANGUAGES) as Language[]).map((code) => ({ value: code, label: tr.shell.language.names[code] ?? code }))}
          onChange={(e) => {
            if (isLanguage(e.target.value)) setFailed(!setLanguage(e.target.value));
          }}
        />
        <div style={{ ...muted, fontSize: 13 }}>{tr.shell.language.hint}</div>
        {failed ? <Notice tone="warn">{tr.shell.language.saveFailed}</Notice> : null}
      </Stack>
    </Panel>
  );
}

function EndpointSection({ agentUrl, onEditEndpoint }: { agentUrl: string; onEditEndpoint: () => void }) {
  return (
    <Panel title={tr.shell.settingsPage.endpointTitle}>
      <Row>
        <span style={{ ...mono, overflowWrap: 'anywhere' }}>{agentUrl}</span>
        <Button size="sm" variant="outline" onClick={onEditEndpoint}>
          {tr.shell.settingsPage.changeEndpoint}
        </Button>
      </Row>
    </Panel>
  );
}

function ProjectsPane() {
  const { projects } = useProjectStore();
  return <ProjectsSection state={projects} onChanged={projects.reload} />;
}

function PersonasPane() {
  const personas = useHubData(fetchPersonas);
  return <PersonasSection state={personas} onReload={personas.reload} onChanged={personas.reload} />;
}

function TeamsPane() {
  const personas = useHubData(fetchPersonas);
  const teams = useHubData(fetchTeams);
  const reloadPersonas = personas.reload;
  const reloadTeams = teams.reload;
  const reloadAll = useCallback(() => {
    reloadPersonas();
    reloadTeams();
  }, [reloadPersonas, reloadTeams]);
  return <TeamsSection state={teams} personas={personas.data?.personas ?? []} onChanged={reloadAll} />;
}

function CommandSetsPane() {
  const sets = useHubData(fetchCommandSets);
  return <CommandSetsSection state={sets} onChanged={sets.reload} />;
}

function WorkersPane() {
  const workers = useHubData(fetchWorkers);
  return <WorkersSection state={workers} />;
}

/**
 * Settings: a section switcher (`#/settings/<section>`) over the configuration the dashboard
 * edits. Each section fetches its own data when shown; the existing dialogs are reused as-is.
 */
export function SettingsPage({
  agentUrl,
  onEditEndpoint,
  section,
}: {
  agentUrl: string;
  onEditEndpoint: () => void;
  /** `#/settings/:section` param. */
  section?: string;
}) {
  const current = parseSection(section);
  return (
    <Stack>
      <SegmentedControl
        aria-label={tr.shell.settingsPage.sectionsAria}
        options={[...SETTINGS_SECTIONS]}
        value={current}
        onChange={(v) => navigate('settings', [v])}
      />
      {current === 'endpoint' ? <EndpointSection agentUrl={agentUrl} onEditEndpoint={onEditEndpoint} /> : null}
      {current === 'endpoint' ? <LanguageSection /> : null}
      {current === 'endpoint' && inDesktopShell() ? <ExportFolderSection /> : null}
      {current === 'projects' ? <ProjectsPane /> : null}
      {current === 'personas' ? <PersonasPane /> : null}
      {current === 'teams' ? <TeamsPane /> : null}
      {current === 'commandSets' ? <CommandSetsPane /> : null}
      {current === 'workers' ? <WorkersPane /> : null}
      <div style={{ ...muted, fontSize: 13 }}>
        {tr.shell.settingsPage.groupsNotePrefix}
        <a href={fleetHref('groups')}>{tr.shell.settingsPage.groupsNoteLink}</a>
        {tr.shell.settingsPage.groupsNoteSuffix}
      </div>
    </Stack>
  );
}
