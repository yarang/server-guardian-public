# 설계 정합성 확보 및 보안·운영 결함 해소 명세 (design-reconciliation.ko.md)

[English](design-reconciliation.md) | 한국어

상태: 확정안 (2026-11-01)  
대상 문서: `docs/design/*.md`, `docs/analysis/2026-10-12-procedure-review.ko.md`, `CLAUDE.md`, `agent.md`  
관련 문서: [server-ops-project.ko.md](server-ops-project.ko.md), [fleet-provisioning.ko.md](fleet-provisioning.ko.md), [batch-execution.ko.md](batch-execution.ko.md), [design-project.ko.md](design-project.ko.md), [repo-audit-mode.ko.md](repo-audit-mode.ko.md)

---

## 1. 개요 및 목적

Server Guardian의 23개 설계 문서는 기능 확장에 따라 빠르게 진화해 왔으나, 그 과정에서 다음과 같은 구조적 결함이 누적되었습니다:
1. 이전 설계서와 후속 설계서 간의 전제 및 명세 충돌 (Specification Drift)
2. 일괄 실행 및 즉석 스크립트 도입에 따른 보안 경계선 완화 (Security Boundary Erosion)
3. Fleet SSH 키 로테이션 및 루프백 우회 등 절차 검토에서 지적된 치명적 결함의 방치 (Unresolved Critical Flaws)
4. 미검증 패키지 어댑터 및 미완료 기능의 관리 부재 (Unverified & Unfinished Roadmap)
5. 설계서 본문과 구현 메모의 혼재로 인한 진실 원천(Single Source of Truth) 약화

본 문서는 이러한 5대 문제를 체계적으로 해소하기 위한 기술적 해결 명세와 설계서 개정 기준을 확정합니다.

---

## 2. 명세 단일화 및 충돌 해소 (Single Source of Truth)

### 2.1 구성 관리(Configuration Management) 범위 경계 재확립
* **문제점**: `server-ops-project.ko.md` 2장의 "구성 관리는 비목표"라는 명세와 `fleet-provisioning.ko.md` 2장의 "패키지 설치/삭제는 범위에 포함"이라는 결정이 충돌하여 시스템 경계가 모호해짐.
* **해소 사양**:
  - `server-ops-project.ko.md`의 범위를 공식 개정합니다:
    - **포함(In-Scope)**: OS 및 런타임 패키지 라이프사이클 관리(`ops:package_install`, `ops:package_remove`), 서비스 상태 제어(`ops:restart_service`), 안전한 디스크 정리(`ops:docker_prune`, `ops:journal_vacuum`, `ops:apt_clean`).
    - **제외(Out-of-Scope)**: Ansible/Puppet 류의 임의 파일 템플릿 배포, OS 사용자 계정 관리, 전역 방화벽 정책 생성 등 일반 구성 관리(Configuration Management).

### 2.2 가상 모듈(`ops_mcp.py`) 명세 정리
* **문제점**: `server-ops-project.ko.md`에 기술된 `ops_mcp.py`와 `OPS_TOOLS`가 실제 구현되지 않고 단일 `SERVER_TOOLS` 및 `ops_actions.py`로 흡수되었으나, 설계서에 가상 명세로 남아 타 설계팀(design-project 등)에 혼선을 초래함.
* **해소 사양**:
  - `server-ops-project.ko.md` 및 `agent.md` 내 `ops_mcp.py` 표기를 완전히 제거하고, 실제 아키텍처인 `cli/llm-agent/ops_actions.py`(도구 구현체)와 `apps/agent/` Go API 엔드포인트 간의 계약으로 문서를 현행화합니다.

