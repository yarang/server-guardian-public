---
category: Data
---

# NodeRow

[English](NodeRow.md) | 한국어

노드 식별 정보, 티어, 상태, 사용량 미터, 부하, SSH 점수를 보여 주는 플릿 표의 한 행이다.

Props: `name`(기본값 `node`), `arch`(기본값 `amd64`), `tier`(T1, T2, 기본값 T1), `isHub`(HUB 태그 추가), `status`(online, offline, 기본값 online), `cpu`, `mem`, `disk`(퍼센트), `load`(1분 평균 부하, 소수점 둘째 자리), `sshScore`(0-100, 칩 tone은 80 이상 ok, 60 이상 warn, 그 아래 crit). 오프라인 행은 이름을 흐리게 하고 미터와 부하 자리에 대시를 표시한다. 값이 없는 지표도 대시로 표시한다. 보통 같은 파일에 정의된 `NodeTable`을 통해 쓴다.

online/offline 텍스트, 값이 없는 지표와 SSH 점수의 aria-label은 UI 메시지의 `nodeTable` 섹션에서 온다([README](../README.ko.md) 참고).

## 예시 1

```tsx
<NodeRow name="site-a-arm1" arch="arm64" tier="T1" isHub status="online" cpu={34} mem={61} disk={48} load={0.42} sshScore={94} />
```

## 예시 2

```tsx
<NodeRow name="edge-mini" arch="amd64" tier="T2" status="offline" />
```
