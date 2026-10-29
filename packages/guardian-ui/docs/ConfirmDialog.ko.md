---
category: Overlays
---

# ConfirmDialog

[English](ConfirmDialog.md) | 한국어

운영자 승인용 확인 대화상자다. HIGH 위험도 조치는 문구를 입력해야 확인 버튼이 활성화된다.

Props: `open`(기본값 true), `title`, `description`, `risk`(기본값 LOW), `confirmText`(기본값 `CONFIRM`), `confirmLabel`, `children`, `onConfirm`, `onCancel`, `onReject`(Reject 버튼 추가), `inline`(오버레이 없음). `risk="HIGH"`이면 확인 버튼이 danger 버튼이 되고 `confirmText`를 정확히 입력할 때까지 비활성 상태로 남는다. 입력한 텍스트는 대화상자가 열릴 때마다 초기화된다. Escape는 `onCancel`을 호출한다. 모달 모드에서는 대화상자에 포커스를 옮기고 Tab 포커스를 안에 가둔다.

기본 제목, Confirm·Cancel·Reject 레이블, 위험도 칩 텍스트, "Type <text> to confirm" 레이블은 UI 메시지(`confirmDialog`, `reject`, `riskLabel`)에서 온다([README](../README.ko.md) 참고).

## 예시 1

```tsx
<ConfirmDialog
  inline
  risk="HIGH"
  title="Disable password login on site-b-ec1"
  description="This changes sshd_config and restarts sshd. Existing sessions stay connected."
  confirmText="site-b-ec1"
  confirmLabel="Run action"
  onConfirm={run}
  onCancel={close}
  onReject={reject}
>
  <CodeBlock code="systemctl restart sshd" copyable={false} />
</ConfirmDialog>
```

## 예시 2

```tsx
<ConfirmDialog inline risk="LOW" title="Restart guardian-agent" description="Brief metrics gap on site-a-arm1." onConfirm={run} onCancel={close} />
```
