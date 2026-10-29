# 웹 대시보드 다국어 지원 (메시지 카탈로그)

[English](dashboard-i18n.md) | 한국어

`apps/web-desktop`의 화면 문구는 언어별 메시지 카탈로그(`src/i18n/`)에서 관리한다. 지금은 한국어(기본)와 영어를 지원한다. i18n 라이브러리는 쓰지 않는다(의존성 추가 없음). 언어는 페이지를 불러올 때 한 번 정하고, 바꾸면 페이지를 다시 불러온다.

## 구조

```
apps/web-desktop/src/i18n/
  index.ts         언어 선택: LANGUAGES, tr, language, locale, setLanguage, <html lang>와 document.title
  types.ts         Messages 타입: 모든 카탈로그가 따라야 하는 형태
  uiMessages.ts    디자인 시스템 문구를 활성 언어로 설정 (setUiMessages(tr.shell.ui))
  ko.ts            한국어 카탈로그 진입점. 규칙·문체·용어집 주석 + 섹션을 합쳐 `ko` 객체로 export
  ko/common.ts     여러 화면이 같이 쓰는 말(저장, 취소, 불러오는 중…, 상대 시간 "5분 전" 등)
  ko/shell.ts      앱 틀, 내비게이션, 허브 연결·로그인·설정, 언어 선택, 디자인 시스템 문구(ui), API 오류 문구
  ko/ops.ts        개요, 승인, 명령 실행
  ko/fleet.ts      노드, 노드 상세·등록, 그룹, SSH 감사
  ko/debate.ts     토론 페이지, 시작 폼, 결과, 액션, 페르소나·팀·프로젝트
  ko/agents.ts     에이전트 모니터(실행 목록, 레인, 타임라인)
  ko/wiki.ts       코드 위키 탭
  en.ts            영어 카탈로그 진입점 (`en: Messages`)
  en/*.ts          ko/와 같은 섹션 파일
```

컴포넌트는 `import { tr } from '../i18n'`로 활성 카탈로그를 가져와 `tr.fleet.…`처럼 읽는다. 카탈로그 파일(`ko`, `en`)을 직접 import하지 않는다. 문구 조각이 있는 문장은 문자열 이어 붙이기 대신 함수(`` (n: number) => `${n}개` ``)로 둔다.

## 언어 선택 (`src/i18n/index.ts`)

- `LANGUAGES`는 언어 코드마다 카탈로그와 BCP 47 locale을 둔다: `ko` → `ko-KR`, `en` → `en-US`. `DEFAULT_LANGUAGE`는 `ko`다.
- 페이지를 불러올 때 localStorage 키 `guardian.language`(`LANGUAGE_STORAGE_KEY`)를 읽는다. 값이 없거나 모르는 언어이거나 저장소에 접근할 수 없으면 한국어다. 선택은 브라우저마다 저장되고 허브에는 저장하지 않는다.
- 그 결과를 `language`(언어 코드), `tr`(활성 카탈로그), `locale`로 export한다. `lib/format.ts`의 `LOCALE`이 이 `locale`이므로 `fmtDateTime()` 등 모든 날짜·숫자가 활성 언어 형식으로 나온다.
- 불러올 때 `<html lang>`을 그 언어로, `document.title`을 `tr.shell.documentTitle`로 바꾼다. `index.html`에는 기본 언어의 제목과 `lang="ko"`가 들어 있어 이 코드가 돌기 전까지 쓰인다.
- `setLanguage(next)`는 선택을 저장하고 페이지를 다시 불러온다. 같은 언어면 아무것도 하지 않는다. 저장에 실패하면(비공개 창 등) `false`를 돌려주고 다시 불러오지 않는다. 같은 언어로 다시 불러오는 일을 막기 위해서다.
- 다시 불러오는 이유: 화면, 표의 열, 모듈을 불러올 때 만든 상수(라우트 라벨 등)가 모두 같은 카탈로그를 쓰게 하기 위해서다. 실행 중에 카탈로그를 바꾸는 경로는 없다.
- 디자인 시스템(`packages/guardian-ui`) 컴포넌트의 문구(복사 버튼, 표 머리글, aria 라벨 등)는 `i18n/uiMessages.ts`가 앱 시작 때 `setUiMessages(tr.shell.ui)`로 활성 언어에 맞춘다. `main.tsx`와 `tests/setup.ts`가 이 파일을 import한다.

## 설정 화면의 언어 선택

설정 > 허브 연결(`#/settings/endpoint`)에 "언어" 패널이 있다(`SettingsPage.tsx`의 `LanguageSection`).

- 선택 상자는 `LANGUAGES`의 언어를 각 언어 자신의 이름(`shell.language.names`, 예: 한국어, English)으로 보여 준다.
- 고르면 `setLanguage`를 부르고 페이지가 그 언어로 다시 열린다. 안내 문구(`shell.language.hint`)는 이 브라우저에만 저장된다고 알려 준다.
- 저장하지 못하면 페이지는 지금 언어로 남고 경고(`shell.language.saveFailed`)를 보여 준다.

## Messages 타입 (`src/i18n/types.ts`)

`Messages`는 한국어 카탈로그의 타입(`Catalog = typeof ko`)에서 문자열 리터럴 타입을 `string`으로 넓힌 것이다. 다른 언어의 카탈로그는 `Messages` 타입으로 선언한다(`export const en: Messages = …`). 그래서

