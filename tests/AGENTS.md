<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-04 | Updated: 2026-06-04 -->

# tests

## Purpose
pytest 테스트. **전부 네트워크/키 없이** 동작한다 — dry_run 설정 또는 fake 의존성 주입.
라이브(비-dry_run) 코드 경로도 fake 클라이언트/응답으로 커버한다.

## Key Files
| File | Description |
|------|-------------|
| `test_pipeline.py` | 오케스트레이터 dry-run 스모크 + 팩트체크 배선(호출/재생성/거절/경계)·검수 거절 중단 |
| `test_ai_text.py` | AITextClient: dry-run 동작 + 라이브 헬퍼(`_first_text`/`_extract_tool_input`)·팩트체크 파싱실패·메타데이터 폴백·링크 비중복 |
| `test_review_gate.py` | TelegramGate: 콜백→결정, 인가/우회, 널 메시지, 타임아웃, 영구오류 전파, 폴링 백오프, JSON store 손상복원·원자적 쓰기 |
| `test_telegram_client.py` | TelegramClient: `ok:false`·토큰 스크럽(RequestError/HTTPStatusError)·429 transient |

## For AI Agents

### Working In This Directory
- 새 기능/수정마다 테스트를 추가한다. 특히 **라이브 분기**는 fake 주입으로 반드시 커버.
- 텔레그램 테스트는 `FakeTelegramClient`/`InMemoryReviewStore`/가짜 `clock`·`sleep`을 주입.
  콜백 update에는 `message.chat.id`를 인가된 chat_id로 넣어야 인가를 통과한다.
- AI 라이브 경로는 `_FakeAnthropic`(messages.create가 `_Msg`/`_Block` 반환)을 `client._client`에 주입.
- 단언은 "호출됐다"가 아니라 "올바른 인자/결과"까지 검증(예: 재생성 피드백에 issues 포함).

### Testing Requirements
- 실행: `./.venv/Scripts/python.exe -m pytest -q` (전부 green이어야 커밋).
- 결정적이어야 함 — `Date.now`/랜덤/실시간 sleep 의존 금지(주입으로 제어).

### Common Patterns
- fake 클라이언트는 스크립트된 응답 큐를 순서대로 반환. 상태(offsets/answered/edited)를 기록해 단언.

## Dependencies

### Internal
- `nutti` 패키지 전체.

### External
- `pytest`, `pytest-asyncio`(dev), `httpx`(일부 fake 구성)

<!-- MANUAL: -->