### 2.3 프로젝트 종류(`kind`) 확장 모델 구조화
* **문제점**: `mcp_client.py`의 `(name in SERVER_TOOLS) == (self.kind == "ops")`와 같은 이분법적 불리언 식이 신규 `kind: "design"` 추가 시 보안 구멍(코드 수정 도구 노출)을 유발함.
* **해소 사양**:
  - `kind`별 기능을 정적 매핑 테이블 `KIND_CAPABILITIES`로 단일화하여 `project_registry.py`와 `mcp_client.py`에서 공통 참조합니다:
    ```python
    KIND_CAPABILITIES = {
        "code": {
            "has_workspace": True,
            "allowed_tools": {"read_file", "apply_patch", "run_tests", "list_dir", "grep_search", "find_by_name"},
            "auto_apply_allowed": True,
        },
        "ops": {
            "has_workspace": False,
            "allowed_tools": {"ops:package_install", "ops:package_remove", "ops:restart_service",
                              "ops:docker_prune", "ops:journal_vacuum", "ops:apt_clean",
                              "ops:run_command_set", "ops:run_script"},
            "auto_apply_allowed": False,
        },
        "design": {
            "has_workspace": False,
            "allowed_tools": {"write_artifact"},
            "auto_apply_allowed": False,
        }
    }
    ```

---

## 3. 보안 안전 경계 재강화 (Security Hardening)

### 3.1 즉석 스크립트(`ops:run_script`) 자동 실행 통제선 복원
* **문제점**: `batch-execution.ko.md` F1에서 비판자 표결만으로 `script_groups` 호스트에 즉석 셸 스크립트를 무인 실행할 수 있도록 허용했으나, 거부 목록 정적 검사(`_BYPASS_PATTERNS`)의 불완전성을 문서 스스로 인정하고 있어 보안 위험이 큼.
* **해소 사양**:
  1. **즉석 스크립트의 무인 실행 금지**: `ops:run_script`는 어떠한 프로젝트나 호스트 그룹에서도 `policy.batch.auto` 대상이 될 수 없으며, **항상 사람의 명시적 승인(`--yes` 또는 UI 확인)**을 받아야만 실행됩니다.
  2. **자동 실행은 검증된 명령 세트로 일원화**: 무인 일괄 실행은 오직 대시보드에서 내용 다이제스트가 서명·확인된 명령 세트(`ops:run_command_set` with `.confirmed.json`)에 한해서만 허용합니다.
  3. `policy.batch.scripts: "vote"` 옵션은 폐지하고 `"approve"`(사람 승인 필수)만을 유효한 정책으로 강제합니다.

### 3.2 비판자 표결 정족수(Quorum) 및 Fail-Closed 원칙 확립
* **문제점**: `batch-execution.ko.md` 5.3의 "기권은 찬성으로 센다(`abstain -> approve`)"는 규칙으로 인해, 모든 비판자 모델이 타임아웃되거나 오류를 내어 정족수가 0이 되어도 관문이 열릴 수 있음.
* **해소 사양**:
  1. **정족수(Quorum) 도입**: 라인업에 등록된 비판자 패널의 **최소 2인 이상(또는 1인 패널인 경우 1인 전원)**의 명시적 `approve` 표가 수신되어야만 게이트 통과를 인정합니다.
  2. 모델 장애, API 제한, 타임아웃 등으로 인한 기권(`abstain`)은 찬성으로 환산되지 않으며, 정족수 미달 시 게이트는 `auto: false` (사유: `insufficient_quorum`)로 판정되어 사람의 수동 승인으로 전환됩니다.

---

## 4. 고위험 운영 결함의 정식 아키텍처 반영 (OPS Resolving)

