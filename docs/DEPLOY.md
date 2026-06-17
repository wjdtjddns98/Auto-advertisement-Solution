# Nutti — VPS Docker 배포 가이드

PC 없이 24시간 자동 운영을 위한 Docker 기반 배포 절차.
Ubuntu 22.04+ VPS 기준으로 작성되었다.

---

## 목차

1. [VPS 준비](#1-vps-준비)
2. [Docker 설치](#2-docker-설치)
3. [레포 클론 + .env 구성](#3-레포-클론--env-구성)
4. [secrets/sa.json 마운트 절차](#4-secretssajson-마운트-절차)
5. [빌드·기동](#5-빌드기동)
6. [스케줄 확인](#6-스케줄-확인)
7. [로그 확인](#7-로그-확인)
8. [수동 실행](#8-수동-실행)
9. [dry_run 스모크 테스트](#9-dry_run-스모크-테스트)
10. [업데이트](#10-업데이트)
11. [대본 생성 경로 선택](#11-대본-생성-경로-선택)
12. [CI docker build 잡 추가 참고](#12-ci-docker-build-잡-추가-참고)

---

## 1. VPS 준비

| 항목 | 최소 사양 |
|------|-----------|
| OS | Ubuntu 22.04 LTS 이상 |
| RAM | 1 GB 이상 (권장 2 GB) |
| 디스크 | 10 GB 이상 (영상 파일 보관 시 더 필요) |
| 아웃바운드 포트 | 443, 80 (HTTPS/HTTP) |
| 인바운드 포트 | **불필요** — Telegram 봇은 폴링(아웃바운드) 방식이므로 서버 포트 개방이 필요 없다 |

> **팁**: DigitalOcean Droplet 기본 플랜($6/월, 1vCPU·1GB RAM)으로 충분히 동작한다.

---

## 2. Docker 설치

```bash
# Docker 공식 설치 스크립트 (Ubuntu)
curl -fsSL https://get.docker.com | sh

# 현재 사용자를 docker 그룹에 추가 (재로그인 필요)
sudo usermod -aG docker $USER
newgrp docker

# Docker Compose v2 포함 여부 확인
docker compose version
```

설치 후 `docker --version` 과 `docker compose version` 이 정상 출력되면 준비 완료.

---

## 3. 레포 클론 + .env 구성

```bash
# 레포 클론
git clone https://github.com/wjdtjddns98/Auto-advertisement-Solution.git nutti
cd nutti

# .env 파일 생성 (.env.example 복사 후 편집)
cp .env.example .env
nano .env   # 또는 vi .env
```

### .env 필수 키 목록

```dotenv
# ── Telegram (검수 게이트) ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id

# ── AI 대본 생성 (11번 섹션에서 옵션 A / B 선택) ────────────────────────────
# 옵션 A: ANTHROPIC_API_KEY 직접 사용 (서버 환경 권장)
ANTHROPIC_API_KEY=sk-ant-...
# 옵션 B: claude CLI 방식 → 11번 섹션 참고 (아래 줄 주석 처리)
# ANTHROPIC_API_KEY=  ← 비워두고 volumes에 ~/.claude 마운트

# ── fal.ai (영상·시작 프레임 생성) ───────────────────────────────────────────
# FAL_KEY 하나로 시작 프레임(FLUX.1 Kontext)·영상(Veo 3.1, veo_fal) 모두 처리한다.
FAL_KEY=your_fal_key

# ── Google Sheets ────────────────────────────────────────────────────────────
GOOGLE_SHEETS_ID=your_sheet_id
GOOGLE_SERVICE_ACCOUNT_JSON=/app/secrets/sa.json

# ── Discord (메타데이터 검수) ─────────────────────────────────────────────────
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# ── YouTube ──────────────────────────────────────────────────────────────────
YOUTUBE_CLIENT_ID=your_youtube_client_id
YOUTUBE_CLIENT_SECRET=your_youtube_client_secret
YOUTUBE_REFRESH_TOKEN=your_youtube_refresh_token

# ── Instagram ────────────────────────────────────────────────────────────────
INSTAGRAM_ACCESS_TOKEN=your_instagram_access_token
INSTAGRAM_ACCOUNT_ID=your_instagram_account_id

# ── 파이프라인 동작 설정 ──────────────────────────────────────────────────────
NUTTI_DRY_RUN=false                             # 실 운영 시 false
NUTTI_STATE_PATH=/app/data/pipeline_state.json  # 볼륨 마운트 경로와 일치
NUTTI_REVIEW_STORE_PATH=/app/data/reviews.json  # 볼륨 마운트 경로와 일치
NUTTI_MEDIA_DIR=/app/data/media                 # 영상 파일 저장 경로

# ── 마스코트 이미지 (실 운영 시 설정) ─────────────────────────────────────────
NUTTI_MASCOT_IMAGE=/app/assets/mascot.png       # 컨테이너 내 경로 그대로 사용
```

> `.env` 파일은 절대 git commit 하지 않는다 — `.gitignore`에 이미 포함되어 있다.

---

## 4. secrets/sa.json 마운트 절차

Google Sheets 기록 기능에 필요한 서비스 계정 키 파일을 설정한다.

```bash
# secrets 디렉터리 생성 (git에서 제외됨)
mkdir -p secrets

# Google Cloud Console에서 다운로드한 sa.json을 복사
cp ~/downloads/your-sa-file.json secrets/sa.json

# 퍼미션 보호 (선택 사항)
chmod 600 secrets/sa.json
```

`docker-compose.yml`의 볼륨 설정(`./secrets:/app/secrets:ro`)에 의해 컨테이너 내부
`/app/secrets/sa.json`으로 읽기 전용 마운트된다. `docker-compose.yml`의 `environment`
블록이 `GOOGLE_SERVICE_ACCOUNT_JSON=/app/secrets/sa.json`(절대경로)으로 오버라이드하므로,
`.env`의 값과 무관하게 컨테이너 내부에서는 항상 절대경로로 동작한다.

> **주의**: `secrets/` 디렉터리는 `.gitignore`와 `.dockerignore` 양쪽에 제외 설정이
> 되어 있어 이미지·커밋에 포함되지 않는다.

---

## 5. 빌드·기동

```bash
# ① 데이터 디렉터리 사전 생성 — nutti 유저(uid=1000)가 쓸 수 있도록 소유권 설정
#    (이 단계를 생략하면 Docker가 root 소유로 디렉터리를 생성하여 첫 실행 시 PermissionError 발생)
mkdir -p data/media secrets
sudo chown -R 1000:1000 data

# ② 이미지 빌드 + 전체 서비스 백그라운드 기동
docker compose up --build -d

# 기동 상태 확인
docker compose ps
```

예상 출력:

```
NAME                  IMAGE              STATUS
nutti-nutti-1         nutti:latest       running
nutti-scheduler-1     mcuadros/ofelia    running
```

`nutti`(파이프라인 워커)·`scheduler`(Ofelia cron 스케줄러)가
모두 `running` 상태이면 정상이다.

---

## 6. 스케줄 확인

```bash
# Ofelia 로그에서 cron 등록 확인
docker compose logs scheduler
```

정상 기동 시 아래와 같은 로그가 출력된다:

```
scheduler  | [scheduler] Starting ...
scheduler  | [scheduler] Listening for new jobs
scheduler  | [job-exec run-pipeline] Schedule: 0 0 * * *
```

`Schedule: 0 0 * * *`(UTC 00:00 = KST 09:00)가 보이면 스케줄 등록 완료.

---

## 7. 로그 확인

```bash
# nutti 파이프라인 실시간 로그
docker compose logs -f nutti

# Ofelia 스케줄러 로그
docker compose logs -f scheduler

# 전체 서비스 로그
docker compose logs -f
```

---

## 8. 수동 실행

스케줄 없이 즉시 파이프라인을 1회 실행하려면 **`runner` 서비스**를 사용한다.

`runner`는 Dockerfile의 `ENTRYPOINT ["nutti"]` 를 그대로 사용하는 one-shot 서비스다.
`nutti` 서비스는 Ofelia job-exec 대기용으로 entrypoint가 오버라이드되어 있어
`docker compose run --rm nutti run "주제"` 는 exit 127 로 실패한다.

```bash
# 실제 주제로 수동 실행 (실 API 호출)
docker compose --profile manual run --rm runner run "강아지 닭가슴살 간식, 하루 적정량은?"

# Reels 형식으로 수동 실행
docker compose --profile manual run --rm runner run "강아지 수제간식 레시피" --reels

# config 확인
docker compose --profile manual run --rm runner config
```

`--rm` 플래그로 실행 완료 후 임시 컨테이너가 자동 삭제된다.
`runner` 서비스는 `profiles: [manual]` 로 선언되어 `docker compose up -d` 에는 포함되지 않는다.

> **주의**: `--profile manual` 플래그는 반드시 명시한다. Docker Compose **v2.20+** 는
> `compose run` 시 대상 서비스의 프로파일을 자동 활성화하므로 생략해도 동작하지만,
> 그 미만 버전(Ubuntu 22.04 LTS의 apt 기본 패키지 등)에서는 생략 시
> `no such service: runner` 오류가 발생한다. 모든 버전에서 동작하도록 항상 붙여 쓴다.

---

## 9. dry_run 스모크 테스트

실제 API 키 없이 전체 파이프라인 흐름을 검증한다.
네트워크 호출이 발생하지 않으며 결정적(deterministic) 결과를 반환한다.

```bash
# dry_run 모드로 전체 사이클 실행
docker compose --profile manual run --rm -e NUTTI_DRY_RUN=true runner run "도커 스모크"
```

`완료: run=...` 형식의 로그와 함께 `exit 0`으로 종료되면 이미지가 정상이다.

---

## 10. 업데이트

새 버전이 릴리스되면:

```bash
# 최신 코드 가져오기
git pull

# 이미지 재빌드 + 서비스 재시작 (다운타임 최소화)
docker compose up --build -d
```

구버전 이미지 정리(선택):

```bash
docker image prune -f
```

---

## 11. 대본 생성 경로 선택

Nutti는 Claude AI를 통해 대본을 생성한다. 컨테이너 환경에서 인증 방식은
두 가지 옵션이 있다.

---

### 옵션 A — ANTHROPIC_API_KEY 환경변수 (서버 환경 권장)

가장 단순하고 안정적인 방법이다. `.env`에 API 키를 직접 설정한다.

```dotenv
ANTHROPIC_API_KEY=sk-ant-api03-...
```

이 환경변수가 있으면 `nutti`는 Anthropic Python SDK를 통해 직접 API를 호출한다.
`claude` CLI 설치가 불필요하며, 재인증 없이 안정적으로 동작한다.

**인증 우선순위**: `ANTHROPIC_API_KEY` > `CLAUDE_CODE_OAUTH_TOKEN` > `~/.claude/.credentials.json`

---

### 옵션 B — claude CLI + ~/.claude 볼륨 마운트

Claude Code CLI(`claude`)가 설치된 개발 머신의 인증 상태를 컨테이너에 재사용하는 방법이다.
API 키 없이 Claude Max 구독으로 `claude -p`를 headless 실행하는 경우에 해당한다.

**동작 원리** (공식 문서 기반):
- 로컬에서 `claude login` 후 생성된 `~/.claude/.credentials.json`(Linux) 또는
  macOS Keychain 토큰을 컨테이너에 볼륨 마운트해 재사용한다.
- `CLAUDE_CONFIG_DIR` 환경변수로 컨테이너 내 credentials 경로를 재지정할 수 있다.
- `subprocess`로 `claude -p <프롬프트>` 를 호출 시 해당 디렉터리를 읽는다.

**docker-compose.yml에서 볼륨 추가**:

```yaml
services:
  nutti:
    # ... 기존 설정 ...
    volumes:
      - ./data:/app/data
      - ./secrets:/app/secrets:ro
      - ~/.claude:/home/nutti/.claude:ro    # 추가: 호스트 인증 파일 마운트
    environment:
      CLAUDE_CONFIG_DIR: /home/nutti/.claude   # credentials 경로 명시
      # ANTHROPIC_API_KEY는 비워두거나 제거
```

**제약 및 미확인 사항**:
- OAuth 토큰 만료 후 자동 갱신 여부: **미확인** — 만료 시 VPS에서 `claude login`
  재실행 후 credentials 파일을 갱신해야 할 가능성이 높다.
- Windows 호스트 → Linux 컨테이너 bind mount 시 경로 변환 동작: **미확인**
  (Dev Containers 레이어 의존).
- macOS Keychain 사용 시 컨테이너에서 credentials 읽기 불가 — Linux VPS에서는
  파일 기반이므로 정상 동작한다.

**결론**: VPS 장기 운영에는 **옵션 A(ANTHROPIC_API_KEY)**가 안정적이다.
옵션 B는 API 키 없이 Claude Max 구독을 활용할 때 사용하되,
토큰 만료 대비 재인증 절차를 운영 계획에 포함해야 한다.

---

## 12. CI docker build 잡 추가 참고

> **참고 메모** — 아래는 GitHub Actions에 Docker 빌드 검증을 추가할 때의 예시이다.
> 실제 CI 파일(`.github/workflows/ci.yml`) 수정은 별도 PR로 진행 권장.

```yaml
# .github/workflows/ci.yml 에 추가할 job 예시
docker-build:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - name: Docker 이미지 빌드 검증
      run: docker build -t nutti:ci .
    - name: dry_run 스모크 테스트
      run: |
        docker run --rm \
          -e NUTTI_DRY_RUN=true \
          -e NUTTI_STATE_PATH=/tmp/state.json \
          -e NUTTI_REVIEW_STORE_PATH=/tmp/reviews.json \
          nutti:ci run "CI 스모크"
```

> **주의**: `docker compose run --rm` 방식의 스모크 테스트는 `.env` 파일 존재를
> 전제로 하므로, CI 환경에서는 `-e` 플래그로 환경변수를 직접 주입하거나
> GitHub Actions Secrets에서 `env_file`을 동적 생성해야 한다.
