# 프로젝트 결과물 탭

[English](project-results.md) | 한국어

상태: 2026-10-22 구현, 2026-10-23 배포. 2026-10-22에 운영자와 정했다: 기존 대시보드 안에 읽기 전용 "결과물" 탭을 두고, 모든
프로젝트 종류(코드, 운영, 설계)에서 쓴다. 발단은 이 질문이었다: 프로젝트가 만든 결과를 읽을 곳이 설계 프로젝트(산출물 탭)에만
있었고, 코드·운영 프로젝트에는 토론별 결과 문서 창만 있었다.

## 무엇을 보여주나

프로젝트마다 목록 하나, 최신순, 토론 스레드마다 한 줄. 스레드는 다음 중 하나라도 남기면 나타난다.

- 토론 결과 문서, `<data dir>/decisions/projects/<project>/<thread>.md` (`debate_report.py`가 씀)
- 설계 산출물, `<data dir>/design-artifacts/<project>/<thread>/vN/` (`artifact_ops.py`가 씀)

둘은 스레드 id로 합친다. 한 줄에는 제목, 결정 단어, 라운드 수, 산출물 개수, 경과 시간이 나온다. 줄을 누르면
`#/debates/results/<thread>`가 열리고, 결과 문서가 그 자리에 보인다(토론의 결과 문서 창과 같은 뷰어, `ReportView`). 산출물이
있는 설계 프로젝트라면 산출물 뷰어(`#/debates/artifacts/<thread>`) 링크도 나온다.

범위 밖: 감사 보고서(감사 A3 단계 몫), 액션·플랜 패널(승인 화면에서 접근), 모든 쓰기 동작.

## API

`GET /api/projects/{id}/results` (`apps/agent/results_handler.go`)는 읽기 전용이며 전역 `withSecurity(withAuth(mux))`
체인 뒤에 있고 `{"project", "items", "total"}`를 돌려준다. 항목은 `thread_id, title, status, rounds, updated_at, has_report,
report_bytes, artifact_versions, artifact_files`를 가진다. `Cache-Control: no-store`.

결과 문서·산출물 경로처럼 디스크를 직접 읽고 CLI가 필요 없다. 같은 방어를 재사용한다: 프로젝트는 `idPattern`(아니면 400),
스레드는 `threadPattern`, 파일은 `isRealDir`·`openRegular`(심볼릭 링크 거부, 일반 파일만).

디스크에서 온 값은 데이터이므로 한도를 둘 수 있는 것만 믿는다.

- 제목은 결과 문서 첫 제목 줄에서 가져오고, 제어·서식 문자(양방향 재정의 포함)를 지우고 120자(rune)에서 자른다. 제목이 없으면
  스레드 id를 보여준다.
- 결정 단어는 `[A-Z][A-Z_]{0,23}`에 맞아야 하고 아니면 버린다. 라운드는 999 이하 숫자여야 한다.
- 결과 문서는 앞 4 KiB만 읽는다.
- 응답에는 최신 스레드 200개만 담고, 더 있으면 `total`로 알린다.

## 대시보드

`apps/web-desktop`: `pages/results/ResultsTab.tsx`, `lib/results.ts`(경로 해석, 결정 단어의 색), 문구는
`i18n/{ko,en}/results.ts`와 `debate.page.tabResults`. 탭은 `DebatesPage`에서 모든 종류에 나온다(코드: 토론·위키·결과물, 운영:
토론·결과물, 설계: 토론·산출물·결과물). 결정 단어는 `debate.decisionLabel`로 표시하고 원래 단어는 칩의 title에 둔다. 모르는
단어는 그대로 보여준다.

## 구현 메모

- 목록은 요청 한 번이고 탭은 클라이언트에서 거르지 않는다. 한도를 넘은 오래된 스레드도 URL로는 열 수 있다. 결과 문서가
  없으면 결과 문서 경로가 404로 답한다.
- `ReportView`를 `ReportButton.tsx`의 대화상자에서 떼어내 불러오기, 내려받기, 404 처리를 둘이 함께 쓴다.
- 테스트: `apps/agent/results_handler_test.go`(순서, 병합, 이상한 머리글, 심볼릭 링크·이상한 이름, 검증, 한도),
  `tests/results.lib.test.ts`, `tests/results.test.tsx`.
- 남은 것: 배포, 허브의 실제 데이터로 탭을 브라우저에서 확인, A3 이후 감사 보고서를 목록에 넣을지.
