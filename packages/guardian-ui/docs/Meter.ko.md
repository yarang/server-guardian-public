---
category: Status
---

# Meter

[English](Meter.md) | 한국어

설정 가능한 임계값에 따라 초록, 주황, 빨강으로 자동 색칠되는 가로 사용량 미터다.

Props: `value`(0-100, 범위 밖은 잘림, 기본값 0), `label`(막대 앞에 표시되며 접근성 이름으로 쓰임), `thresholds`(경계값 포함, 기본값 warn 70, crit 85, `DEFAULT_THRESHOLDS`로 export됨), `showValue`(기본값 true), `size`(`sm`, `lg`, 기본값 `sm`), 그 밖의 모든 div 속성. `role="meter"`를 노출한다. `label`이 없으면 aria-label은 UI 메시지 `meterUsage`(영어 "Usage")다([README](../README.ko.md) 참고). 직접 쓸 수 있도록 `meterTone(value, thresholds)`도 export한다.

## 예시 1

```tsx
<Meter value={42} label="CPU" />
```

## 예시 2

```tsx
<Meter value={91} label="Disk /" size="lg" thresholds={{ warn: 75, crit: 90 }} />
```
