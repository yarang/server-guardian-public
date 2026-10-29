---
category: Data
---

# NodeRow

English | [한국어](NodeRow.ko.md)

One fleet table row showing node identity, tier, status, usage meters, load and SSH score.

Props: `name` (default `node`), `arch` (default `amd64`), `tier` (T1, T2; default T1), `isHub` (adds a HUB tag), `status` (online, offline; default online), `cpu`, `mem`, `disk` (percent), `load` (1-minute load average, two decimals), `sshScore` (0-100; chip tone ok from 80, warn from 60, crit below). Offline rows dim the name and show dashes for the meters and load; a missing metric also shows a dash. Normally used through `NodeTable`, which is defined in the same file.

The online/offline text and the aria-labels of unavailable metrics and of the SSH score come from the UI messages `nodeTable` section (see the [README](../README.md)).

## Example 1

```tsx
<NodeRow name="site-a-arm1" arch="arm64" tier="T1" isHub status="online" cpu={34} mem={61} disk={48} load={0.42} sshScore={94} />
```

## Example 2

```tsx
<NodeRow name="edge-mini" arch="amd64" tier="T2" status="offline" />
```
