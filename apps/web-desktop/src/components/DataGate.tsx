import type { ReactNode } from 'react';
import { Button, StatusDot } from '@server-guardian/ui';
import { tr } from '../i18n';
import type { HubDataState } from '../hooks/useHubData';

/** Failure notice with the hub's message and a retry. */
export function ErrorNotice({
  title,
  message,
  onRetry,
  retrying = false,
}: {
  title: string;
  message: string;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  return (
    <div
      role="alert"
      style={{
        display: 'grid',
        gap: 'var(--sg-s-3)',
        padding: 'var(--sg-s-6)',
        border: '1px solid var(--sg-crit-border)',
        background: 'var(--sg-crit-bg)',
        borderRadius: 'var(--sg-r-card)',
        color: 'var(--sg-text)',
      }}
    >
      <strong style={{ color: 'var(--sg-crit)' }}>{title}</strong>
      <span style={{ fontFamily: 'var(--sg-font-mono)', fontSize: 13, overflowWrap: 'anywhere' }}>{message}</span>
      <span style={{ color: 'var(--sg-text-secondary)', fontSize: 13 }}>
        {tr.shell.dataGate.checkHub}
      </span>
      {onRetry ? (
        <div>
          <Button size="sm" variant="outline" loading={retrying} onClick={onRetry}>
            {tr.shell.dataGate.retry}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

export function LoadingNote({ what }: { what: string }) {
  return (
    <div
      role="status"
      style={{ display: 'flex', alignItems: 'center', gap: 'var(--sg-s-3)', color: 'var(--sg-text-secondary)' }}
    >
      <StatusDot tone="info" pulse label={tr.shell.dataGate.loadingDot} />
      <span>{tr.shell.dataGate.loading(what)}</span>
    </div>
  );
}

/**
 * Renders loading, error (with retry) or the data. Data is cleared on failure by the hooks, so the
 * children never see stale values.
 */
export function DataGate<T>({
  state,
  what,
  children,
}: {
  state: HubDataState<T>;
  /** Human name of the data, used in the loading and error text. */
  what: string;
  children: (data: T) => ReactNode;
}) {
  if (state.error !== null) {
    return <ErrorNotice title={tr.shell.dataGate.couldNotLoad(what)} message={state.error} onRetry={state.reload} retrying={state.loading} />;
  }
  if (state.data === null) return <LoadingNote what={what} />;
  return <>{children(state.data)}</>;
}
