---
category: Data
---

# Heatmap

[English](Heatmap.md) | 한국어

점검 행렬(예: 노드별 sshd 감사)이다. 각 셀은 기호를 표시하므로 상태가 색으로만 전달되지 않는다.

## Props

| Prop | 타입 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `columns` | `Array<string \| HeatmapColumn>` | `[]` | `HeatmapColumn`은 `{ label, title?, key? }`다. `title`은 헤더 툴팁이자 접근성 설명이 되고, `key`는 React key다(기본값은 `label`). 헤더 셀은 줄바꿈하지 않으며 표는 가로로 스크롤된다. |
| `rows` | `HeatmapRow[]` | `[]` | 아래 참고. |
| `rowLabel` | `string` | UI 메시지 `heatmap.rowLabel`(영어 `'Node'`) | 첫 열의 헤더. |
| `onSelectRow` | `(id: string) => void` | — | 각 행 헤더를 `aria-pressed`가 있는 실제 `<button type="button">`으로 만든다. 없으면 아무것도 상호작용하지 않는다. |
| `selectedId` | `string` | — | 이 id의 행을 강조한다(배경 색조와 얇은 안쪽 테두리). |
| `legend` | `boolean` | `false` | 표 아래에 범례를 표시하며, 실제로 나타난 셀 상태만 나열한다. |
| `scoreLabel` | `string` | UI 메시지 `heatmap.scoreLabel`(영어 `'Score'`) | 점수 열의 헤더. |
| `showScore` | `boolean` | `true` | `false`면 점수 열을 숨긴다. |
| `className` | `string` | — | 바깥 스크롤 래퍼에 붙는 추가 클래스. |

`HeatmapRow`: `{ name, id?, cells, score?, unavailable?, status? }`

- `id`는 React key이자 `onSelectRow`에 넘기고 `selectedId`와 비교하는 값이다. 기본값은 `name`이므로 이름이 겹칠 수 있으면 id를 준다.
- `unavailable`(사유 문자열)은 점검 셀들을 모든 점검 열에 걸친 하나의 셀로 바꾸어 사유를 표시하고, 점수 열에는 "—"를 표시한다.
- `status`(`{ tone, label }`)는 행 이름 옆에 작은 `StatusChip`을 렌더링한다(예: `partial`).
- `score`가 없는 행은 점수 열에 "—"를 표시한다.

셀 상태(`HeatmapCell`):

| 상태 | 기호 | Tone | 레이블 |
| --- | --- | --- | --- |
| `P` | ✓ | ok | Pass |
| `W` | ! | warn | Warn |
| `F` | ✕ | crit | Fail |
| `I` | i | info | Info(참고용, 예: 기본 포트 22) |
| `N` | – | idle, 점선 | Not checked |

각 셀에는 "Root login: Fail" 같은 aria-label이 붙는다. 열 레이블이 없는 셀은 "Check <n>"을 쓴다. 위 레이블은 UI 메시지 `heatmap.words`의 영어 기본값이며, 범례의 aria-label과 "Check <n>"도 `heatmap` 섹션에서 온다([README](../README.ko.md) 참고).

## 예시

규칙 툴팁, 연결할 수 없는 노드, 범례를 포함한 플릿 sshd 감사:

```tsx
const columns = [
  { key: 'SEC-SSH-001', label: 'Root login', title: 'SEC-SSH-001: Disable Direct Root Login via SSH' },
  { key: 'SEC-SSH-002', label: 'Password auth', title: 'SEC-SSH-002: Enforce Key-Based Authentication Only' },
  { key: 'SEC-SSH-003', label: 'Pubkey auth', title: 'SEC-SSH-003: Enable Public Key Authentication' },
  { key: 'SEC-SSH-004', label: 'MaxAuthTries', title: 'SEC-SSH-004: Limit Maximum Authentication Attempts' },
  { key: 'SEC-SSH-005', label: 'Port', title: 'SEC-SSH-005: Custom Non-standard SSH Port' },
];

<Heatmap
  legend
  columns={columns}
  rows={[
    { id: 'a1', name: 'site-a-arm1', cells: ['P', 'P', 'P', 'P', 'I'], score: 95 },
    { id: 'b2', name: 'site-b-ec1', cells: ['F', 'F', 'P', 'N', 'P'], score: 40, status: { tone: 'warn', label: 'partial' } },
    { id: 'm1', name: 'edge-mini', cells: [], unavailable: 'unreachable: ssh: Could not resolve hostname edge-mini' },
  ]}
/>
```

상세 보기를 구동하는 선택 가능한 행:

```tsx
const [selected, setSelected] = useState('b2');

<Heatmap columns={columns} rows={rows} selectedId={selected} onSelectRow={setSelected} />
```
