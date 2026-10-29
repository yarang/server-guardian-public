import { useState } from 'react';
import { Button, TextField } from '@server-guardian/ui';
import { tr } from '../../i18n';
import { AuthRequiredError, saveWikiPage } from '../../api';
import { Modal } from '../../components/Modal';
import { Notice } from '../../components/Notice';
import { TextAreaField } from '../../components/TextAreaField';
import { Stack } from '../../components/layout';
import { errorText } from '../../lib/format';
import { WIKI_PAGE_ID, WIKI_TEXT_LIMIT, formatScope, parseScope, textBytes } from '../../lib/wiki';
import type { WikiSaveResult } from '../../types';

export interface WikiDraft {
  id: string;
  title: string;
  scope: string[];
  body: string;
}

/**
 * Create or edit a page by hand. Saving makes you the page's author and records the files the text cites as its
 * basis, so the page turns stale when they change. The hub validates everything again.
 */
export function WikiEditDialog({
  project,
  draft,
  isNew,
  onClose,
  onSaved,
}: {
  project: string;
  draft: WikiDraft;
  isNew: boolean;
  onClose: () => void;
  onSaved: (id: string, result: WikiSaveResult) => void;
}) {
  const [id, setId] = useState(draft.id);
  const [title, setTitle] = useState(draft.title);
  const [scope, setScope] = useState(formatScope(draft.scope));
  const [body, setBody] = useState(draft.body);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const bytes = textBytes(body);

  const save = async () => {
    const pageId = id.trim();
    if (!WIKI_PAGE_ID.test(pageId)) return setError(tr.wiki.edit.pageIdInvalid);
    if (!title.trim()) return setError(tr.wiki.edit.titleRequired);
    if (title.includes('\n')) return setError(tr.wiki.edit.titleOneLine);
    if (!body.trim()) return setError(tr.wiki.edit.bodyEmpty);
    if (bytes > WIKI_TEXT_LIMIT) return setError(tr.wiki.edit.tooLarge(Math.round(bytes / 1024), WIKI_TEXT_LIMIT / 1024));
    setBusy(true);
    setError(null);
    try {
      onSaved(pageId, await saveWikiPage(project, pageId, { title: title.trim(), scope: parseScope(scope), body }));
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setError(errorText(err));
      setBusy(false);
    }
  };

  return (
    <Modal title={isNew ? tr.wiki.edit.newTitle : tr.wiki.edit.editTitle(draft.id)} width={760} onClose={busy ? undefined : onClose}>
      <Stack gap="var(--sg-s-6)">
        <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13, lineHeight: 1.5 }}>
          {tr.wiki.edit.intro1}
          <code>path:LINE</code>
          {tr.wiki.edit.intro2}
          <code>path#Symbol</code>
          {tr.wiki.edit.intro3}
        </div>
        <TextField label={tr.wiki.edit.pageId} mono value={id} readOnly={!isNew} onChange={(e) => setId(e.target.value)} />
        <TextField label={tr.wiki.edit.title} value={title} onChange={(e) => setTitle(e.target.value)} />
        <TextAreaField
          label={tr.wiki.edit.scope}
          mono
          rows={3}
          placeholder={'cli/llm-agent/'}
          value={scope}
          onChange={(e) => setScope(e.target.value)}
        />
        <TextAreaField
          label={tr.wiki.edit.body}
          mono
          rows={18}
          hint={tr.wiki.edit.size(Math.round(bytes / 1024), WIKI_TEXT_LIMIT / 1024)}
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
        {error ? <Notice tone="crit">{error}</Notice> : null}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--sg-s-4)' }}>
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            {tr.common.cancel}
          </Button>
          <Button variant="primary" loading={busy} onClick={() => void save()}>
            {tr.common.save}
          </Button>
        </div>
      </Stack>
    </Modal>
  );
}
