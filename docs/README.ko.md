# 문서

[English](README.md) | 한국어

모든 문서는 영어(`name.md`, 기본본)와 한국어(`name.ko.md`)로 있다. 프로젝트 개요는 저장소의
[README](../README.ko.md)이고, 자세한 시스템 명세는 [agent.md](../agent.ko.md)이다.

## 용어

- [용어집: 한 가지 대상에는 한 가지 낱말 (노드, 실행 호스트, 비평가, 액션 ...)](glossary.ko.md)

## 설계

- [코드 위키의 범위 확장: 설계 문서 연결, 큰 범위 나누기, 파일 보기 (초안)](design/code-wiki-coverage.ko.md)
- [위키 뷰어: 시작할 수 있는 인덱스와 읽으면서 페이지 사이를 이동하기 (2026-10-27 승인, V0-V3 2026-10-28 배포, V4 모드 2026-10-28 배포)](design/wiki-viewer.ko.md)
- [웹 대시보드 다국어 지원 (메시지 카탈로그)](design/dashboard-i18n.ko.md)
- [대시보드 React 전환과 디자인 시스템 적용 (설계)](design/dashboard-react-migration.ko.md)
- [대시보드 디자인 언어: Notion 분석을 따른 웜 페이퍼 라이트 테마](design/dashboard-notion-theme.ko.md)
- [토론 에이전트의 코드 탐색과 Agents 모니터 (설계)](design/debate-agent-retrieval.ko.md)
- [토론 에이전트의 코드 읽기 (설계)](design/debate-code-context.ko.md)
- [토론 에이전트의 심볼 인덱스, 저장소 지도, 코드 위키 (설계)](design/debate-code-index-wiki.ko.md)
- [판정자 액션: 부분 수정 도구 (설계)](design/debate-patch-tool.ko.md)
- [토론 팀 UI 연동 계약 (Python CLI ↔ Go hub API ↔ 대시보드)](design/debate-teams-api.ko.md)
- [토론 팀(라인업) 설계](design/debate-teams.ko.md)
- [노드별 SSH 보안 감사 (fleet SSH audit)](design/fleet-ssh-audit.ko.md)
- [사용자 정의 노드 그룹 (Python CLI ↔ Go hub API ↔ 대시보드)](design/node-groups.ko.md)
- [허브 SSH 설정을 통한 노드 등록](design/node-registration.ko.md)
- [스레드 변경 게시](design/publish-thread.ko.md)
- [저장소 전체 감사 모드 (속성 감사, 발견 목록만, 설계 확정, A1·A2 구현됨)](design/repo-audit-mode.ko.md)
- [다각도 토론: 독립 제안과 블라인드 비판 (결정 확정, 구현 없음)](design/multi-perspective.ko.md)
- [설계·문서·이미지 산출물 프로젝트 (`kind: "design"`, 검토, P1 구현됨)](design/design-project.ko.md)
- [프로젝트 결과물 탭 (모든 종류 프로젝트의 결과 문서와 산출물, 구현됨·배포됨)](design/project-results.ko.md)
- [데스크톱 내보내기 폴더 (보고서와 산출물을 고른 폴더에 저장, 수동·자동, 승인됨·구현 없음)](design/desktop-export.ko.md)
- [오피스 내보내기: 보고서의 DOCX·PPTX, 모든 프로젝트의 다이어그램 (D2 DOCX 2026-10-28 배포; D3 PPTX와 보고서 ".mmd로 저장" 2026-10-28 `b50af88`로 배포; D1 렌더 배포됨; 보고서 스냅샷 "문서로 보관" 2026-10-28 구현, `f6497bd`로 배포)](design/office-export.ko.md)
- [설치 트랙: 개발자와 운영자 (설계 합의)](design/install-tracks.ko.md)
- [메시 가입 (대시보드에서 tailnet 노드 승인과 등록 해제)](design/mesh-join.ko.md)
- [노드 운영 프로젝트 (저장소 없는 ops 프로젝트, 노드 조회 MCP, 정리 액션 카탈로그)](design/server-ops-project.ko.md)
- [Fleet 프로비저닝 (계획에 따른 패키지 설치와 삭제: 노드 사실, 패키지 도구, 보호, 계획)](design/fleet-provisioning.ko.md)
- [일괄 실행: 여러 노드에 명령 세트를 동시에, 비평가가 표결하고 반대할 때만 사람이 결정](design/batch-execution.ko.md)
- [웹 대시보드 UI 흐름 재설계](design/ui-flow-redesign.ko.md)

## 운영

