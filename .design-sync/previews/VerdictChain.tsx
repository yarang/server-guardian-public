import { VerdictChain } from '@server-guardian/ui';

export const ApprovedChain = () => (
  <div style={{ padding: 16, width: 460 }}>
    <VerdictChain
      steps={[
        { label: 'LLM judge', result: 'APPROVED', tone: 'ok', detail: 'PasswordAuthentication 비활성화 승인 (console fallback 확인)' },
        { label: 'jev-mcp', result: 'ADOPT 0.86', tone: 'ok', detail: 'adopt score 0.86 ≥ threshold 0.75' },
        { label: 'Gate', result: 'APPROVED', tone: 'ok', detail: 'target site-b-ec1 · action #57 queued' },
      ]}
    />
  </div>
);

export const DisputedPending = () => (
  <div style={{ padding: 16, width: 460 }}>
    <VerdictChain
      steps={[
        { label: 'Expert', result: 'PROPOSED', tone: 'info', detail: 'site-b-ec1 디스크 91% — journal vacuum 제안' },
        { label: 'Critic panel', result: 'DISPUTED', tone: 'warn', detail: '2 of 3 critics flagged log retention policy' },
        { label: 'LLM judge', result: 'APPROVED', tone: 'ok', detail: 'Approved with --vacuum-time=14d patch' },
        { label: 'jev-mcp gate', result: 'PENDING_JEV', tone: 'warn' },
      ]}
    />
  </div>
);

export const HeldRejected = () => (
  <div style={{ padding: 16, width: 460 }}>
    <VerdictChain
      steps={[
        { label: 'LLM judge', result: 'REJECTED', tone: 'crit', detail: 'Reboot of hub site-a-arm1 during active debates' },
        { label: 'Gate', result: 'HELD', tone: 'idle', detail: 'Held for operator review' },
      ]}
    />
  </div>
);
