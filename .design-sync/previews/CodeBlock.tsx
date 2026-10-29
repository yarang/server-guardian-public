import { CodeBlock } from '@server-guardian/ui';

export const SshdFix = () => (
  <div style={{ padding: 16, width: 840 }}>
    <CodeBlock
      title="bash · site-b-ec1"
      code={"sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config\nsshd -t && systemctl reload ssh"}
    />
  </div>
);

export const DiffPatch = () => (
  <div style={{ padding: 16, width: 840 }}>
    <CodeBlock
      diff
      title="/etc/ssh/sshd_config"
      code={'@@ -56,7 +56,7 @@\n # To disable tunneled clear text passwords, change to no here!\n- PasswordAuthentication yes\n+ PasswordAuthentication no\n PermitEmptyPasswords no\n KbdInteractiveAuthentication no'}
    />
  </div>
);

export const DiskOutput = () => (
  <div style={{ padding: 16, width: 840 }}>
    <CodeBlock
      copyable={false}
      title="df -h /"
      code={'Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1        45G   41G  4.1G  91% /'}
    />
  </div>
);

export const Bare = () => (
  <div style={{ padding: 16, width: 840 }}>
    <CodeBlock copyable={false} code={'sshd -t && systemctl reload ssh'} />
  </div>
);
