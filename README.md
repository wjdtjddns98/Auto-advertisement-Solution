# 🐾 Nutti — AI 광고/콘텐츠 자동화 파이프라인

애견 수제간식 쇼핑몰을 위한 **대본 생성 → AI 영상 제작 → 업로드 → 성과 분석** 자동화 파이프라인.
[기획서(Notion)](https://www.notion.so/Nutti-AI-373a471660468062bd4cc4b991e8c525) 기반의 Python 구현 스캐폴딩.

> N8n 스케줄러가 `nutti run ...`(또는 파이썬 함수)을 호출하고, 실제 로직·검수·업로드는 이 패키지가 담당하는 구조입니다.

## 전체 플로우

```
대본 생성(Claude) ──▶ [검수① 텔레그램]
      │
      ▼
영상 제작(Hedra+Seedance+자막) ──▶ [검수② 텔레그램]
      │
      ▼
메타데이터 생성(제목·태그) ──▶ [검수③ 디스코드]
      │
      ▼
업로드(YouTube / Instagram)
      │
      ▼
성과 수집·분석(Claude) ──┐
      ▲                  │ 다음 대본 개선 피드백 루프
      └──────────────────┘
```

## 구조

```
nutti/
  config.py              # 환경설정 (.env, dry_run 플래그)
  models.py              # 도메인 모델 (Script, VideoAsset, Metadata, PipelineRun ...)
  logging.py             # 구조화 로깅
  integrations/
    ai_text.py           # Claude: 대본·메타데이터·성과분석
    video.py             # NanoBanana(시작 프레임) → Veo 3.1 단일컷 8초 영상
    publishing.py        # YouTube Data API · Instagram Graph API
  review/
    gates.py             # 검수 게이트 (텔레그램·디스코드·자동승인)
  storage/
    sheets.py            # Google Sheets 기록
  pipeline/
    orchestrator.py      # 5단계 + 검수 게이트 오케스트레이션
  cli.py                 # `nutti run` 진입점
tests/
  test_pipeline.py       # dry_run 전체 흐름 스모크 테스트
```

## 핵심: DRY-RUN 우선

`NUTTI_DRY_RUN=true`(기본값)이면 **외부 API 키 없이도 전 파이프라인이 시뮬레이션으로 동작**합니다.
구조를 먼저 검증한 뒤, 실제 API 연동(`integrations/*.py`, `review/gates.py`의 `# TODO`)을 하나씩 채우면 됩니다.

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
nutti run "강아지 수제간식 레시피" --reels
```

## 실제 연동 시 채울 곳 (TODO)

| 단계 | 파일 | 연동 대상 |
|------|------|-----------|
| 1·3·5 | `integrations/ai_text.py` | Anthropic API (dry_run 외 자동 동작) |
| 2 | `integrations/video.py` | Gemini(NanoBanana 시작 프레임) → Veo 3.1 image-to-video |
| 4·5 | `integrations/publishing.py` | YouTube Data API v3, Instagram Graph API |
| 검수 | `review/gates.py` | Telegram Bot(인라인 버튼), Discord Webhook |
| 저장 | `storage/sheets.py` | Google Sheets API |

## ⚠️ 운영 주의 (기획서 반영)

- **다계정 운영 금지(초기)** — 플랫폼 제재 위험. 1~2계정 파일럿 후 단계적 확장.
- **자동 댓글 링크 금지** — 스팸 감지. 간식계산기 링크는 영상 설명란/프로필에만 고정.
- **수의학 정보 팩트체크 필수** — 대본 생성 프롬프트에 사실 기반 조건 포함(`ai_text.py`의 system prompt).
- **영상 품질 검수 필수** — AI 영상 특성상 불안정 → 텔레그램 검수 게이트 통과 필수.
