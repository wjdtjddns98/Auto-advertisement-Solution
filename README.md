# 🐾 Nutti — AI 광고/콘텐츠 자동화 파이프라인

애견 수제간식 쇼핑몰을 위한 **대본 생성 → AI 영상 제작 → 검수 → 업로드 → 성과 분석 → 피드백 루프** 자동화 파이프라인.
[기획서(Notion)](https://www.notion.so/Nutti-AI-373a471660468062bd4cc4b991e8c525) 기반의 Python 구현.

> N8n 스케줄러(또는 수동)가 `nutti run ...`을 호출하고, 실제 로직·검수·업로드·분석은 이 패키지가 담당합니다.
> 단계 사이마다 **텔레그램/디스코드 사람-검수 게이트**가 있고, 매 사이클의 성과 분석이 다음 사이클 주제·대본으로 자동 연결됩니다.

## 전체 플로우

```
대본 생성(Claude, 비트별 분할) ──▶ [검수① 텔레그램 — 클립 경계·길이 확인, 그 자리서 수정(REVISE)]
      │
      ▼
영상 제작(비트별 멀티컷 → ffmpeg 스티칭, 9:16 세로) ──▶ [검수② 텔레그램 — MP4 인라인 미리보기]
      │
      ▼
메타데이터 생성(제목·태그) ──▶ [검수③ 디스코드]
      │
      ▼
업로드(YouTube / Instagram Reels)
      │
      ▼
성과 수집·분석(Claude) ──┐
      ▲                  │ 분석 결과를 상태에 저장 → 다음 대본 주제·개선 피드백으로 자동 반영
      └──────────────────┘
```

## 영상 백엔드 (`NUTTI_VIDEO_BACKEND`)

대본을 **비트(훅 → 핵심설명 → 마무리/CTA)** 단위로 나눠 비트별 독립 클립을 만든 뒤 ffmpeg로 이어 붙입니다(세로 9:16 쇼츠). 화면 자막은 `negativePrompt`로 억제하고 대사는 음성으로만 내보냅니다. 백엔드는 두 가지를 지원합니다.

| 백엔드 | 음성 | 영상 소스 | 비용/특징 |
|--------|------|-----------|-----------|
| **`veo`** (기본) | Veo 네이티브 한국어 음성(별도 TTS 불요) | Gemini 이미지(시작 프레임) → **Veo 3.1** image-to-video | 비트당 8초 클립. 산출물 48h 후 삭제되므로 완료 즉시 로컬 저장 |
| **`kling`** | **무음 영상 + Gemini TTS** 한국어 내레이션을 ffmpeg로 mux(립싱크 포기·보이스오버) | Gemini 이미지 → **fal.ai Kling** v2.1 standard image-to-video(무음) | 클립 5/10초·음성 길이에 맞춰 `-shortest`로 자름. Veo보다 저렴 |

> Kling은 네이티브 한국어 음성을 못 냅니다(v3는 영어로 자동번역, v1.6/2.1은 무음). 그래서 무음 클립을 만들고 한국어 TTS를 별도 합성해 입히는 보이스오버 포맷을 씁니다.

## 구조

```
nutti/
  config.py                    # 환경설정 (.env, dry_run, 영상 백엔드 선택)
  models.py                    # 도메인 모델 (Script[비트], VideoAsset, Metadata, PipelineRun ...)
  logging.py                   # 구조화 로깅
  integrations/
    ai_text.py                 # Claude: 대본(비트 분할)·메타데이터·팩트체크·성과분석
    video.py                   # VideoStudio: Gemini 프레임 → Veo 3.1 비트별 클립 + ffmpeg 스티칭
    video_kling.py             # Kling 무음 클립 + Gemini TTS 한국어 보이스오버 백엔드
    telegram.py                # 텔레그램 Bot: 인라인 버튼·MP4 미리보기·대본 수정 입력
    publishing.py              # YouTube Data API · Instagram Graph API
  review/
    gates.py                   # 검수 게이트 (텔레그램·디스코드·자동승인)
  storage/
    sheets.py                  # Google Sheets 기록
    reviews.py                 # 검수 요청/결정 영속 저장
    state_store.py             # 사이클 간 상태(직전 성과 피드백·최근 주제) 영속 → 피드백 루프
  pipeline/
    orchestrator.py            # 대본→검수→영상→검수→메타→검수→업로드→분석 오케스트레이션
  cli.py                       # `nutti run` / `nutti config` 진입점
tests/                         # dry_run·fake 주입 기반 단위 테스트 (네트워크 불필요)
```

## 핵심: DRY-RUN 우선

`NUTTI_DRY_RUN=true`(기본값)이면 **외부 API 키 없이도 전 파이프라인이 결정적 시뮬레이션으로 동작**합니다.
모든 외부 연동(`integrations`, `review`, `storage`)은 dry_run 분기에서 네트워크/SDK 없이 더미 결과를 반환합니다. 실제 연동을 추가할 때도 이 계약을 유지합니다.

## 배포(VPS) → [docs/DEPLOY.md](docs/DEPLOY.md) 참고

## 시작하기

```bash
# 1) 가상환경 + 설치
python -m venv .venv
.venv\Scripts\activate           # Windows (PowerShell: .venv\Scripts\Activate.ps1)
pip install -e ".[dev]"

# 2) 환경설정
copy .env.example .env           # 이후 키 채우기 (dry_run에서는 비워둬도 동작)

# 3) 테스트 (키 불필요)
pytest

# 4) 한 사이클 실행 (dry_run)
nutti config
nutti run "강아지 닭가슴살 간식, 하루 적정량은?"
nutti run                        # 주제 생략 → 직전 성과·최근 주제 반영해 자동 생성
nutti run "강아지 수제간식 레시피" --reels
```

영상 백엔드를 바꾸려면 `.env`에 `NUTTI_VIDEO_BACKEND=kling`(기본 `veo`)을 설정합니다.

## 실제 연동에 필요한 키

| 단계 | 파일 | 연동 대상 | 필요 키 |
|------|------|-----------|---------|
| 1·3·5 | `integrations/ai_text.py` | Anthropic API (대본·메타·팩트체크·분석) | `ANTHROPIC_API_KEY` |
| 2 (veo) | `integrations/video.py` | Gemini 이미지 + Veo 3.1 image-to-video | `GEMINI_API_KEY` |
| 2 (kling) | `integrations/video_kling.py` | Gemini 이미지·TTS + fal.ai Kling | `GEMINI_API_KEY`, `FAL_KEY` |
| 4·5 | `integrations/publishing.py` | YouTube Data API v3, Instagram Graph API | `YOUTUBE_*`, `INSTAGRAM_*` |
| 검수 | `integrations/telegram.py`·`review/gates.py` | Telegram Bot(인라인 버튼), Discord Webhook | `TELEGRAM_BOT_TOKEN`/`_CHAT_ID`, `DISCORD_WEBHOOK_URL` |
| 저장 | `storage/sheets.py` | Google Sheets API | `GOOGLE_SHEETS_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON` |

> `kling` 백엔드의 무음 영상 생성과 mux에는 **ffmpeg**가 시스템에 설치돼 있어야 합니다.

## ⚠️ 운영 주의 (기획서 반영)

- **다계정 운영 금지(초기)** — 플랫폼 제재 위험. 1~2계정 파일럿 후 단계적 확장.
- **자동 댓글 링크 금지** — 스팸 감지. 간식계산기 링크는 영상 설명란/프로필에만 고정.
- **수의학 정보 팩트체크 필수** — 대본 생성 프롬프트에 사실 기반 조건 포함(`ai_text.py`의 system prompt).
- **영상 품질 검수 필수** — AI 영상 특성상 불안정 → 텔레그램 검수 게이트(MP4 미리보기) 통과 필수.

## 개발 규칙

- **브랜칭**: GitHub Flow + dev. `main`(보호) ← `dev`(통합) ← `feat/*`·`fix/*`·`docs/*`.
- **커밋 전 검증**: `ruff check .` 와 `pytest -q` 둘 다 green 필수.
- **CI**: push/PR(main·dev)마다 `.github/workflows/ci.yml`이 ruff + pytest(3.11/3.12/3.13) 실행.
- 상세 규칙은 루트 `CLAUDE.md`·`AGENTS.md` 참고.
