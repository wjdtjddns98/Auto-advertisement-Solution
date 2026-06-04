<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-04 | Updated: 2026-06-04 -->

# nutti

## Purpose
Nutti 애플리케이션 패키지. 5단계 콘텐츠 자동화 파이프라인의 모든 로직을 담는다.
계층: 설정·모델·로깅(기반) → integrations(외부 연동) · review(검수 게이트) ·
storage(영속) → pipeline(오케스트레이션) → cli(진입점).

## Key Files
| File | Description |
|------|-------------|
| `config.py` | `Settings`(pydantic-settings). `.env`/환경변수 로드, `dry_run` 플래그, 모든 키 |
| `models.py` | 공통 도메인 모델: `Script`, `VideoAsset`, `Metadata`, `UploadResult`, `PerformanceReport`, `ReviewRequest`, `PipelineRun` + enum(`Stage`, `ReviewDecision`, `ContentFormat`) |
| `logging.py` | structlog 구조화 로깅 설정(`configure_logging`, `get_logger`) |
| `cli.py` | typer CLI. `nutti run <topic> [--reels] [--feedback]`, `nutti config` |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `integrations/` | 외부 서비스 클라이언트(Claude·영상·퍼블리싱·텔레그램) (see `integrations/AGENTS.md`) |
| `review/` | 사람-검수 게이트 추상화·구현 (see `review/AGENTS.md`) |
| `storage/` | 영속 계층(Sheets 기록·검수 상태) (see `storage/AGENTS.md`) |
| `pipeline/` | 5단계 오케스트레이션 (see `pipeline/AGENTS.md`) |

## For AI Agents

### Working In This Directory
- `config.Settings`는 alias로 환경변수를 매핑한다(예: `NUTTI_DRY_RUN`). 새 설정 추가 시
  `.env.example`에도 반드시 항목을 추가한다.
- `models.py`는 모든 계층이 공유한다. 필드 추가는 영향 범위가 넓으니 신중히, 직렬화
  (`model_dump(mode="json")`)/역직렬화 호환을 깨지 말 것.
- 시간값은 timezone-aware UTC(`_utcnow`) 사용 — `datetime.utcnow()`는 Python 3.12+ 폐기.

### Testing Requirements
- 설정/모델 변경은 `tests/test_pipeline.py` dry-run 스모크가 통과하는지 확인.

### Common Patterns
- enum은 `str, Enum` 혼합형(직렬화 안정). ID는 `uuid4().hex[:12]`.

## Dependencies

### Internal
- 하위 모든 모듈이 `config`·`models`·`logging`에 의존(기반 계층).

### External
- `pydantic`, `pydantic-settings`, `structlog`, `typer`

<!-- MANUAL: -->