- 키가 빠지거나, 남거나, 타입이 다르면 컴파일 오류다.
- 함수 키는 인자와 반환 타입이 같아야 한다. 문장은 언어마다 자기 함수로 만든다(영어는 `n === 1 ? 'item' : 'items'` 같은 복수형, 한국어는 숫자 + 단위 명사).

## 원시값 규칙 (raw-value rule)

1. **데이터는 원시값 그대로 둔다.** API가 돌려주거나 코드가 비교하는 상태·종류 값(`APPROVED`, `DISPUTED`, `PROPOSED`, `fresh` / `stale` / `broken`, `running` / `failed`, 이벤트 타입 이름 등)은 state, props, URL, 비교, 전송 payload에서 번역하지 않는다. 어느 언어로 보든 같다.
2. **표시용 라벨만 활성 언어로 바꾼다.** 카탈로그의 표로 변환하고 모르는 값은 원시값 그대로 보여 준다(`tr.x.label[value] ?? value`). 빈칸으로 두지 않는다.
3. **운영자가 CLI·로그·API에서 보는 값은 화면에도 남긴다.** `title` 툴팁이나 작은 mono 글씨로 원시값을 함께 표시한다.
4. **번역하지 않는 것**: 식별자, API 필드명, 이벤트 타입 이름, 라우트, 명령·로그·터미널 출력, 코드, 파일 경로, 호스트 이름, 모델·제공자 이름, 운영자가 직접 입력하는 용어(sha, sudo, SSH 키 이름, CLI 플래그, JSON, HTTP 코드). 허브·API·Python이 돌려주는 서버 오류 문구도 그대로 보여 준다. 제품 이름 "Server Guardian"도 그대로다.
5. **날짜·시간**은 `lib/format.ts`의 `fmtDateTime()`(활성 언어의 locale)과 `ago()` / `duration()` / `shortUptime()`을 쓴다. 단위 문구는 카탈로그에서 온다. UTC·상대 시간 계산 방식은 언어와 무관하다.

## 문체

- 한국어: 간결한 해요체/명사형 혼용. 버튼·라벨은 명사·어간형(저장, 삭제, 다시 쓰기, 새로 고침), 문장·확인창은 해요체("이 페이지를 삭제할까요?", "저장했어요"). 라벨·제목에는 마침표를 붙이지 않는다.
- 영어: 평이한 sentence case, 라벨·제목에는 마침표를 붙이지 않는다.
- 용어집은 `ko.ts` 머리 주석(허브, 노드, 플릿, 그룹, 토론, 승인, 위키 …)을 따른다. 규칙과 용어집은 `ko.ts`에만 있고 `en.ts`는 그것을 가리킨다.

## 테스트

- `tests/i18n.test.tsx`:
  - 영어 카탈로그가 한국어와 정확히 같은 키와 함수 인자 수를 갖는다.
  - 영어의 모든 문자열과, 표본 인자로 부른 함수의 결과가 빈 문자열이 아니다.
  - 모든 언어의 이름이 모든 카탈로그에서 같다(각 언어 자신의 이름).
  - 저장된 값이 없거나 모르는 값이면 한국어이고, 저장된 언어가 카탈로그, locale, `<html lang>`, `document.title`, `format.LOCALE`을 정한다.
  - `setLanguage`는 저장하고 다시 불러오며, 언어가 같거나 저장에 실패하면 둘 다 하지 않는다.
  - 영어가 저장되어 있으면 설정 화면이 영어로 그려진다.
- 다른 테스트는 기본 언어(한국어)로 돌고, 리터럴 대신 `ko.…` 값을 import해서 단언한다(그 환경에서는 `tr`가 `ko`다). 문구를 고쳐도 테스트가 깨지지 않는다.

## 문구 추가 방법

1. 알맞은 섹션 파일 `ko/<영역>.ts`에 화면별로 묶어 키를 추가한다(세 화면 이상이 쓰면 `common.ts`).
2. 같은 키를 `en/<영역>.ts`에 영어로 추가한다. 빠뜨리면 컴파일러가 알려 주고 `i18n.test.tsx`도 실패한다.
3. 컴포넌트에서 `{tr.fleet.nodes.addNode}`처럼 참조한다. 하드코딩 문자열을 두지 않는다.
4. 테스트는 리터럴 대신 `ko.…` 값을 import해서 단언한다.
5. 디자인 시스템(`packages/guardian-ui`)의 영어 기본값은 라이브러리 자체 테스트·스토리를 위해 유지한다. 앱은 `shell.ui`(→ `setUiMessages`)나 props로 활성 언어의 문구를 넘긴다.

## 언어 추가 방법

1. `src/i18n/<코드>.ts`(와 섹션 파일 `<코드>/*.ts`)를 `Messages` 타입으로 만든다. 컴파일러가 빠진 키를 모두 알려 준다.
2. `index.ts`의 `LANGUAGES`에 그 언어를 BCP 47 locale과 함께 추가한다.
3. 모든 카탈로그의 `shell.language.names`에 그 언어의 이름(그 언어 자신의 말로)을 추가한다. 설정 화면의 선택 상자에 자동으로 나타난다.
4. `tests/i18n.test.tsx`의 키·빈 문자열 검사는 지금 `en`만 비교하므로 새 카탈로그도 같은 검사에 넣는다.
