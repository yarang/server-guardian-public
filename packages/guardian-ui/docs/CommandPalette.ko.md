---
category: Overlays
---

# CommandPalette

[English](CommandPalette.md) | 한국어

검색 범위, 그룹별 결과, 키보드 탐색을 갖춘 검색형 명령 팔레트 대화상자다.

Props: `open`(기본값 true), `query`, `onQueryChange`, `scopes`(`SegmentedControl`로 표시), `activeScope`, `onScopeChange`, `groups`(`{ name, items }`, 항목은 `{ icon?, label, sub?, shortcut?, tag?, tagTone? }`), `selectedIndex`(모든 그룹을 통틀은 인덱스), `onSelectedIndexChange`, `onSelect(item)`, `onClose`, `placeholder`, `inline`(오버레이 없음, 미리보기용). `query`와 `selectedIndex`는 controlled와 uncontrolled 모두 지원한다. 필터링은 호출하는 쪽이 한다. 화살표 키로 이동하고(끝에서 처음으로 돌아감), Enter로 선택하며, Escape와 배경 클릭은 `onClose`를 호출한다. 숨기려면 `open={false}`를 넘긴다.

대화상자·검색·범위·결과 레이블, "No results" 문구, 기본 placeholder는 UI 메시지의 `commandPalette` 섹션에서 온다([README](../README.ko.md) 참고).

## 예시

```tsx
<CommandPalette
  inline
  query="oci"
  scopes={['All', 'Nodes', 'Actions']}
  activeScope="All"
  selectedIndex={0}
  groups={[
    { name: 'Nodes', items: [
      { icon: <Icon name="server" />, label: 'site-a-arm1', sub: 'Hub · arm64 · online', shortcut: 'G H' },
      { icon: <Icon name="server" />, label: 'site-b-ec1', sub: 'T1 · amd64 · disk 91%' },
    ] },
    { name: 'Actions', items: [
      { icon: <Icon name="bolt" />, label: 'Approve act-7f3a', sub: 'ssh.harden on site-b-ec1', tag: 'HELD', tagTone: 'warn' },
    ] },
  ]}
/>
```
