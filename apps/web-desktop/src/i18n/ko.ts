/**
 * Korean message catalog of the web dashboard: the one place where user-visible copy lives.
 *
 * Layout: this file composes `ko/<area>.ts` section files into one `ko` object, so components and tests do
 * `import { ko } from '../i18n/ko'` and read `ko.common.save`, `ko.fleet.nodes.title`, ...
 *   common   words shared by many screens (buttons, loading, errors, units)
 *   shell    app frame, navigation, hub connection / sign-in / settings dialogs, API error text
 *   ops      overview, approvals, command run
 *   fleet    nodes, node detail / registration, groups, SSH audit
 *   debate   debates page, start form, results, actions, personas / teams / projects
 *   agents   agents monitor (run list, lanes, timeline)
 *   wiki     code wiki tab
 *   artifacts design project artifacts tab
 *   results   project results tab (debate reports and artifacts of every project kind)
 *
 * RAW-VALUE RULE (applies everywhere)
 *   1. Data stays raw. Status / kind / enum values that come from the API or are compared in code
 *      (APPROVED, DISPUTED, PROPOSED, fresh / stale / broken, running, failed, event type names, ...) are never
 *      translated in state, props, URLs, comparisons or sent payloads.
 *   2. Display labels are Korean. Map the raw value to a label through a catalog table, e.g.
 *      `ko.debate.verdict[value] ?? value`; an unknown raw value falls back to itself, never to a blank.
 *   3. Where the raw value helps an operator (it is what the CLI, logs and API show), keep it visible: as a
 *      `title` tooltip ("APPROVED") or as small mono text next to the label. Chips with a Korean label use the
 *      raw value as `title`.
 *   4. Left in English on purpose: identifiers, API field names, event type names, routes, command / log /
 *      terminal output, code, file paths, host names, model and provider names, and terms operators type
 *      (sha, sudo, SSH, ssh-ed25519, CLI flags, JSON, HTTP codes). "Server Guardian" stays the product name.
 *   5. Interpolation: a string with parts is a function (n: number) => `${n}개`, never string concatenation
 *      in the component. Counters use the bare number + counter noun (개, 건, 명); no English plural logic.
 *
 * HOW TO ADD A STRING
 *   1. Find the section above (or `common` when 3+ screens need it). 2. Add a typed key there, grouped by screen.
 *   3. Reference it from the component: `{ko.fleet.nodes.addNode}`. 4. In tests import `ko` and assert on
 *   `ko.…` values instead of literals, so wording edits do not break tests.
 *
 * STYLE: concise polite-neutral UI tone. Buttons and labels are noun/verb stems ("저장", "삭제", "다시 쓰기",
 * "새로 고침"); sentences and confirmations use 해요체 ("이 페이지를 삭제할까요?", "저장했어요", "불러오지
 * 못했어요"). No trailing period on labels / titles; sentences keep it. Keep spacing per 국립국어원 rules.
 *
 * GLOSSARY: Hub 허브, Node 노드, Fleet 플릿, Group 그룹, Debate 토론, Round 라운드, Action 액션, Approval 승인,
 * Persona 페르소나, Team 팀, Project 프로젝트, Workspace 워크스페이스, Wiki 위키, Page 페이지,
 * Run (command) 실행, Job 작업, Hub agent 허브 에이전트, Debate agent 토론 에이전트, Critic 비평가,
 * Verdict 판정, Finding 발견 사항, Retry 다시 시도,
 * Refresh 새로 고침, Sign in 로그인, Sign out 로그아웃, Settings 설정, Audit 감사, Key 키, Token 토큰.
 */
import { common } from './ko/common';
import { shell } from './ko/shell';
import { ops } from './ko/ops';
import { fleet } from './ko/fleet';
import { debate } from './ko/debate';
import { agents } from './ko/agents';
import { wiki } from './ko/wiki';
import { artifacts } from './ko/artifacts';
import { personas } from './ko/personas';
import { results } from './ko/results';

export const ko = { common, shell, ops, fleet, debate, agents, wiki, artifacts, personas, results } as const;

export type Catalog = typeof ko;
