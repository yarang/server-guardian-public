import { ActionCard } from '@server-guardian/ui';

export const HighRiskProposed = () => (
  <div style={{ padding: 16, width: 420 }}>
    <ActionCard
      id="57"
      tool="server_remediation"
      risk="HIGH"
      status="PROPOSED"
      title="PasswordAuthentication 비활성화 후 sshd reload"
      meta="target site-b-ec1 · from T-0142"
      command={"sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config\nsshd -t && systemctl reload ssh"}
    />
  </div>
);

export const HeldByJev = () => (
  <div style={{ padding: 16, width: 420 }}>
    <ActionCard
      id="55"
      tool="execute_code_action"
      risk="MEDIUM"
      status="HELD"
      title="SSE 재연결 백오프 패치 적용"
      meta="hub workspace · from T-0139"
      warning="jev DISPUTED · confidence 0.54 — re-evaluate before running"
    />
  </div>
);

export const LowRisk = () => (
  <div style={{ padding: 16, width: 420 }}>
    <ActionCard
      id="58"
      tool="run_tests"
      risk="LOW"
      status="PROPOSED"
      title="Agent 테스트 실행"
      meta="hub workspace · from T-0142"
      command="go test ./apps/agent/..."
    />
  </div>
);

export const Executed = () => (
  <div style={{ padding: 16, width: 420 }}>
    <ActionCard
      id="51"
      tool="run_tests"
      risk="LOW"
      status="EXECUTED"
      title="jev gate 단위 테스트"
      meta="decided_by dashboard@100.64.0.7 · exit 0"
    />
  </div>
);
