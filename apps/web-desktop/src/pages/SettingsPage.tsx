import { useCallback, useState } from 'react';
import { Button, Panel, SegmentedControl } from '@server-guardian/ui';
import { fetchWorkers } from '../api';
import { LANGUAGES, isLanguage, language, setLanguage, tr } from '../i18n';
import type { Language } from '../i18n';
import { Notice } from '../components/Notice';
import { SelectField } from '../components/SelectField';
import { Row, Stack, mono, muted } from '../components/layout';
import { useHubData } from '../hooks/useHubData';
import { fleetHref, navigate } from '../router';
import { useProjectStore } from '../state/projectStore';
import { ProjectsSection } from './debate/ProjectsSection';
import { WorkersSection } from './debate/WorkersSection';
import { FlowsTab } from './flows/FlowsTab';
import { PersonasTab } from './personas/PersonasTab';

/** Top-level groups of the Settings page (`#/settings/<group>`). */
export const SETTINGS_GROUPS = [
  { value: 'endpoint', label: tr.shell.settingsPage.sections.endpoint },
  { value: 'flowConfig', label: tr.shell.settingsPage.sections.flowConfig },
  { value: 'workers', label: tr.shell.settingsPage.sections.workers },
  { value: 'display', label: tr.shell.settingsPage.sections.display },
] as const;

export type SettingsGroup = (typeof SETTINGS_GROUPS)[number]['value'];

/** The sections the flow setup group switches between, under a control of its own. */
export const FLOW_CONFIG_SECTIONS = [
  { value: 'projects', label: tr.shell.settingsPage.sections.projects },
  { value: 'flows', label: tr.shell.settingsPage.sections.flows },
  { value: 'personas', label: tr.shell.settingsPage.sections.personas },
] as const;

export type FlowConfigSection = (typeof FLOW_CONFIG_SECTIONS)[number]['value'];

/** Every section `#/settings/<value>` can show: a group of its own or one of the flow setup sections. */
export type SettingsSection = 'endpoint' | 'workers' | 'display' | FlowConfigSection;

/**
 * `#/settings/:section` param as a section. `flowConfig` means the flow setup group's first section
 * (projects); missing or unknown values show the hub connection.
 */
export function parseSection(param: string | undefined): SettingsSection {
  if (param === 'flowConfig') return 'projects';
  // The team editor was removed (docs/design/ui-verification-review.md, X1): flows replace teams.
  if (param === 'teams') return 'flows';
  const flow = FLOW_CONFIG_SECTIONS.find((s) => s.value === param);
  if (flow) return flow.value;
  return param === 'workers' || param === 'display' ? param : 'endpoint';
}

/** The group a section renders under: the flow setup sections share `flowConfig`. */
function groupOf(section: SettingsSection): SettingsGroup {
  return SETTINGS_GROUPS.find((g) => g.value === section)?.value ?? 'flowConfig';
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

function WorkersPane() {
  const workers = useHubData(fetchWorkers);
  return <WorkersSection state={workers} />;
}

/**
 * Settings: a section switcher (`#/settings/<section>`) over the configuration the dashboard
 * edits. Four groups; the flow setup group holds a second switcher for projects, flows and
 * characters (personas). Each section fetches its own data when shown; the existing dialogs are reused as-is.
 */
export function SettingsPage({
  agentUrl,
  onEditEndpoint,
  section,
  params = [],
}: {
  agentUrl: string;
  onEditEndpoint: () => void;
  /** `#/settings/:section` param. */
  section?: string;
  /** Path segments after the section (`#/settings/personas/<id>`: the character page). */
  params?: string[];
}) {
  const current = parseSection(section);
  const group = groupOf(current);
  return (
    <Stack>
      <SegmentedControl
        aria-label={tr.shell.settingsPage.sectionsAria}
        options={[...SETTINGS_GROUPS]}
        value={group}
        onChange={(v) => {
          if (v !== group) navigate('settings', [v === 'flowConfig' ? 'projects' : v]);
        }}
      />
      {group === 'flowConfig' ? (
        <SegmentedControl
          aria-label={tr.shell.settingsPage.flowSectionsAria}
          options={[...FLOW_CONFIG_SECTIONS]}
          value={current}
          onChange={(v) => navigate('settings', [v])}
        />
      ) : null}
      {current === 'endpoint' ? <EndpointSection agentUrl={agentUrl} onEditEndpoint={onEditEndpoint} /> : null}
      {current === 'projects' ? <ProjectsPane /> : null}
      {current === 'flows' ? <FlowsTab /> : null}
      {current === 'personas' ? <PersonasTab params={params} /> : null}
      {current === 'workers' ? <WorkersPane /> : null}
      {current === 'display' ? <LanguageSection /> : null}
      <div style={{ ...muted, fontSize: 13 }}>
        {tr.shell.settingsPage.groupsNotePrefix}
        <a href={fleetHref('groups')}>{tr.shell.settingsPage.groupsNoteLink}</a>
        {tr.shell.settingsPage.groupsNoteSuffix}
      </div>
    </Stack>
  );
}
