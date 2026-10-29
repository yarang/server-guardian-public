# Glossary

English | [한국어](glossary.ko.md)

One word for one thing, in the dashboard (both catalogs), the API, the CLI and the documents. Decided 2026-10-28 by the
operator. When a new screen, field or document needs a word that is not here, add it here first.

Not covered: the product name "Server Guardian", the inventory file name `servers.json` and `GUARDIAN_SERVERS_FILE`
(they name a file, not a concept), and the products "headscale server", "MCP server" and "SSH server", where "server"
means that software.

## Machines

| English | 한국어 | Meaning | Do not use |
|---|---|---|---|
| node | 노드 | Any machine in the fleet inventory, the hub included. This is what Guardian inspects and operates. API: `/api/nodes`, `node_start`, `total_nodes`, type `FleetNode`. | server, 서버, 머신, 장비 |
| fleet | 플릿 | All nodes of the inventory. | |
| inventory | 인벤토리 | The list of nodes (`servers.json`) with their metadata. | |
| node group | 노드 그룹 | A user-defined set of nodes (`docs/design/node-groups.md`). | |
| hub | 허브 | The one node that runs the hub agent, the dashboard and the data. Its `guardian_role` is `hub`. | |
| hub agent | 허브 에이전트 | The Go program in `apps/agent/` that runs on the hub. Other nodes run no agent. | "the agent" alone when a debate agent is meant |
| worker | 워커 | The resident, stateless model worker on an opted-in node (`docs/design/remote-agents.md`). | |
| execution host | 실행 호스트 | The machine where a debate agent's LLM CLI runs: the hub, or a worker node. It is not the node that gets inspected. | host as a synonym of node |
| host | 호스트 | Only inside "execution host", SSH "host key" and "hostname". | node |

The `guardian_role` of a node is `hub` or `node`. The legacy value `agent` is read as `node` until the inventory is
migrated.

## Debates

| English | 한국어 | Meaning | Do not use |
|---|---|---|---|
| debate | 토론 | One run of a team on a task. | 논의, 디베이트 |
| debate agent | 토론 에이전트 | A participant in a debate. Its roles: | agent alone |
| expert | 전문가 | The debate agent that drafts and consolidates. | |
| critic | 비평가 | A debate agent that checks the draft; a team has a panel of one to several. | 비판자, 크리틱 |
| proposer | 제안자 | An extra drafting agent in the first round (`proposers`). | |
| judge | 판정자 | The debate agent that gives the verdict. | 판사, 심판 |
| vote | 투표 | A critic's explicit approve or object on a plan. | |
| gate | 게이트 | The check that decides whether a plan or action may run without a person (jev gate, batch gate). | |
| team | 팀 | The lineup of debate agents with their personas and flow. | |
| project | 프로젝트 | The target of a debate: `code`, `ops` or `design`. | |
| thread | 스레드 | A series of rounds on one task. | 대화 |

## Changes

| English | 한국어 | Meaning | Do not use |
|---|---|---|---|
| action | 액션 | A typed, proposed change (`apply_patch`, `ops:docker_prune`, ...). It goes through the judge, the gate and, where required, an approval. | remediation as the name of the object |
| approval | 승인 | A person's decision on an action. | 허가, 결재 |
| plan | 계획 | An ordered list of actions across nodes (`plan_ops.py`). | |

In Korean running text, write what happens ("서비스를 재시작해요", "패키지를 삭제해요") instead of the noun 조치. The
noun 액션 names the object.