- [2026-10-05 Fleet sshd 하드닝 (MaxAuthTries, PermitRootLogin)](operations/2026-10-05-fleet-sshd-hardening.ko.md)
- [허브 백업과 복원](operations/backup-restore.ko.md)
- [headscale 운영 (tailnet 제어 서버, 비상 접속, 노드 추가)](operations/headscale.ko.md)

## 분석과 평가 (시점 기록)

- [Server Guardian 시스템 분석 보고서 (2026-10-04)](analysis/2026-10-04-system-analysis.ko.md)
- [동작하지 않는 API 정리 (2026-10-12)](analysis/2026-10-12-non-working-api.ko.md)
- [절차·UI 정합성 검토 보고서 (2026-10-12)](analysis/2026-10-12-procedure-review.ko.md)
- [1단계 - 토론 / 결정 / 조치 생애주기 검토](analysis/2026-10-12-procedure-review/phase1-debate.ko.md)
- [Phase 1 - 개발 워크플로 검토 (server-guardian)](analysis/2026-10-12-procedure-review/phase1-dev.ko.md)
- [Phase 1 - 서버 / 플릿 관리 검토 (운영 분석가)](analysis/2026-10-12-procedure-review/phase1-ops.ko.md)
- [UI 검토: apps/web-desktop (1단계, 읽기 전용)](analysis/2026-10-12-procedure-review/phase1-ui.ko.md)
- [Phase 3 - 토론 / 결정 / 조치 재검토](analysis/2026-10-12-procedure-review/phase3-debate.ko.md)
- [Phase 3 - 개발 워크플로 재검토 (5aa8e2e..52fbfe2)](analysis/2026-10-12-procedure-review/phase3-dev.ko.md)
- [3단계 - 서버 / 플릿 관리 재검토 (운영 분석가)](analysis/2026-10-12-procedure-review/phase3-ops.ko.md)
- [UI 재검토, 3단계 (apps/web-desktop, 52fbfe2 시점, 읽기 전용)](analysis/2026-10-12-procedure-review/phase3-ui.ko.md)
- [Phase 4 - 토론 / 결정 / 조치 재검토](analysis/2026-10-12-procedure-review/phase4-debate.ko.md)
- [Phase 4 - 개발 workflow 재검토 (52fbfe2..66cf41e)](analysis/2026-10-12-procedure-review/phase4-dev.ko.md)
- [Phase 4 - 서버 / 플릿 관리 재검토 (ops 분석가)](analysis/2026-10-12-procedure-review/phase4-ops.ko.md)
- [UI 재검토, phase 4 (66cf41e의 apps/web-desktop, 읽기 전용)](analysis/2026-10-12-procedure-review/phase4-ui.ko.md)
- [Phase 5 - 토론 / 결정 / 조치 재검토](analysis/2026-10-12-procedure-review/phase5-debate.ko.md)
- [Phase 5 - 개발 workflow 재검토 (66cf41e..b82310c)](analysis/2026-10-12-procedure-review/phase5-dev.ko.md)
- [Phase 5 - 서버 / 플릿 관리 재검토 (ops 분석가)](analysis/2026-10-12-procedure-review/phase5-ops.ko.md)
- [UI 재검토, phase 5 (7783afa의 apps/web-desktop, 읽기 전용)](analysis/2026-10-12-procedure-review/phase5-ui.ko.md)
- [코드 읽기 구조 변경 검토: Agentic MapReduce 제안 (2026-10-12)](analysis/2026-10-12-retrieval-architecture-review.ko.md)
- [탐색 평가 1차: 심볼 인덱스와 저장소 지도 (계획)](analysis/2026-10-12-retrieval-eval/plan.ko.md)
- [탐색 평가 1차 결과 (2026-10-12)](analysis/2026-10-12-retrieval-eval/results.ko.md)
- [평가 2 정답(ground truth) (server-guardian @ b82310c)](analysis/2026-10-13-retrieval-eval2/ground-truth-e.ko.md)
- [탐색 평가 2차 (계획)](analysis/2026-10-13-retrieval-eval2/plan.ko.md)
- [탐색 평가 2차 결과 (재실행, 2026-10-16~17)](analysis/2026-10-13-retrieval-eval2/results.ko.md)
- [감사 모드 A4 1차 검증 (2026-10-19)](analysis/2026-10-19-audit-a4/results.ko.md)
- [감사 모드 A4 2차 검증: 넓은 범위 (2026-10-19)](analysis/2026-10-19-audit-a4b/results.ko.md)
- [다중 초안 평가 결과 (2026-10-19)](analysis/2026-10-19-multi-draft-eval/results.ko.md)
