# Nutti 파이프라인 — 프로덕션 이미지
# 베이스: python:3.12-slim (경량, glibc 포함)
# 빌드: docker build -t nutti:latest .
# 실행: docker compose up -d

FROM python:3.12-slim

# ── 시스템 패키지 ──────────────────────────────────────────────────────────────
# cryptography / grpcio (google-auth 의존) 빌드에 필요한 C 라이브러리 + claude CLI
# 설치용 curl. 대본/팩트체크는 ANTHROPIC_API_KEY 없을 때 `claude -p`(Claude Code) 폴백을
# 타므로(옵션 B: Max 구독 재사용), 이미지에 claude CLI를 포함한다.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        libffi-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── claude CLI (Claude Code) 설치 ─────────────────────────────────────────────
# npm 글로벌 설치로 /usr/bin/claude(전 사용자 PATH)에 확실히 올린다. Node는 NodeSource
# 20.x. 인증은 런타임에 CLAUDE_CONFIG_DIR로 마운트한 credentials를 사용(이미지에 토큰 미포함).
# 끝에 `claude --version`으로 설치를 build-time 검증(실패 시 빌드 중단).
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/* \
    && claude --version

# ── 환경변수 ──────────────────────────────────────────────────────────────────
ENV PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul \
    # dry_run 기본값 true — 이미지 자체가 키를 포함하지 않도록 보호
    NUTTI_DRY_RUN=true

# ── 작업 디렉터리 ─────────────────────────────────────────────────────────────
WORKDIR /app

# ── 소스 복사 (.dockerignore 적용 후 남는 파일만) ────────────────────────────
COPY . .

# ── 패키지 설치 (dev extras 제외, --no-cache-dir 로 이미지 크기 절감) ──────────
# pyproject.toml + 전체 소스를 한 레이어에서 설치한다.
# hatchling 선행 레이어를 분리해도 pip install . 이 COPY . . 뒤에 오므로
# 소스 한 줄만 바꿔도 의존성이 재설치된다 — 캐시 분리 효과가 없어 단일 흐름으로 정리.
RUN pip install --no-cache-dir .

# ── non-root 사용자 (보안) ────────────────────────────────────────────────────
# uid=1000 / gid=1000 — 대부분의 Linux VPS 기본 사용자 UID와 일치
RUN groupadd --gid 1000 nutti \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /sbin/nologin nutti \
    # data/ 디렉터리는 볼륨 마운트 전 소유권 설정
    && mkdir -p /app/data/media \
    && chown -R nutti:nutti /app/data

USER nutti

# ── 헬스체크 ──────────────────────────────────────────────────────────────────
# `nutti config` 는 네트워크 호출 없이 현재 설정을 출력하므로 기동 확인에 적합
HEALTHCHECK --interval=60s --timeout=10s --start-period=5s --retries=3 \
    CMD nutti config || exit 1

# ── 진입점 ────────────────────────────────────────────────────────────────────
# ENTRYPOINT ["nutti"] + CMD ["config"] → 기본 실행 시 nutti config 출력
# docker compose run --rm nutti run "주제" 처럼 CMD를 오버라이드해서 사용
ENTRYPOINT ["nutti"]
CMD ["config"]
