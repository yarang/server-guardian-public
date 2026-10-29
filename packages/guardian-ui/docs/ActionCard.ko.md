---
category: Actions
---

# ActionCard

[English](ActionCard.md) | 한국어

위험도·상태 칩, 명령 미리보기, 승인/거부 버튼을 갖춘 복구 조치 제안 카드다.

Props: `id`(기본값 `act-0000`), `tool`(기본값 `tool`), `risk`(LOW, MEDIUM, HIGH, 기본값 LOW), `status`(PROPOSED, HELD, RUNNING, EXECUTED, FAILED, REJECTED, 기본값 PROPOSED), `title`, `meta`, `command`(복사 버튼 없는 `CodeBlock`으로 표시), `warning`(note 콜아웃), `onApprove`, `onReject`, `className`. 사람의 승인이 필수이므로 Approve와 Reject 버튼은 PROPOSED와 HELD 조치에만 나타난다.

위험도 칩 텍스트, Approve와 Reject 레이블, 대체 제목("Untitled action"), 카드의 aria-label("Action <id>")은 UI 메시지(`riskLabel`, `approve`, `reject`, `untitledAction`, `actionAria`)에서 온다([README](../README.ko.md) 참고).

## 예시 1

```tsx
<ActionCard
  id="act-7f3a"
  tool="ssh.harden"
  risk="HIGH"
  status="HELD"
  title="PasswordAuthentication 비활성화"
  meta="site-b-ec1 · proposed by Remediation agent"
  command="sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config"
  warning="jev-mcp gate held this action: no console fallback verified."
  onApprove={() => approve('act-7f3a')}
  onReject={() => reject('act-7f3a')}
/>
```

## 예시 2

```tsx
<ActionCard id="act-81c2" tool="systemctl" risk="LOW" status="EXECUTED" title="Restart guardian-agent" meta="site-a-arm1" />
```
