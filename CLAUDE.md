# Nutti 프로젝트 — 작업 규칙 (세션마다 자동 적용)

> PO(정성운/제니스)가 한 줄 지시 → 역할별 에이전트 팀이 설계→개발→리뷰→QA→보고까지 자율 수행.
> **substantive 작업은 기본이 팀 위임**이다. 매번 "팀으로 해줘"라 말할 필요 없다.

## 0. 기본 동작
- **아침 일괄 지시**(작업 목록·"오늘 할 일"·"풀오토"·"하루 돌려"·`/nutti-day`) →
  `.claude/skills/nutti-day/SKILL.md` 루프 수행: 작업별 [nutti-team 빌드→검증→브랜치→커밋→PR→CI→머지]를
  무인 반복 후 종합 보고. 중간 질문으로 멈추지 말 것.
- **실질 개발 단건**(기능 추가·멀티파일·리팩터·디버깅·연동) → `Workflow({ name: 'nutti-team', args: '<한 줄>' })`로 위임. 인라인 금지.
- **사소한 작업**(git status/commit/push, 한 줄 수정, 단순 질문, 설정 확인) → 직접 처리, 팀 안 띄움.
- 범위 애매하면 한 번만 확인, 합리적이면 추정하고 진행. (nutti-day 중엔 묻지 말고 추정+보고)
- PO 옵션: `opus`(그 작업 개발자 전원 상향) · `lite`/"간단히"·"빠르게"(리서치·보고 생략+자동 상향 억제, 리뷰 유지).
  planner가 complex로 등급한 workstream은 기본에서도 자동 opus 상향됨.

## 1. 안전 가드레일 (반드시 준수)
- **main/dev 직접 `git push` 절대 금지** — 브랜치 보호 ACTIVE라 거부됨. 항상 `feat/*`·`fix/*`·`docs/*` 브랜치 + PR(`gh pr create --base dev`) + green CI 경유.
- **dev 머지**: 리뷰어 지적 수정 → **같은 리뷰어 재승인 후에만** 자동 머지(재승인 없이 금지). 자기 PR은 `--approve` 불가 → `gh pr review --comment`.
- **main 머지(dev→main 릴리스)**: 항상 **PO 명시 승인 후에만.** 임의 머지 금지.
- **커밋 전 검증**: `./.venv/Scripts/python.exe -m ruff check .` + `-m pytest -q` 둘 다 green 필수. Conventional commit(`feat(scope):`…) + `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` 트레일러.
- **리뷰 전략**: 기본 단일 `nutti-reviewer` 1회. 보안민감(인증·시크릿·신뢰불가 입력)·대규모 변경만 `Workflow({name:'nutti-review'})` full(비쌈). 확정 `high`/`critical` 결함은 수정 후 재리뷰 필수, `medium`/`low`만 남으면 PR comment 기록 후 머지.

## 2. 핵심 계약 — DRY-RUN 우선
`NUTTI_DRY_RUN=true`(기본)면 외부 키 없이 전 파이프라인이 결정적 시뮬레이션으로 동작. 모든 외부 연동(`nutti/integrations`·`review`·`storage`)은 dry_run 분기에서 네트워크/SDK 없이 더미 반환 — 실연동 추가 시에도 유지. 단위 테스트는 네트워크 없이 통과. 도메인 모델=Pydantic v2(`nutti/models.py`), 주석·docstring 한국어, ruff line-length=100.

## 3. 모델 라우팅 (economy 기본)
opus=깊은 추론·아키텍처·복잡 구현 · sonnet=표준 구현·리뷰·테스트 · haiku=대량·기계적(검색·반박검증·fetch). fan-out 고개수 단계는 haiku/sonnet, opus는 저개수 깊은 단계만.

## 4. 한계
- 실제 외부 API 호출 검증은 키가 있어야 가능 — 키 전엔 dry_run·테스트까지.
- 비개발(미팅·단가 협상 등) 업무는 자동화 대상 아님.

> 상세 — 팀 역할표·파이프라인·PO 옵션: `.claude/TEAM.md` · 모듈 구조·아키텍처: 루트 및 하위 `AGENTS.md`(디렉터리 작업 전 가장 가까운 것 먼저 읽기) · 기획서: Notion "🐾 Nutti" · 레포: github.com/wjdtjddns98/Auto-advertisement-Solution (gh: `/c/Program Files/GitHub CLI/gh.exe`).
