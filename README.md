# 🐾 Nutti — AI 광고/콘텐츠 자동화 파이프라인

애견 수제간식 쇼핑몰을 위한 **대본 생성 → AI 영상 제작 → 검수 → 업로드 → 성과 분석 → 피드백 루프** 자동화 파이프라인.
[기획서(Notion)](https://www.notion.so/Nutti-AI-373a471660468062bd4cc4b991e8c525) 기반의 Python 구현.

> 스케줄러(VPS의 Ofelia, [docs/DEPLOY.md](docs/DEPLOY.md)) 또는 수동 실행이 `nutti run ...`을 호출하고,
> 실제 로직·검수·업로드·분석은 이 패키지가 담당합니다.
> 단계 사이마다 **텔레그램 사람-검수 게이트**(단일 채널)가 있고, 매 사이클의 성과 분석이
> 다음 사이클 주제·대본으로 자동 연결됩니다.

## 전체 플로우

```
대본 생성(Claude, 비트별 분할) ──▶ [검수① 텔레그램 — 클립 경계·길이 확인, 그 자리서 수정(REVISE)]
      │
      ▼
영상 제작(비트별 멀티컷 → QC → ffmpeg 스티칭, 9:16 세로) ──▶ [검수② 텔레그램 — MP4 인라인 미리보기]
      │
      ▼
메타데이터 생성(제목·태그) ──▶ [검수③ 텔레그램]
      │
      ▼
업로드(YouTube 자동 · --reels 시 인스타용 영상+캡션을 텔레그램으로 수동 핸드오프)
      │
      ▼
비용 기록(원장) + 성과 수집 대기 큐 등록 ──┐
      ▲                                    │ 숙성(기본 48h) 후 다음 실행 시작 시 조회·분석
      └────────────────────────────────────┘ → 다음 대본 주제·개선 피드백으로 자동 반영
```

업로드 직후에는 YouTube Analytics 집계가 비어 있으므로, 성과 수집은 **다음 사이클 시작 시**
숙성된(기본 48시간 경과) 업로드만 조회해 피드백으로 저장합니다(`NUTTI_ANALYTICS_MIN_AGE_HOURS`).

## 영상 백엔드 (fal.ai 단일 결제처)

대본을 **비트(훅 → 핵심설명 → 마무리/CTA)** 단위로 나눠 비트별 독립 클립을 만든 뒤 ffmpeg로 이어
붙입니다(세로 9:16 쇼츠). 대사는 Veo 네이티브 한국어 음성으로 내보내고, Veo가 임의로 박는 깨진
자막은 프롬프트 + 비전 QC(`NUTTI_QC_TEXT_ENABLED`)로 차단합니다. 통제된 한글 자막은 기본으로
하단에 굽고(`NUTTI_CAPTION_BURN=true` 기본), 훅 첫 문장은 상단 텍스트 오버레이로 굽습니다
(`NUTTI_HOOK_OVERLAY=true` 기본 — 무음 시청 대응).

| 단계 | 소스 | 특징 |
|------|------|------|
| 시작 프레임 | **fal.ai FLUX.1 Kontext [pro]** — 마스코트 레퍼런스 이미지를 편집 | 편별 의상·장소·포맷 로테이션을 유지한 채 캐릭터 일관성 확보 |
| 영상 | **fal.ai Veo 3.1**(기본 Lite) | 기본은 **끝프레임 고정 모드**(`NUTTI_VEO_FAL_ENDFRAME_LOCK=true`) — first-last-frame-to-video(`NUTTI_VEO_FAL_FLF_MODEL`)로 모든 클립의 시작·끝 포즈를 고정해 비트 경계 끊김을 제거. 비트당 8초, 무음 트림 후 유사도 스티칭 |
| 클립 QC | ffmpeg 분석 + Claude 비전 | 중간 프리즈·블랙·무발화·꼬리 미수렴·화면 내 깨진 자막 검출 시 그 비트만 자동 재생성(`NUTTI_QC_*`) |

> 과거의 `veo`(Gemini API)·`kling`(무음+TTS) 백엔드는 제거됐고 `veo_fal` 단일입니다.
> `FAL_KEY` 하나로 프레임·영상을 모두 처리합니다. 끝프레임 고정을 끄면
> (`NUTTI_VEO_FAL_ENDFRAME_LOCK=false`) image-to-video(`NUTTI_VEO_FAL_MODEL`) + 프레임 체이닝 경로를 씁니다.

## 구조

```
nutti/
  config.py                    # 환경설정 (.env, dry_run, 키 유효성 판정 _usable_key)
  models.py                    # 도메인 모델 (Script[비트], VideoAsset, Metadata, PipelineRun ...)
  logging.py                   # 구조화 로깅
  integrations/
    ai_text.py                 # Claude: 대본(비트 분할)·메타데이터·팩트체크·성과분석
    video.py                   # VideoStudio: 프롬프트 빌더·편별 스타일·QC·자막/훅 굽기·스티칭(파사드)
    video_veo_fal.py           # FalVeoClient: fal.ai Veo 3.1 비트별 클립 생성
    image_kontext.py           # FalKontextClient: fal.ai FLUX.1 Kontext 시작 프레임 생성
    _fal_common.py             # fal.ai 큐 REST 공통 헬퍼(검증·헤더·SSRF 가드)
    telegram.py                # 텔레그램 Bot: 인라인 버튼·MP4 미리보기·대본 수정 입력
    publishing.py              # YouTube 업로드 + YouTube/Instagram 성과 조회
  review/
    gates.py                   # 검수 게이트 (텔레그램·자동승인)
  storage/
    __init__.py                # atomic_write_json 공용 헬퍼
    sheets.py                  # Google Sheets 기록
    reviews.py                 # 검수 요청/결정 영속 저장
    state_store.py             # 사이클 간 상태(직전 성과 피드백·최근 주제·성과 대기 큐)
  pipeline/
    orchestrator.py            # 대본→검수→영상→검수→메타→검수→업로드→비용→분석 오케스트레이션
    cost.py                    # 편당 제작 비용 사전 추정(estimate_run_cost)
    cost_ledger.py             # 실행별 비용 누적 원장(`nutti cost`의 데이터 소스)
  cli.py                       # `nutti run` / `nutti cost` / `nutti config` 진입점
tests/                         # dry_run·fake 주입 기반 단위 테스트 (네트워크 불필요)
```

## 핵심: DRY-RUN 우선

`NUTTI_DRY_RUN=true`(기본값)이면 **외부 API 키 없이도 전 파이프라인이 네트워크 없이 재현 가능한
시뮬레이션으로 동작**합니다. 모든 외부 연동(`integrations`, `review`, `storage`)은 dry_run 분기에서
네트워크/SDK 없이 더미 결과를 반환합니다. 실제 연동을 추가할 때도 이 계약을 유지합니다.

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
nutti run "..." --feedback "..."  # 개선 포인트 수동 주입(생략 시 직전 분석 자동 사용)
nutti run "강아지 수제간식 레시피" --reels   # 인스타용 영상+캡션 텔레그램 핸드오프 포함
nutti cost --days 7              # 오늘·이번 달·전체(+최근 7일) 누적 제작 비용 조회
```

라이브 실행(`NUTTI_DRY_RUN=false`)에는 `NUTTI_MASCOT_IMAGE`(예: `assets/mascot.png`)가 필수입니다 —
Kontext 프레임 생성이 마스코트 레퍼런스 이미지를 요구합니다. 자막 폰트(잘난체)는 라이선스상
저장소에 커밋되지 않으므로 로컬에 별도 배치합니다(없으면 맑은고딕→Noto 폴백).

## 실제 연동에 필요한 키

| 단계 | 파일 | 연동 대상 | 필요 키 |
|------|------|-----------|---------|
| 1·3·5 | `integrations/ai_text.py` | Anthropic API (대본·메타·팩트체크·분석). 키 없으면 `claude -p` CLI 폴백 | `ANTHROPIC_API_KEY` |
| 2 (프레임·영상) | `integrations/image_kontext.py`·`video_veo_fal.py` | fal.ai FLUX.1 Kontext(프레임) + Veo 3.1(영상) | `FAL_KEY` |
| 4 | `integrations/publishing.py` | YouTube Data API v3 (자동 업로드) | `YOUTUBE_*` |
| 5 | `integrations/publishing.py` | YouTube Analytics · Instagram Graph API 인사이트(성과 조회) | `YOUTUBE_*`, `INSTAGRAM_*` |
| 검수 | `integrations/telegram.py`·`review/gates.py` | Telegram Bot(인라인 버튼·수동 핸드오프) | `TELEGRAM_BOT_TOKEN`/`_CHAT_ID` |
| 저장 | `storage/sheets.py` | Google Sheets API | `GOOGLE_SHEETS_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON` |

> Instagram Reels 자동 게시는 없습니다 — `--reels`는 최종 영상과 캡션을 텔레그램으로 보내
> 사람이 직접 올립니다(2026-06-18 PO 결정). Instagram 키는 성과 조회에만 쓰입니다.
> 비트별 클립 스티칭·무음 트림·자막 굽기에는 **ffmpeg**(imageio-ffmpeg 번들)가 쓰입니다 — 시스템 설치 불요.

## ⚠️ 운영 주의 (기획서 반영)

- **다계정 운영 금지(초기)** — 플랫폼 제재 위험. 1~2계정 파일럿 후 단계적 확장.
- **자동 댓글 링크 금지** — 스팸 감지. 간식계산기 링크는 영상 설명란/프로필에만 고정.
- **수의학 정보 팩트체크 필수** — 대본 생성 후 팩트체크 실패 시 자동 재생성, 한도 초과 시 중단.
- **영상 품질 검수 필수** — AI 영상 특성상 불안정 → 클립 QC 레이어 + 텔레그램 검수 게이트(MP4 미리보기) 통과 필수.

## 개발 규칙

- **브랜칭**: GitHub Flow + dev. `main`(보호) ← `dev`(통합) ← `feat/*`·`fix/*`·`docs/*`.
- **커밋 전 검증**: `ruff check .` 와 `pytest -q` 둘 다 green 필수.
- **CI**: push/PR(main·dev)마다 `.github/workflows/ci.yml`이 ruff + pytest(3.11/3.12/3.13) 실행.
- 상세 규칙은 루트 `CLAUDE.md`·`AGENTS.md` 참고.
