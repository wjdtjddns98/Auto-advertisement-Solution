<!-- Generated: 2026-06-04 | Updated: 2026-06-04 -->

# Nutti — AI 광고/콘텐츠 자동화 파이프라인

## Purpose
애견 수제간식 쇼핑몰을 위한 **대본 생성 → AI 영상 제작 → 업로드 → 성과 분석**
자동화 파이프라인. 5단계 흐름 사이에 텔레그램/디스코드 기반 사람-검수 게이트가 있다.
N8n 스케줄러가 `nutti run`(또는 파이썬 함수)을 호출하고, 실제 로직·검수·업로드는
이 패키지가 담당한다. (기획서: Notion "🐾 Nutti | AI 광고 자동화 계획서")

## 전체 플로우
```
대본 생성(Claude) → [팩트체크] → [검수① 텔레그램]
  → 영상 제작(Hedra+Seedance+자막) → [검수② 텔레그램]
  → 메타데이터(제목·태그) → [검수③ 디스코드]
  → 업로드(YouTube/Instagram) → 성과 수집·분석(Claude) → 다음 대본 개선(피드백 루프)
```

## Key Files
| File | Description |
|------|-------------|
| `pyproject.toml` | 패키지/의존성/스크립트(`nutti`)·ruff·pytest 설정 |
| `requirements.txt` | 런타임 의존성 평면 목록 |
| `.env.example` | 모든 외부 서비스 키·설정 템플릿 (실제 `.env`는 gitignore) |
| `README.md` | 사람용 개요·시작 가이드 |
| `demo_local.py` | (선택) dry-run 로컬 시연 스크립트 |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `nutti/` | 애플리케이션 패키지 (see `nutti/AGENTS.md`) |
| `tests/` | pytest 테스트 (see `tests/AGENTS.md`) |
| `.github/workflows/` | GitHub Actions CI (ruff + pytest) |

## For AI Agents

### 핵심 계약 — DRY-RUN 우선
`NUTTI_DRY_RUN=true`(기본값)이면 **외부 API 키 없이 전 파이프라인이 결정적
시뮬레이션으로 동작**한다. 모든 외부 연동(`nutti/integrations`, `nutti/review`,
`nutti/storage`)은 dry_run 분기에서 네트워크/SDK 없이 더미 결과를 반환해야 한다.
실제 연동을 추가할 때도 이 계약을 깨지 말 것.

### 개발 워크플로 (이 저장소의 표준 — 반드시 준수)
- **브랜칭**: GitHub Flow + dev. `main`(프로덕션, 보호됨) ← `dev`(통합) ← `feat/*`·`fix/*`·`docs/*`.
- **커밋 전 검증**: `./.venv/Scripts/python.exe -m ruff check .` 와 `-m pytest -q` 둘 다 green 필수.
- **CI**: push/PR(main·dev)마다 `.github/workflows/ci.yml`이 ruff+pytest(3.11/3.12/3.13) 실행.
- **PR**: `gh pr create --base dev`. **dev 머지는 자동**(단 리뷰어 재검증/approve 후에만). **main 머지는 PO 명시 승인 후에만.** 상세 정책은 루트 `CLAUDE.md` 참고.
- **완료 전**: 멀티에이전트 적대적 코드리뷰 루프(리뷰 차원 → 반박검증 → 수정 → clean까지) 권장.
- **에이전트 모델 라우팅**: 깊은 추론=opus, 표준=sonnet, 대량·기계적(검색·반박검증)=haiku.

### Testing Requirements
- 모든 단위 테스트는 dry_run 또는 fake 주입으로 **네트워크 없이** 돌아야 한다.
- 새 코드 경로(특히 라이브 분기)는 fake 클라이언트 주입으로 테스트한다.

### Common Patterns
- 도메인 모델은 Pydantic v2(`nutti/models.py`)에 정의, 플랫폼별 응답을 공통 모델로 정규화.
- 주석·docstring은 한국어 사용(기존 스타일 유지). ruff line-length=100.
- 외부 클라이언트는 `settings`와 주입 가능한 의존성(http/client/store/clock/sleep)을 받아 테스트 가능하게.

## Dependencies

### External
- `anthropic` — Claude(대본·메타데이터·팩트체크·분석)
- `pydantic` / `pydantic-settings` — 모델·환경설정
- `httpx` — HTTP 클라이언트 / `tenacity` — 재시도 / `structlog` — 로깅
- `APScheduler` — 스케줄링 / `typer` — CLI

<!-- MANUAL: 수동 메모는 이 줄 아래에 추가하면 재생성 시 보존됩니다 -->
