<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-04 | Updated: 2026-06-04 -->

# storage

## Purpose
영속 계층. 대본/실행 기록과 검수 상태를 저장한다. 자격증명이 없거나 dry_run이면
인메모리/로컬 폴백으로 동작해 테스트·초기 개발이 가능하다.

## Key Files
| File | Description |
|------|-------------|
| `sheets.py` | `SheetStore`: 대본·실행 기록(기획서의 'Sheets 저장'). 실연동 gspread append 구현(자격증명 없으면 인메모리 폴백) |
| `reviews.py` | `ReviewStore`(Protocol) + `InMemoryReviewStore`(테스트) + `JsonFileReviewStore`(영속). 검수 요청 상태 저장 |

## For AI Agents

### Working In This Directory
- `JsonFileReviewStore`는 **원자적 쓰기**(임시파일 → `os.replace`)로 중간 크래시 시 손상을
  방지한다. 직접 `write_text`로 덮어쓰지 말 것.
- 로드 시 손상 파일·잘못된 행은 예외를 던지지 말고 **경고 로그 후 스킵**한다(대기 중 검수 분실 방지).
- `ReviewRequest` 직렬화는 `model_dump(mode="json")`, 역직렬화는 `ReviewRequest(**data)`.
- store 경로는 `settings.review_store_path`(기본 `data/reviews.json`).

### Testing Requirements
- `tmp_path`로 JSON 라운드트립·손상복원·bad-row 스킵·임시파일 잔존 없음을 검증.
- dry_run/자격증명 없음 → 인메모리 폴백 경로 확인.
- fake gspread 클라이언트 주입으로 append 검증(네트워크 없이 원격 경로 진입).

### Common Patterns
- 저장소는 추상 인터페이스(Protocol) + 인메모리/영속 구현 쌍. 파이프라인은 인터페이스에만 의존.

## Dependencies

### Internal
- `nutti.config`, `nutti.models`(Script/PipelineRun/ReviewRequest), `nutti.logging`

### External
- 표준 라이브러리(`json`, `pathlib`, `os`) + `gspread`(실연동, lazy import).

<!-- MANUAL: -->
