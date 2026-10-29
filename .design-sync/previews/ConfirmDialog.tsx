import { ConfirmDialog, CodeBlock } from '@server-guardian/ui';

export const HighRiskTypeToConfirm = () => (
  <div style={{ padding: 16, width: 512 }}>
    <ConfirmDialog
      inline
      risk="HIGH"
      title="Run this command on site-b-ec1?"
      description="Proposed by the judge in T-0142. It runs once over the hub's SSH key."
      confirmText="57"
      confirmLabel="Run action #57"
      onReject={() => {}}
    >
      <CodeBlock code={"echo 'PasswordAuthentication no' |\n  sudo tee /etc/ssh/sshd_config.d/90-guardian.conf\nsudo systemctl reload sshd"} />
    </ConfirmDialog>
  </div>
);

export const MediumRisk = () => (
  <div style={{ padding: 16, width: 512 }}>
    <ConfirmDialog
      inline
      risk="MEDIUM"
      title="Clear journal logs on site-b-ec1?"
      description="Frees about 6.2 GB on /var. Proposed by the judge in T-0141."
      confirmLabel="Run action #58"
    >
      <CodeBlock code="sudo journalctl --vacuum-time=7d" />
    </ConfirmDialog>
  </div>
);

export const LowRisk = () => (
  <div style={{ padding: 16, width: 512 }}>
    <ConfirmDialog
      inline
      risk="LOW"
      title="Re-run SSH audit on Site B Cluster?"
      description="Read-only check across site-b-ec1, site-b-ec2 and site-b-arm1."
      confirmLabel="Start audit"
    />
  </div>
);
