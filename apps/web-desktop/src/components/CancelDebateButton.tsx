import { useState } from 'react';
import { Button } from '@server-guardian/ui';
import { tr } from '../i18n';
import { ConfirmModal } from './ConfirmModal';

/** Cancel button for a running debate; cancelling stops the agents and cannot be undone, so it asks first. */
export function CancelDebateButton({ cancelling, onConfirm, wiki = false }: { cancelling: boolean; onConfirm: () => void; wiki?: boolean }) {
  const [asking, setAsking] = useState(false);
  return (
    <>
      <Button size="sm" variant="danger" loading={cancelling} onClick={() => setAsking(true)}>
        {cancelling ? tr.shell.cancelDebate.cancelling : tr.shell.cancelDebate.cancel}
      </Button>
      {asking ? (
        <ConfirmModal
          title={wiki ? tr.shell.cancelDebate.titleWiki : tr.shell.cancelDebate.titleDebate}
          message={
            wiki
              ? tr.shell.cancelDebate.messageWiki
              : tr.shell.cancelDebate.messageDebate
          }
          confirmLabel={wiki ? tr.shell.cancelDebate.confirmWiki : tr.shell.cancelDebate.confirmDebate}
          cancelLabel={tr.shell.cancelDebate.keep}
          onConfirm={() => {
            setAsking(false);
            onConfirm();
          }}
          onCancel={() => setAsking(false)}
        />
      ) : null}
    </>
  );
}
