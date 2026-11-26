import { useState } from 'react';
import { Button, StatusChip } from '@server-guardian/ui';
import { AuthRequiredError, fetchReviewList, startReview } from '../../api';
import { Caption } from '../../components/Card';
import { Ago } from '../../components/Ago';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { useHubData } from '../../hooks/useHubData';
import { jobStatusTone, parseTime } from '../../lib/debate';
import { errorText } from '../../lib/format';
import { reviewParams } from '../../lib/review';
import { hrefFor } from '../../router';
import { useProjectKind } from '../../state/projectStore';
import type { ReviewListItem } from '../../types';
import { tr } from '../../i18n';

/** While at least one attached review runs, the list is polled this often (paused while the page is hidden). */
const ACTION_REVIEW_POLL_MS = 5000;

/** One attached review: status chip, findings by severity, and the link into the Quick review tab. */
function ActionReviewRow({ item }: { item: ReviewListItem }) {
  const created = item.created ? parseTime(item.created) : null;
  return (
    <Row>
      <StatusChip tone={jobStatusTone(item.status)} title={item.status}>
        {tr.agents.jobStatus[item.status] ?? item.status}
      </StatusChip>
      {item.state && item.state !== 'done' ? <StatusChip tone="warn">{tr.review.stateLabel[item.state] ?? item.state}</StatusChip> : null}
      <a
        href={hrefFor('debates', reviewParams({ id: item.id }))}
        aria-label={tr.review.openRun(item.id)}
        title={item.id}
        style={{ ...mono, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 13 }}
      >
        {item.id}
      </a>
      {item.counts && item.state === 'done' ? (
        <span style={{ ...muted, fontSize: 12, whiteSpace: 'nowrap' }}>{tr.review.counts(item.counts.high, item.counts.medium, item.counts.low)}</span>
      ) : null}
      {item.started_by ? (
        <span style={{ ...mono, ...muted, fontSize: 12 }} title={item.started_by}>
          {item.started_by}
        </span>
      ) : null}
      {created !== null ? (
        <span style={{ ...muted, fontSize: 12, whiteSpace: 'nowrap' }}>
          <Ago at={created} />
        </span>
      ) : null}
    </Row>
  );
}

function ActionReviewsBody({ projectId, actionId, proposed }: { projectId: string; actionId: number; proposed: boolean }) {
  const R = tr.review;
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  const [anyRunning, setAnyRunning] = useState(false);
  const reviews = useHubData(
    () =>
      fetchReviewList(undefined, actionId).then((runs) => {
        setAnyRunning(runs.some((r) => r.status === 'running'));
        return runs;
      }),
    { pollMs: anyRunning ? ACTION_REVIEW_POLL_MS : undefined, pauseWhenHidden: true },
  );

  // Advisory only (V2b): the review reads the action's edits and decides nothing, so it never locks the buttons.
  const onStart = async () => {
    if (starting) return;
    setStarting(true);
    setError('');
    try {
      await startReview({ project: projectId, action: actionId });
      reviews.reload();
    } catch (err) {
      if (!(err instanceof AuthRequiredError)) setError(errorText(err));
    } finally {
      setStarting(false);
    }
  };

  return (
    <Stack gap="var(--sg-s-3)">
      <Caption>{R.actionReviewsTitle}</Caption>
      {proposed ? (
        <div>
          <Button variant="outline" loading={starting} disabled={starting} onClick={() => void onStart()}>
            {starting ? R.actionReviewStarting : R.actionReview}
          </Button>
        </div>
      ) : null}
      {error ? <Notice tone="crit">{error}</Notice> : null}
      {reviews.error ? (
        <div style={{ ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>{R.actionReviewsError(reviews.error)}</div>
      ) : reviews.data === null ? null : reviews.data.length === 0 ? (
        <div style={{ ...muted, fontSize: 12 }}>{R.actionNoReviews}</div>
      ) : (
        <Stack gap="var(--sg-s-2)" aria-label={R.actionReviewsTitle}>
          {reviews.data.map((r) => (
            <ActionReviewRow key={r.id} item={r} />
          ))}
        </Stack>
      )}
    </Stack>
  );
}

/**
 * The quick reviews attached to one `jev_mcp:apply_patch` action (V2e of docs/design/verification-layer.md), shown
 * in the approval card: the button starts one on the action's edits, the rows follow it into the Quick review tab.
 * Only a code project can be reviewed this way (the CLI re-checks and refuses the rest), and a review never
 * touches the approve / reject flow.
 */
export function ActionReviews({ projectId, actionId, proposed }: { projectId: string | undefined; actionId: number; proposed: boolean }) {
  const kind = useProjectKind(projectId);
  // kind is undefined for a code project (the field is absent there), an unknown id or no store mounted (tests).
  if (projectId === undefined || kind === 'ops' || kind === 'design') return null;
  return <ActionReviewsBody projectId={projectId} actionId={actionId} proposed={proposed} />;
}
