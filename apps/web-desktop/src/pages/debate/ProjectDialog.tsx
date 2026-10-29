import { useState } from 'react';
import { Button, TextField } from '@server-guardian/ui';
import { AuthRequiredError, deleteProject, saveProject } from '../../api';
import { ConfirmModal } from '../../components/ConfirmModal';
import { Modal } from '../../components/Modal';
import { Notice } from '../../components/Notice';
import { TextAreaField } from '../../components/TextAreaField';
import { SelectField } from '../../components/SelectField';
import { Stack } from '../../components/layout';
import { errorText } from '../../lib/format';
import type { ProjectData, ProjectEntry, ProjectTestCommand, SaveResult } from '../../types';
import { OpsProjectFields, opsDataOf, opsFormOf } from './OpsProjectFields';
import { tr } from '../../i18n';

export const NEW_PROJECT: ProjectData = { id: '', name: '', repo: 'https://', branch: 'main' };

/** "cargo test: --release, -p=" lines <-> test command objects (a trailing = marks a flag that takes a value). */
export function formatTestCommands(commands: ProjectTestCommand[] | undefined): string {
  return (commands ?? [])
    .map((c) => {
      const flags = Object.entries(c.flags ?? {}).map(([f, v]) => (v ? `${f}=` : f));
      return flags.length ? `${c.prefix}: ${flags.join(', ')}` : c.prefix;
    })
    .join('\n');
}

export function parseTestCommands(text: string): ProjectTestCommand[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [prefix, flagText = ''] = line.split(':', 2);
      const flags: Record<string, boolean> = {};
      for (const raw of flagText.split(',').map((f) => f.trim()).filter(Boolean)) {
        flags[raw.replace(/=$/, '')] = raw.endsWith('=');
      }
      return Object.keys(flags).length ? { prefix: prefix.trim(), flags } : { prefix: prefix.trim() };
    });
}

