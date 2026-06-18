<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-04 | Updated: 2026-06-04 -->

# integrations

## Purpose
외부 서비스 연동 클라이언트. **모든 클라이언트는 `settings.dry_run`을 존중**한다 —
dry_run이면 네트워크/SDK 없이 결정적 더미 결과를 반환해 파이프라인을 테스트할 수 있다.
실제 연동부는 `# TODO`로 표시되며, 시그니처/반환 형태는 실연동 시 그대로 유지하도록 설계됐다.

## Key Files
| File | Description |
|------|-------------|
| `ai_text.py` | Claude 텍스트 생성: 대본(1단계)·메타데이터(3단계)·성과분석(5단계)·팩트체크. 구조화 출력은 Anthropic tool use, 시스템 프롬프트 prompt caching. 키 없으면 `claude -p` CLI 폴백 |
| `video.py` | `VideoStudio` 파사드: 시작 프레임(Kontext) → fal Veo 3.1 비트별 클립 → 앞뒤 침묵 트림 → ffmpeg 스티칭. `VeoPromptBuilder`가 대사를 따옴표 인용(네이티브 음성)·편별 스타일·마스코트 외형 고정. 백엔드 중립 헬퍼(HTTP·저장·redaction) 보유 |
| `video_veo_fal.py` | `FalVeoClient`: fal.ai Veo 3.1 image-to-video(제출→폴링→다운로드). 비트당 8초 클립 |
| `image_kontext.py` | `FalKontextClient`: fal.ai FLUX.1 Kontext [pro] 시작 프레임 생성(레퍼런스 이미지를 fal-storage 업로드 후 편집) |
| `_fal_common.py` | fal.ai 큐 REST 공통 헬퍼(상수·`_fal_headers`·`_validate_*`·SSRF 가드). image_kontext·video_veo_fal이 공유 |
| `publishing.py` | `Publisher`: YouTube Data API·Instagram Graph API 업로드 + 성과 조회. `FalMediaUploader`로 로컬 영상을 fal-storage에 업로드해 공개 URL(*.fal.media)을 만들어 Instagram `video_url`로 넘긴다(Meta가 직접 cURL). image_kontext와 동일한 fal-storage 흐름 |
| `telegram.py` | `TelegramClient`: Bot API 래퍼(sendMessage/getUpdates/answerCallback/editMessageText). `_call`이 오류를 분류 |

## For AI Agents

### Working In This Directory
- **dry_run 분기는 절대 네트워크/SDK를 요구하면 안 된다.** SDK(`anthropic`)는 실연동
  경로에서만 lazy import한다.
- 공개 메서드 시그니처를 바꾸지 말 것(오케스트레이터가 의존). 새 동작은 메서드 추가로.
- `ai_text`: Anthropic 응답 파싱은 방어적으로 — `_first_text()`(첫 text 블록),
  `_extract_tool_input()`(tool_use input dict). `content[0].text` 직접 인덱싱 금지.
- `video`/`video_veo_fal`/`image_kontext`: fal 인증은 `Authorization: Key <FAL_KEY>` 헤더 —
  **queue.fal.run 요청에만** 붙이고 CDN(fal.media) 다운로드엔 미첨부(키 유출 방지). 폴링 경계는
  `elapsed < timeout`(off-by-one 금지). 에러 메시지는 상태 코드/예외 타입명만 — URL·request id·
  응답 본문 노출 금지(redaction). 응답 영상 URL은 신뢰 불가 입력 → `_validate_fal_video_url`로
  scheme·host 검증(SSRF). 완료 영상은 즉시 다운로드해 로컬 저장. fal 공통 헬퍼는 `_fal_common.py`.
- `telegram._call`: 일시적 오류(네트워크/타임아웃/429/5xx)는 `TelegramTransientError`,
  영구 오류(그 외 4xx·`ok:false`)는 `TelegramError`로 구분한다. 봇 토큰은 에러 메시지에서
  `_scrub`으로 가린다(URL 경로에 토큰이 박히므로). 텔레그램은 논리 오류 시 HTTP 200 +
  `{"ok": false}`를 반환하므로 `ok` 필드를 반드시 검사.

### Testing Requirements
- HTTP 클라이언트(`http=`)나 SDK 클라이언트(`client._client`)를 주입해 네트워크 없이 테스트.
- 라이브 분기(파싱·폴백·오류 분류)는 fake 응답으로 반드시 커버.

### Common Patterns
- 클라이언트는 `__init__(settings, *, 주입가능_의존성)` 형태. dry_run 우선 분기.

## Dependencies

### Internal
- `nutti.config`(Settings), `nutti.models`(도메인 모델), `nutti.logging`

### External
- `anthropic`(ai_text, lazy), `httpx`(telegram/video/publishing)

<!-- MANUAL: -->
