# Nutti 프로젝트 — 작업 규칙 (세션마다 자동 적용)

> 이 파일은 세션 시작 시 자동 로드된다. **사용자(정성운/제니스)는 기획자(PO) 역할만 한다.**
> PO가 한 줄 지시를 내리면, 역할별 에이전트 팀이 설계→개발→리뷰→QA→보고까지 자율 수행한다.
> 매번 "팀으로 해줘"라고 말할 필요 없다 — **substantive 작업은 기본이 팀 위임이다.**

## 0. 기본 동작 (이게 디폴트)

- **실질적 개발/구현 작업** (기능 추가, 멀티파일 수정, 리팩터, 디버깅, 연동 구현 등)
  → 무조건 **`nutti-team` 워크플로로 위임**한다. 혼자 인라인으로 짜지 않는다.
  ```
  Workflow({ name: 'nutti-team', args: '<원하는 작업 한 줄>' })
  ```
- **사소한 작업**(git status/commit/push, 단일 파일 한 줄 수정, 단순 질문, 설정 확인)
  → 위임 없이 **직접** 처리. 팀 띄우지 않는다.
- 범위가 애매하면 먼저 한 번만 확인하고, 합리적 추정이 가능하면 추정하고 진행한다.

## 1. 에이전트 팀 (`.claude/agents/`)

| 역할 | 에이전트 | 모델 | 하는 일 |
|------|----------|------|---------|
| 🧭 기획 보조 | `nutti-planner` | sonnet | PO 지시 → 스펙·작업분해·수용기준·리서치 주제 |
| 🔬 리서치 | `nutti-researcher` | sonnet | 외부 API/SDK 조사 → 출처 포함 노트 |
| 💻 개발 | `nutti-developer` | sonnet | 스펙대로 구현+테스트. workstream(기능)별 병렬. **opus 옵트인** |
| 🔍 코드리뷰 | `nutti-reviewer` | sonnet | 적대적 리뷰(결함·보안·회귀·테스트) |
| ✅ QA | `nutti-qa` | sonnet | 테스트·수용기준 검증, 증거 기반 PASS/FAIL |
| 📝 보고서 | `nutti-reporter` | haiku | 한국어 작업 보고 + 다음 Todo |

파이프라인: `기획 → [리서치] → 공유파일 선처리 → 개발 ×N 병렬 → 리뷰↔수정(≤2) → QA → 보고`

**PO 옵션** (지시문에 단어만 넣으면 됨):
- `opus` / "오퍼스" → 그 작업 개발자를 opus로 상향 (중요·복잡한 구현)
- `lite` / "라이트"·"간단히"·"빠르게" → 리서치·보고 생략(리뷰는 유지), 최소 비용

개별 역할만 쓰려면 Agent 툴 직접 호출: `Agent(subagent_type: 'nutti-researcher', ...)` 등.
적대적 리뷰만 따로: `Workflow({ name: 'nutti-review' })`.

## 2. 모델 라우팅 (토큰 절감 — economy 기본)

- **opus**: 적게, 깊게 — 아키텍처/플래닝/복잡 구현/깊은 디버깅/최종 합성.
- **sonnet**: 표준 — executor 구현, 차원별 리뷰, 테스트 작성.
- **haiku**: 많고 기계적 — 검색(Explore), 반박검증 yes/no, 문서 fetch, 단순 검증.
- fan-out 워크플로의 고개수 단계(파인더·반박검증·1건당 1에이전트)는 haiku/sonnet, opus는 저개수 깊은 단계만.

## 3. 표준 개발 워크플로 (반드시 준수)

1. **브랜칭 — GitHub Flow + dev**: `main`(프로덕션, 보호됨) ← `dev`(통합) ← `feat/*`·`fix/*`·`docs/*`.
   Conventional commit (`feat(scope):`…) + `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` 트레일러.
2. **커밋 전 검증**: `./.venv/Scripts/python.exe -m ruff check .` 와 `-m pytest -q` 둘 다 green 필수.
3. **CI**: push/PR(main·dev)마다 `.github/workflows/ci.yml`이 ruff+pytest(3.11/3.12/3.13) 실행 — green 유지.
4. **PR — `gh`로**: `gh pr create --base dev --head feat/...`.
   - `gh` 경로: `/c/Program Files/GitHub CLI/gh.exe`. 레포: github.com/wjdtjddns98/Auto-advertisement-Solution.
5. **머지 정책**:
   - **dev로의 머지는 자동** — 단 적대적 리뷰어가 지적한 사항을 **고친 뒤 같은 리뷰어에게 재검증(approve) 받고 나서만** 머지. 수정만 하고 재승인 없이 머지 금지. CI fail/changes-needed면 고치고 재시도. 자기 PR은 `--approve` 불가(브랜치 보호 0 approvals라 머지는 됨) → `gh pr review --comment`로 코멘트.
   - **main으로의 머지(dev→main 릴리스)는 항상 PO 명시 승인 후에만.** 절대 임의 머지 금지.
6. **완료 전 적대적 리뷰 루프** — 규모에 따라 자동 선택, 매번 동일 전략 쓰지 않는다:

   | 작업 규모 | 리뷰 전략 | 방법 |
   |-----------|-----------|------|
   | 신규 기능·아키텍처 변경·security-sensitive | `nutti-review` full (4차원 sonnet + 반박 haiku) | `Workflow({name:'nutti-review'})` |
   | 버그픽스·1-3파일 수정 | 단일 `nutti-reviewer` 에이전트 1회 | `Agent({subagent_type:'nutti-reviewer',...})` |
   | docstring·주석·설정값·테스트 문자열만 수정 | 리뷰 생략 | 직접 커밋 |

   **재리뷰 기준**: 확정 결함 중 `high`/`critical`이 있으면 수정 후 재리뷰 필수.
   `medium`/`low`만 남으면 수정 후 PR comment에 기록하고 재리뷰 없이 머지.
7. **브랜치 보호 ACTIVE** (main·dev): PR 필수, CI 체크 통과 필수, force-push/삭제 금지.
   → **절대 `git push`로 main/dev 직접 푸시 금지** (거부됨). 항상 브랜치+PR+green CI 경유.

## 4. Nutti 핵심 계약 — DRY-RUN 우선

- `NUTTI_DRY_RUN=true`(기본)면 외부 키 없이 전 파이프라인이 결정적 시뮬레이션으로 동작.
  모든 외부 연동(`nutti/integrations`, `nutti/review`, `nutti/storage`)은 dry_run 분기에서 네트워크/SDK 없이 더미 반환. 실제 연동 추가 시에도 이 계약 유지.
- 단위 테스트는 dry_run/fake 주입으로 **네트워크 없이** 통과해야 한다.
- 도메인 모델 = Pydantic v2(`nutti/models.py`). 주석·docstring 한국어. ruff line-length=100.
- 디렉터리 작업 전 가장 가까운 `AGENTS.md`(계층형 deepinit 문서)를 먼저 읽는다.

## 5. 한계
- 실제 외부 API 호출 검증은 **키가 있어야** 가능 — 키 전엔 dry_run·테스트까지만.
- 비개발(미팅·단가 협상 등) 업무는 자동화 대상 아님.

> 상세 팀 가이드: `.claude/TEAM.md` · 모듈 문서: 루트 및 하위 `AGENTS.md` · 기획서: Notion "🐾 Nutti".