/** Create / edit / delete a project. The hub validates everything again (URL, paths, test commands). */
export function ProjectDialog({
  entry,
  isNew,
  onClose,
  onSaved,
  onDeleted,
}: {
  entry: ProjectEntry | null;
  isNew: boolean;
  onClose: () => void;
  onSaved: (result: SaveResult) => void;
  onDeleted: (id: string) => void;
}) {
  const data = entry?.data ?? NEW_PROJECT;
  const isBuiltin = !isNew && entry?.source === 'builtin';
  const [id, setId] = useState(data.id);
  const [name, setName] = useState(data.name ?? '');
  const [description, setDescription] = useState(data.description ?? '');
  const [repo, setRepo] = useState(data.repo ?? '');
  const [branch, setBranch] = useState(data.branch ?? 'main');
  const [workspace, setWorkspace] = useState(data.workspace ?? '');
  const [defaultTest, setDefaultTest] = useState(data.tests?.default ?? '');
  const [testCommands, setTestCommands] = useState(formatTestCommands(data.tests?.commands));
  const [contextPaths, setContextPaths] = useState((data.context?.paths ?? []).join('\n'));
  // The kind of a saved project is fixed: an ops project has no repository to add, a code project no targets.
  const [kind, setKind] = useState<'code' | 'ops'>(data.kind === 'ops' ? 'ops' : 'code');
  const [opsForm, setOpsForm] = useState(() => opsFormOf(data));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const save = async () => {
    const commands = parseTestCommands(testCommands);
    const paths = contextPaths.split('\n').map((p) => p.trim()).filter(Boolean);
    const project: ProjectData = kind === 'ops' ? {
      id: id.trim(),
      name: name.trim() || undefined,
      description: description.trim() || undefined,
      kind: 'ops',
      ...opsDataOf(opsForm),
    } : {
      id: id.trim(),
      name: name.trim() || undefined,
      description: description.trim() || undefined,
      repo: repo.trim() || undefined,
      branch: branch.trim() || undefined,
      workspace: workspace.trim() || undefined,
      tests: defaultTest.trim() || commands.length ? { default: defaultTest.trim() || undefined, commands } : undefined,
      context: paths.length ? { paths } : undefined,
    };
    setBusy(true);
    setError(null);
    try {
      onSaved(await saveProject(project));
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setError(errorText(err));
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    setDeleteError(null);
    try {
      await deleteProject(data.id);
      onDeleted(data.id);
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setDeleteError(errorText(err));
      setBusy(false);
    }
  };

  return (
    <>
      <Modal title={isNew ? tr.debate.project.newTitle : tr.debate.project.editTitle(data.name || data.id)} width={640} onClose={busy ? undefined : onClose}>
        <Stack gap="var(--sg-s-6)">
          <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13, lineHeight: 1.5 }}>
            {isBuiltin
              ? tr.debate.project.subBuiltin
              : kind === 'ops' ? tr.debate.project.subOps : tr.debate.project.subCustom}
          </div>
          {isNew ? (
            <SelectField
              label={tr.debate.project.kind}
              value={kind}
              onChange={(e) => setKind(e.target.value === 'ops' ? 'ops' : 'code')}
              options={[
                { value: 'code', label: tr.debate.project.kindCode },
                { value: 'ops', label: tr.debate.project.kindOps },
              ]}
            />
          ) : null}
          <TextField label={tr.debate.project.id} mono value={id} readOnly={!isNew} onChange={(e) => setId(e.target.value)} />
          <TextField label={tr.debate.persona.name} value={name} onChange={(e) => setName(e.target.value)} />
          <TextField label={tr.debate.project.description} value={description} onChange={(e) => setDescription(e.target.value)} />
          {kind === 'ops' ? <OpsProjectFields form={opsForm} onChange={setOpsForm} /> : null}
          {kind === 'ops' ? null : <>
          <TextField
            label={tr.debate.project.repo}
            mono
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
          />
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sg-s-6)' }}>
            <TextField label={tr.debate.result.branch} mono value={branch} onChange={(e) => setBranch(e.target.value)} />
            <TextField
              label={tr.debate.project.workspace}
              mono
              placeholder={`~/guardian-workspaces/${id || '<id>'}`}
              value={workspace}
              onChange={(e) => setWorkspace(e.target.value)}
            />
          </div>
          <TextAreaField
            label={tr.debate.project.testCommands}
            mono
            rows={3}
            placeholder={'cargo test: --release, -p=\nnpx vitest run'}
            value={testCommands}
            onChange={(e) => setTestCommands(e.target.value)}
          />
          <TextField
            label={tr.debate.project.defaultTest}
            mono
            placeholder="cargo test"
            value={defaultTest}
            onChange={(e) => setDefaultTest(e.target.value)}
          />
          <TextAreaField
            label={tr.debate.project.contextPaths}
            mono
            rows={3}
            placeholder={'src/**/*.rs\nREADME.md'}
            value={contextPaths}
            onChange={(e) => setContextPaths(e.target.value)}
          />
          </>}
          {error ? <Notice tone="crit">{error}</Notice> : null}
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--sg-s-4)', flexWrap: 'wrap' }}>
            <div>
              {!isNew && entry?.source === 'custom' ? (
                <Button variant="danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
                  {tr.debate.project.deleteCustom}
                </Button>
              ) : null}
            </div>
            <div style={{ display: 'flex', gap: 'var(--sg-s-4)' }}>
              <Button variant="ghost" disabled={busy} onClick={onClose}>
                {tr.common.cancel}
              </Button>
              <Button variant="primary" loading={busy} onClick={() => void save()}>
                {isBuiltin ? tr.debate.persona.saveOverride : tr.common.save}
              </Button>
            </div>
          </div>
        </Stack>
      </Modal>
      {confirmDelete ? (
        <ConfirmModal
          title={tr.debate.project.deleteCustom}
          message={tr.debate.project.deleteConfirm(data.id)}
          confirmLabel={tr.debate.project.deleteAction}
          busy={busy}
          error={deleteError}
          onConfirm={() => void remove()}
          onCancel={() => {
            setConfirmDelete(false);
            setDeleteError(null);
          }}
        />
      ) : null}
    </>
  );
}
