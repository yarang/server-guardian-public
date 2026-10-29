# @server-guardian/ui

[English](README.md) | 한국어

Server Guardian 운영 콘솔용 웜 페이퍼 라이트 React 컴포넌트다(디자인 언어: [docs/design/dashboard-notion-theme.ko.md](../../docs/design/dashboard-notion-theme.ko.md)).

## 사용법

```tsx
import '@server-guardian/ui/styles.css';
import { GuardianRoot, StatusChip } from '@server-guardian/ui';

<GuardianRoot>
  <StatusChip tone="ok">APPROVED</StatusChip>
</GuardianRoot>;
```

JS 엔트리는 CSS를 import하지 않는다. `@server-guardian/ui/styles.css`를 한 번 import한다. 이 파일에는 토큰, 컴포넌트 스타일, `@font-face` 규칙이 들어 있고, woff2 파일은 `dist/assets`에 있다.

대시보드(`apps/web-desktop`)는 React 앱이다. `src/main.tsx`가 스타일시트를 import하고 앱 전체를 `GuardianRoot` 안에서 렌더링한다. 컴포넌트를 조금씩 도입하는 바닐라 페이지는 `import { mount } from '@server-guardian/ui/mount'`로 아일랜드를 마운트한다. `mount(el, node)`는 언마운트 함수를 돌려주며, `GuardianRoot`를 대신 감싸 주지 않는다.

패키지는 공용 타입 `Tone`(`ok`, `warn`, `crit`, `info`, `idle`), `Risk`(`LOW`, `MEDIUM`, `HIGH`)와 위험도를 tone으로 바꾸는 `RISK_TONE` 맵도 export한다.

## UI 메시지

컴포넌트에 내장된 화면 텍스트와 접근성 텍스트(복사 버튼, 표 헤더, aria 레이블, 위험도와 상태 단어, 대화상자 버튼, placeholder)는 기본이 영어다(`DEFAULT_UI_MESSAGES`, 타입 `UiMessages`). 애플리케이션은 시작할 때 렌더링 전에 `setUiMessages(overrides)`로 라이브러리 전체를 한 번 현지화한다. 각 섹션은 영어 기본값 위에 병합되므로 일부만 담은 객체로 충분하다. `getUiMessages()`는 현재 텍스트를 돌려준다. 컴포넌트에 prop으로 넘긴 텍스트는 언제나 이 기본값보다 우선한다.

대시보드는 현재 언어의 메시지 카탈로그로 이 텍스트를 채운다. `apps/web-desktop/src/i18n/uiMessages.ts`가 `setUiMessages(tr.shell.ui)`를 호출하고 `src/main.tsx`가 이 파일을 import하므로, 컴포넌트는 대시보드의 나머지 부분과 함께 한국어나 영어로 표시된다.

```tsx
import { setUiMessages } from '@server-guardian/ui';

setUiMessages({ copy: '복사', copied: '복사됨', confirmDialog: { cancel: '취소' } });
```

## 스크립트

- `npm run typecheck` / `npm test` / `npm run test:coverage`
- `npm run build`는 `dist/index.js`, `dist/mount.js`, `.d.ts` 트리, `dist/guardian-ui.css`, `dist/assets/*`(폰트)를 만든다.

컴포넌트별 문서는 `docs/`에 있다.