### 4.1 Fleet SSH Key 안전 로테이션 명세 (`OPS-P5-1`, `OPS-P5-4`)
* **문제점**: `--rotate`가 Tailscale IP 노드만 변경하여 대시보드 등록 노드의 접속을 단절시키며, 임시 개인키가 `ubuntu` 소유 디스크 파일로 노출되고 구 키가 영구 보존됨.
* **해소 사양**:
  1. **인벤토리 전수 검증**: 로테이션 대상은 `servers.json` 및 `guardian_fleet.conf`의 활성 노드 전체를 대상으로 하며 부분 서브넷 하드코딩을 금지합니다.
  2. **메모리 런타임 격리**: 임시 키 생성 위치를 `/run/guardian-fleet/`(0700, root:guardian-fleet)로 강제하여 일반 계정의 접근을 원천 차단합니다.
  3. **3단계 안전 배포**:
     - 1단계: 플릿 전체 노드에 신규 공개키 배포 및 전수 로그인 테스트.
     - 2단계: 100% 성공 시에만 허브의 `/etc/guardian/fleet_key`를 원자적으로 교체. 단 1대라도 실패 시 신규 공개키를 회수하고 즉시 중단.
     - 3단계: 전체 노드에서 구 공개키 제거 및 구 개인키 `shred -u` 소거.

### 4.2 허브 루프백 SSH 권한 우회 차단 (`OPS-P5-2`)
* **문제점**: 허브 sudo 차단이 fleet 라우트만 막고, 허브 `authorized_keys`에 플릿 키가 남아 있어 `/api/exec`에서 `ssh localhost sudo` 호출이 가능함.
* **해소 사양**:
  1. 허브 자신의 `authorized_keys`에서 Fleet 키를 완전히 제거하고 설치 스크립트(`install-agent-service.sh`)에서 허브 자체 키 주입을 차단합니다.
  2. 허브의 `/etc/ssh/sshd_config`에 다음 블록을 강제 적용합니다:
     ```
     Match Address 127.0.0.1,::1,<Hub-Tailscale-IP>
         DenyUsers ubuntu
     ```
     (로컬 작업은 SSH가 아닌 허브 내부 프로세스 러너 `executeRaw`를 통해서만 수행)

---

## 5. 미검증 매니저 및 미완료 기능 생명주기 관리

### 5.1 패키지 매니저의 실서버 검증 격리 (`ops_packages.py`)
* **사양**:
  1. `apt`, `npm`, `brew`를 제외한 `dnf`, `zypper`, `apk`, `pacman`, `bun`은 `GUARDIAN_PACKAGES_ALLOW_UNVERIFIED`에 묶어 두는 것을 넘어, 프로젝트 정책 `policy.allowed_package_managers`에 명시적으로 활성화하지 않는 한 제안 단계에서 `INVALID`로 거부합니다.
  2. 허브 내 `systemd-nspawn` 컨테이너 기반의 정기 테스트 하네스를 구축하여 각 배포판별 시뮬레이션 출력과 실제 설치/삭제 결과를 자동 검증한 뒤에만 프로덕션 허용 매니저로 승격합니다.

### 5.2 설계 문서 상태 메타데이터 표준화
모든 설계 문서 최상단에 표준화된 4단계 생명주기 상태를 명시하여 독자 혼선을 방지합니다:
* `PROPOSED`: 검토 중인 제안.
* `ACCEPTED`: 승인되어 구현 진행 중인 사양.
* `IMPLEMENTED`: 구현 및 검증 완료된 현재 프로덕션 사양.
* `DEPRECATED`: 후속 설계에 의해 대체되거나 폐기된 사양 (대체 문서 링크 필수).

---

## 6. 문서 거버넌스 및 다국어(ko/en) 동기화

1. **단일 진실 원천(Single Source of Truth) 원칙**:
   - 설계 문서 본문은 항상 "현재 유효한 최종 스펙"만을 기술합니다.
   - 시점별 절차 검토, 논쟁 이력, 트러블슈팅 메모는 본문에서 분리하여 `## 부록: 구현 및 결정 이력`으로 일괄 이동합니다.
2. **Bilingual 검증 자동화**:
   - `scripts/ci.sh config`에 `scripts/check-docs-sync.py`를 추가하여 영문(`name.md`)과 국문(`name.ko.md`)의 목차(H2, H3), 코드 블록 수, 메타데이터 상태 일치 여부를 정적으로 검증합니다.
