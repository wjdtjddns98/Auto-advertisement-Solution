# Nutti 에이전트 팀 (PO 전용 가이드)

당신은 **기획자(PO)** 역할만 합니다. 지시만 내리면 역할별 에이전트 팀이 **완전 자동**으로
설계 → 리서치 → 개발 → 리뷰 → QA → 보고까지 수행합니다.

## 팀 구성 (`.claude/agents/`)

| 역할 | 에이전트 | 모델 | 하는 일 |
|------|----------|------|---------|
| 🧭 기획 보조 | `nutti-planner` | **opus** | PO 지시 → 스펙·workstream 분해(+복잡도 등급)·수용기준·브랜치/커밋 제안 |
| 🔬 리서치 | `nutti-researcher` | sonnet | 외부 API/SDK 조사 → 출처 포함 구현 노트 |
| 💻 개발 | `nutti-developer` | **sonnet**(자동 상향) | 스펙대로 구현 + 테스트. workstream별 병렬. planner가 `complex`로 등급한 workstream은 **자동 opus**(lite 시 억제), `opus` 옵션이면 전원 opus |
| 🔍 코드리뷰 | `nutti-reviewer` | sonnet(검증 haiku) | 적대적 리뷰(결함·보안·회귀·테스트) + haiku 반박검증 |
| ✅ QA | `nutti-qa` | sonnet | 테스트·수용기준 검증, 증거 기반 PASS/FAIL. **FAIL이면 수정→재QA 루프(최대 2회)** |
| 📝 보고서 | `nutti-reporter` | haiku | 한국어 작업 보고 + 다음 Todo |

## 쓰는 법

**아침 풀오토(권장)** — 지시 한 건이든 목록이든 그대로 주면 리드가 작업별로
빌드→검증→브랜치→커밋→PR→CI→머지→종합보고까지 **무인 루프**로 수행:
```
/nutti-day  오늘 할 일:
1. storage/sheets.py Google Sheets 실연동 (드라이런 유지)
2. telegram 알림에 영상 썸네일 첨부 lite
```
("아침 지시"·"풀오토"·"하루 돌려" 같은 말로도 트리거됨 — 상세 절차는 `.claude/skills/nutti-day/SKILL.md`)

**단건 팀 빌드** — 빌드까지만(이후 git/PR은 리드가 별도 처리):
```
Workflow({ name: 'nutti-team', args: '<원하는 기능/작업 한 줄>' })
```

파이프라인:
```
기획(planner·opus) → [리서치·필요시 병렬]
   → 공유파일 선처리(1명) → 개발 ×N 병렬 (workstream=기능 단위·파일 분리, complex→opus 자동)
   → 리뷰(reviewer) ↔ 수정 루프(최대 2회) → QA ↔ 수정 루프(최대 2회) → 보고(reporter)
```
> planner가 작업을 **파일이 겹치지 않는 workstream**으로 쪼개고 복잡도(simple/standard/complex)를
> 매깁니다. 공유 파일(models/config)은 먼저 1명이 선처리 → 충돌 방지.

결과로 `plan / research / builds / confirmed_findings / qa / report_markdown / git(브랜치·커밋 제안)`이
돌아옵니다. 그 뒤 **리드(메인 세션)가** 표준 워크플로대로 처리: ruff/pytest 최종 확인 → 브랜치 →
PR → **dev 자동 머지**(정책상 dev는 자율, main은 PO 승인) → 보고서를 Notion 일일 업무보고
DB에 기록(원하면). nutti-day는 이 리드 단계까지 통째로 자동화한 것.

## 개별 호출도 가능

특정 역할만 쓰고 싶으면 Agent 툴로 직접:
`Agent(subagent_type: 'nutti-researcher', prompt: 'Hedra Character-3 API 조사')` 등.

## 비용/모델 (economy 기본)
- **기획 = opus**(전체 설계 품질), 개발·수정 = **sonnet**(complex workstream만 자동 opus 상향),
  보고·반박검증 = **haiku**, 리뷰 차원·QA = sonnet.
- **적대적 리뷰 유지** (차원별 sonnet → 반박검증 haiku → 확정 결함 → 수정) — 싼 개발자가 놓친 걸 리뷰가 잡음.
- 수정(fix) 단계는 complex workstream이 하나라도 있었으면 opus(lite 시 억제), 아니면 sonnet.

### PO 옵션 (지시문에 단어만 넣으면 됨)
- **`opus`** / "오퍼스" → 그 작업의 개발자 **전원** opus 강제 (중요·복잡한 구현)
- **`lite`** / "라이트"·"간단히"·"빠르게" → 리서치·보고 생략 + complex 자동 상향 억제(리뷰는 유지), 최소 비용
  - 예: `Workflow({name:'nutti-team', args:'X 기능 구현 lite'})`

### 모델 라우팅 환경 주의 (프록시)
이 머신은 로컬 프록시(`ANTHROPIC_BASE_URL`) 경유라 워크플로우/에이전트의 `model`은 **티어 별칭**
(`opus`/`sonnet`/`haiku`)만 사용한다(현 환경에서 동작 실증됨). 만약 enforcer가 별칭을 거부하면
`settings.json`의 `env`에 `ANTHROPIC_DEFAULT_OPUS_MODEL`/`..._SONNET_MODEL`/`..._HAIKU_MODEL`을
프록시가 지원하는 실모델 ID로 지정하는 것이 해법. 세션 모델이 `[1m]` 접미사를 가지므로 에이전트
호출에 `model` 명시는 **필수**(생략 금지).

## 한계
- 실제 외부 API 호출 검증은 **키가 있어야** 가능 — 키 전엔 dry_run·테스트까지만 검증됩니다.
- 비개발(미팅·단가 등) 업무는 자동화 대상이 아닙니다.
