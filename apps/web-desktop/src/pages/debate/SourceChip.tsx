import { StatusChip } from '@server-guardian/ui';
import { tr } from '../../i18n';

/** builtin (read-only, from the repository) or custom (saved on the hub). */
export function SourceChip({ source }: { source: string | undefined }) {
  return <StatusChip tone={source === 'custom' ? 'info' : 'idle'}title={source === 'custom' ? 'custom' : 'builtin'}>
      {source === 'custom' ? tr.debate.source.custom : tr.debate.source.builtin}
    </StatusChip>;
}
