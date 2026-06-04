<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-04 | Updated: 2026-06-04 -->

# review

## Purpose
사람-검수 게이트. 파이프라인은 `ReviewGate` 인터페이스(`request(review) -> ReviewDecision`)
에만 의존하고, 실제 채널(텔레그램/디스코드)이나 자동 승인(테스트)은 구현체를 갈아끼운다.
검수①·②는 텔레그램, 검수③은 디스코드를 사용한다.

## Key Files
| File | Description |
|------|-------------|
| `gates.py` | `ReviewGate`(Protocol), `AutoApproveGate`(dry_run/테스트), `TelegramGate`(인라인 버튼+롱폴), `DiscordGate`, `_decision_from_callback`, `_callback_origin_chat` |

## For AI Agents

### Working In This Directory
- **설계 결정**: `request()`는 **동기** 시그니처를 유지한다. '비동기'는 사람이 비동기로
  버튼을 탭하는 것을 뜻하며, 파이프라인은 승인 전까지 블로킹돼야 한다(영상 비용 지출 전 검수 필수).
  `TelegramGate`는 내부에서 `getUpdates`를 **롱폴링**하며 콜백을 기다린다.
- **인가(보안)**: 콜백은 `message.chat.id == settings.telegram_chat_id`일 때만 인정한다
  (`_is_authorized`). `from.id`는 봇이 속한 다른 채팅에서도 일치할 수 있어 인증 기준으로 쓰면
  우회가 생기므로 **사용 금지**. inline-mode 콜백은 `message`가 null이라 `_callback_origin_chat`이
  `or {}`로 방어(크래시 방지) — 미인가로 처리한다.
- **폴링 복원력**: 롱폴 루프는 `TelegramTransientError`(일시적)만 잡아 재시도하고, 영구 오류
  (`TelegramError`, 예: 잘못된 토큰)는 전파해 빠르게 실패한다. 전체 대기는 `review_timeout_sec`이 제한.
- **상태 영속**: 검수 요청은 `storage`의 `ReviewStore`에 저장해 재시작에도 살아남는다.
- 결정은 UI 호출(answer/edit) 전에 먼저 store에 영속화(분실 방지). UI 갱신은 best-effort.

### Testing Requirements
- `client`(텔레그램)·`store`·`clock`·`sleep`을 주입해 네트워크·실시간 없이 테스트.
- 인가 우회·널 메시지·타임아웃·알수없는 콜백값·영구오류 전파 케이스를 반드시 커버.

### Common Patterns
- 콜백 데이터 형식: `nutti:{review_id}:{decision}`. 알 수 없는 값은 보수적으로 REJECTED.

## Dependencies

### Internal
- `nutti.config`, `nutti.models`(ReviewRequest/ReviewDecision),
  `nutti.integrations.telegram`(TelegramClient/오류 타입), `nutti.storage.reviews`(ReviewStore)

### External
- (간접) `httpx` via TelegramClient

<!-- MANUAL: -->
