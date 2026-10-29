---
category: Inputs
---

# SegmentedControl

[English](SegmentedControl.md) | 한국어

aria-pressed 버튼으로 만든 작은 단일 선택 토글 그룹이다.

Props: `options`(`{ value, label }`), `value`(controlled) 또는 `defaultValue`(uncontrolled, 없으면 첫 옵션), `onChange(value)`, `aria-label`(기본값은 UI 메시지 `segmentedOptions`, 영어 "Options", [README](../README.ko.md) 참고), `className`.

## 예시

```tsx
<SegmentedControl
  aria-label="Time range"
  options={[{ value: '1h', label: '1h' }, { value: '24h', label: '24h' }, { value: '7d', label: '7d' }]}
  defaultValue="24h"
  onChange={setRange}
/>
```
