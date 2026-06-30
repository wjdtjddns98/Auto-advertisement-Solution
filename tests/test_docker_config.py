"""Docker 배포 설정 검증 테스트.

Dockerfile, .dockerignore, docker-compose.yml 파일의 보안·구조·스케줄 설정을
네트워크/Docker 데몬 없이 파일 내용 파싱으로 검증한다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# 프로젝트 루트 (tests/ 의 부모)
ROOT = Path(__file__).parent.parent


# ── Dockerfile 검증 ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    """Dockerfile 내용을 읽어 반환한다."""
    path = ROOT / "Dockerfile"
    assert path.exists(), "Dockerfile 이 프로젝트 루트에 없음"
    return path.read_text(encoding="utf-8")


def test_dockerfile_base_image(dockerfile_text: str) -> None:
    """python:3.12-slim 베이스 이미지를 사용해야 한다."""
    assert "FROM python:3.12-slim" in dockerfile_text


def test_dockerfile_no_secret_copy(dockerfile_text: str) -> None:
    """.env 또는 secrets/ 를 이미지에 COPY하는 라인이 없어야 한다."""
    for line in dockerfile_text.splitlines():
        stripped = line.strip()
        # 주석 라인 제외
        if stripped.startswith("#"):
            continue
        assert "COPY .env" not in stripped, f"Dockerfile에 .env COPY 발견: {stripped!r}"
        assert "COPY secrets" not in stripped, f"Dockerfile에 secrets COPY 발견: {stripped!r}"


def test_dockerfile_non_root_user(dockerfile_text: str) -> None:
    """non-root USER(nutti, uid=1000)로 전환해야 한다."""
    assert "USER nutti" in dockerfile_text
    assert "uid=1000" in dockerfile_text or "--uid 1000" in dockerfile_text


def test_dockerfile_entrypoint_nutti(dockerfile_text: str) -> None:
    """ENTRYPOINT 가 nutti CLI 를 가리켜야 한다."""
    assert 'ENTRYPOINT ["nutti"]' in dockerfile_text


def test_dockerfile_claude_cli_installed(dockerfile_text: str) -> None:
    """이미지에 claude CLI(claude -p 폴백용)가 설치돼야 한다(옵션 B, 2026-06-30).

    실모드 대본 생성은 ANTHROPIC_API_KEY 없을 때 `claude -p`로 폴백하므로 이미지에 claude
    CLI가 없으면 침묵 실패한다(무인 검증서 실측). 설치 라인이 통째로 제거되는 회귀를 잡는다.
    """
    non_comment = "\n".join(
        line for line in dockerfile_text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "@anthropic-ai/claude-code" in non_comment, "claude CLI 설치 라인 없음"
    assert "claude --version" in non_comment, "claude CLI build-time 검증 없음"


def test_dockerfile_cmd_config(dockerfile_text: str) -> None:
    """기본 CMD 가 config 서브커맨드여야 한다."""
    assert 'CMD ["config"]' in dockerfile_text


def test_dockerfile_timezone_utf8(dockerfile_text: str) -> None:
    """TZ=Asia/Seoul 과 PYTHONIOENCODING=utf-8 이 설정되어야 한다."""
    assert "TZ=Asia/Seoul" in dockerfile_text
    assert "PYTHONIOENCODING=utf-8" in dockerfile_text


def test_dockerfile_workdir_app(dockerfile_text: str) -> None:
    """WORKDIR 이 /app 이어야 한다."""
    assert "WORKDIR /app" in dockerfile_text


def test_dockerfile_pip_no_dev(dockerfile_text: str) -> None:
    """pip install 시 dev extras 를 포함하지 않아야 한다.

    `.[dev]` 또는 `-e ".[dev]"` 형태가 없어야 하며,
    `pip install .` (dev 제외) 만 있어야 한다.
    """
    # dev extras 설치 패턴 검색
    dev_pattern = re.compile(r"pip install[^#\n]*\[dev\]")
    matches = dev_pattern.findall(dockerfile_text)
    assert not matches, f"Dockerfile에 dev extras 설치 라인 발견: {matches}"


def test_dockerfile_no_flock_package(dockerfile_text: str) -> None:
    """Dockerfile apt-get install 에 존재하지 않는 'flock' 패키지가 없어야 한다.

    flock(1) 바이너리는 util-linux 에 포함되어 python:3.12-slim 에 이미 존재한다.
    별도 'flock' 패키지는 Debian/Ubuntu 저장소에 없으므로 빌드가 실패한다.
    """
    # 주석이 아닌 라인 중 apt-get install 문맥에서 flock 단독 패키지 검사
    non_comment_lines = [
        ln for ln in dockerfile_text.splitlines() if not ln.strip().startswith("#")
    ]
    non_comment_text = "\n".join(non_comment_lines)
    install_pattern = re.compile(r"apt-get install[^\n]*\bflock\b")
    matches = install_pattern.findall(non_comment_text)
    assert not matches, (
        f"Dockerfile apt-get install 에 존재하지 않는 'flock' 패키지 발견: {matches} — "
        "util-linux 에 포함된 flock 명령은 별도 설치 불필요"
    )


def test_dockerfile_user_after_pip_install(dockerfile_text: str) -> None:
    """USER nutti 전환이 마지막 pip install 보다 뒤에 와야 한다.

    pip install 이 root 권한으로 실행되어야 /usr/local/lib 에 패키지가 설치된다.
    USER 전환이 앞에 오면 pip 가 권한 오류로 실패한다.
    """
    lines = dockerfile_text.splitlines()
    last_pip_index = -1
    user_nutti_index = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"pip install", stripped):
            last_pip_index = i
        if re.match(r"USER\s+nutti", stripped):
            user_nutti_index = i
    assert user_nutti_index != -1, "USER nutti 라인이 Dockerfile 에 없음"
    assert last_pip_index != -1, "pip install 라인이 Dockerfile 에 없음"
    assert user_nutti_index > last_pip_index, (
        f"USER nutti(라인 {user_nutti_index + 1})가 마지막 pip install(라인 {last_pip_index + 1})보다 "
        "앞에 있음 — pip 가 권한 오류로 실패함"
    )


def test_dockerfile_dry_run_default_true(dockerfile_text: str) -> None:
    """Dockerfile 에 NUTTI_DRY_RUN=true 안전 기본값 ENV 가 있어야 한다.

    이 ENV 가 없으면 키 미설정 상태의 컨테이너가 실제 외부 API 를 호출하여
    예상치 못한 과금이나 오류가 발생할 수 있다. 안전 계약의 핵심 불변식이다.
    """
    assert "NUTTI_DRY_RUN=true" in dockerfile_text, (
        "Dockerfile ENV NUTTI_DRY_RUN=true 가 없으면 이미지 기본값이 live 모드가 되어 "
        "키 없는 콜드 스타트에서 실제 API 호출이 발생함"
    )


# ── .dockerignore 검증 ───────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def dockerignore_text() -> str:
    """.dockerignore 내용을 읽어 반환한다."""
    path = ROOT / ".dockerignore"
    assert path.exists(), ".dockerignore 가 프로젝트 루트에 없음"
    return path.read_text(encoding="utf-8")


def test_dockerignore_excludes_env(dockerignore_text: str) -> None:
    """.env 파일이 제외 목록에 있어야 한다."""
    assert ".env" in dockerignore_text


def test_dockerignore_excludes_secrets(dockerignore_text: str) -> None:
    """secrets/ 디렉터리가 제외 목록에 있어야 한다."""
    assert "secrets/" in dockerignore_text


def test_dockerignore_excludes_venv(dockerignore_text: str) -> None:
    """.venv/ 가 제외 목록에 있어야 한다."""
    assert ".venv/" in dockerignore_text


def test_dockerignore_excludes_git(dockerignore_text: str) -> None:
    """.git/ 이 제외 목록에 있어야 한다."""
    assert ".git/" in dockerignore_text


def test_dockerignore_includes_assets(dockerignore_text: str) -> None:
    """assets/ 디렉터리(mascot.png 포함)가 명시적으로 포함되어야 한다.

    !assets/ 또는 ! assets/mascot.png 패턴으로 재포함해야 한다.
    """
    lines = dockerignore_text.splitlines()
    include_lines = [ln.strip() for ln in lines if ln.strip().startswith("!")]
    # !assets/ 또는 !assets/mascot.png 중 하나가 있어야 함
    assets_included = any("assets" in ln for ln in include_lines)
    assert assets_included, f"!assets 재포함 규칙 없음. include 라인: {include_lines}"


def test_dockerignore_excludes_pycache(dockerignore_text: str) -> None:
    """__pycache__/ 가 제외 목록에 있어야 한다."""
    assert "__pycache__/" in dockerignore_text


# ── docker-compose.yml 검증 ──────────────────────────────────────────────────


@pytest.fixture(scope="module")
def compose_text() -> str:
    """docker-compose.yml 내용을 읽어 반환한다."""
    path = ROOT / "docker-compose.yml"
    assert path.exists(), "docker-compose.yml 이 프로젝트 루트에 없음"
    return path.read_text(encoding="utf-8")


def _non_comment_lines(text: str) -> list[str]:
    """주석(#으로 시작하는) 라인을 제외한 라인 목록을 반환한다."""
    return [ln for ln in text.splitlines() if not ln.strip().startswith("#")]


def test_compose_top_level_name(compose_text: str) -> None:
    """docker-compose.yml 에 top-level name: nutti 가 있어야 한다.

    name: 필드가 없으면 Docker Compose v2 가 작업 디렉터리명에서 프로젝트 이름을 유도한다.
    디렉터리에 한글·공백이 포함된 경우(예: '광고 자동화 솔루션') 프로젝트 이름이 빈 문자열이
    되어 'project name must not be empty' 오류로 모든 compose 명령이 실패한다.
    """
    non_comment = "\n".join(_non_comment_lines(compose_text))
    assert re.search(r"^name\s*:\s*nutti\s*$", non_comment, re.MULTILINE), (
        "docker-compose.yml 에 top-level 'name: nutti' 가 없음 — "
        "한글/공백 디렉터리에서 모든 docker compose 명령이 실패함"
    )


def test_compose_has_nutti_service(compose_text: str) -> None:
    """nutti 서비스가 정의되어야 한다."""
    assert "nutti:" in compose_text


def test_compose_has_scheduler_service(compose_text: str) -> None:
    """scheduler(Ofelia) 서비스가 정의되어야 한다."""
    assert "scheduler:" in compose_text


def test_compose_ofelia_image(compose_text: str) -> None:
    """scheduler 서비스가 mcuadros/ofelia 이미지를 사용해야 한다."""
    assert "mcuadros/ofelia" in compose_text


def test_compose_ofelia_docker_sock(compose_text: str) -> None:
    """Ofelia 가 Docker socket 을 마운트해야 한다."""
    assert "/var/run/docker.sock" in compose_text


def test_compose_ofelia_daemon_docker(compose_text: str) -> None:
    """Ofelia 커맨드가 daemon --docker 여야 한다."""
    assert "daemon --docker" in compose_text


def test_compose_ofelia_enabled_label(compose_text: str) -> None:
    """nutti 서비스에 ofelia.enabled: "true" 레이블이 비주석 라인에 정확히 있어야 한다.

    단순 "true" 문자열은 compose 파일 내 다른 위치(no-overlap 등)에도 존재하므로
    ofelia.enabled 키와 결합된 패턴으로 검증한다.
    ofelia.enabled: "false" 로 바뀌면 Ofelia가 해당 컨테이너를 스캔에서 제외해
    모든 스케줄 실행이 중단된다.
    """
    non_comment = "\n".join(_non_comment_lines(compose_text))
    assert re.search(r'ofelia\.enabled\s*:\s*"true"', non_comment), (
        'ofelia.enabled: "true" 레이블이 비주석 라인에 없음 — '
        "Ofelia가 컨테이너를 스캔하지 않아 모든 스케줄 실행이 중단됨"
    )


def test_compose_ofelia_schedule_utc_midnight(compose_text: str) -> None:
    """스케줄이 UTC 00:00:00 (KST 09:00) 이어야 한다.

    Ofelia(robfig/cron)는 6필드 "초 분 시 일 월 요일"로 해석한다 — 5필드 "0 0 * * *"는
    매일 09시가 아니라 엉뚱하게 동작한다(2026-06-30 무인 검증 실측). 반드시 6필드
    "0 0 0 * * *"가 schedule 레이블에 결합되어 있어야 한다(주석 아닌 라인).
    """
    non_comment = "\n".join(_non_comment_lines(compose_text))
    assert re.search(
        r'ofelia\.job-exec\.run-pipeline\.schedule\s*:\s*"0 0 0 \* \* \*"', non_comment
    ), 'schedule 레이블이 6필드 "0 0 0 * * *"(UTC 00:00:00 = KST 09:00)가 아님'


def test_compose_ofelia_no_overlap(compose_text: str) -> None:
    """no-overlap 레이블 값이 "true" 여야 한다.

    키 존재만 검사하면 "false" 로 바뀌어도 통과하므로 값까지 결합해 검증한다.
    no-overlap 이 꺼지면 장기 실행이 겹쳐 파이프라인 중복 실행이 발생한다.
    """
    non_comment = "\n".join(_non_comment_lines(compose_text))
    assert re.search(
        r'ofelia\.job-exec\.run-pipeline\.no-overlap\s*:\s*"true"', non_comment
    ), 'no-overlap 레이블이 비주석 라인에 "true" 값으로 없음'


def test_compose_absolute_paths_in_env(compose_text: str) -> None:
    """환경변수에 /app/data 절대경로가 명시되어야 한다(상대경로 위험 방지)."""
    assert "/app/data/pipeline_state.json" in compose_text
    assert "/app/data/reviews.json" in compose_text
    assert "/app/data/media" in compose_text


def test_compose_google_sa_json_absolute_path(compose_text: str) -> None:
    """environment 블록에 GOOGLE_SERVICE_ACCOUNT_JSON 절대경로가 명시되어야 한다.

    상대경로(secrets/sa.json)는 컨테이너 CWD 가 /app 이 아닐 때(docker compose run --workdir
    등) SheetStore._build_client 의 Path.exists() 검사가 False 를 반환하여 문자열을 inline
    JSON 으로 간주하고 JSONDecodeError 를 발생시킨다. /app/secrets/sa.json 절대경로로
    명시하면 CWD 와 무관하게 항상 올바른 파일을 읽는다.
    """
    non_comment = "\n".join(_non_comment_lines(compose_text))
    assert re.search(
        r"GOOGLE_SERVICE_ACCOUNT_JSON\s*:\s*/app/secrets/sa\.json", non_comment
    ), (
        "environment 블록에 GOOGLE_SERVICE_ACCOUNT_JSON: /app/secrets/sa.json 이 없음 — "
        "상대경로는 CWD 변경 시 SheetStore 가 JSONDecodeError 로 실패함"
    )


def test_compose_data_volume_mount(compose_text: str) -> None:
    """./data:/app/data 볼륨 마운트가 있어야 한다."""
    assert "./data:/app/data" in compose_text


def test_compose_secrets_readonly_mount(compose_text: str) -> None:
    """./secrets:/app/secrets 가 읽기 전용(:ro)으로 마운트되어야 한다."""
    assert "./secrets:/app/secrets:ro" in compose_text


def test_compose_restart_policy(compose_text: str) -> None:
    """nutti 서비스에 restart: unless-stopped 정책이 있어야 한다."""
    assert "unless-stopped" in compose_text


def test_compose_no_hardcoded_secrets(compose_text: str) -> None:
    """docker-compose.yml 에 하드코딩된 API 키가 없어야 한다.

    모든 시크릿 환경변수(키·토큰·시크릿·웹훅 URL)가 값과 함께 하드코딩되면 안 된다.
    env_file 또는 환경변수 참조($VAR) 방식만 허용.
    URL·하이픈·점(.)이 포함된 토큰 형식도 탐지한다.
    """
    dangerous_pattern = re.compile(
        r"(ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|TELEGRAM_BOT_TOKEN|GEMINI_API_KEY"
        r"|YOUTUBE_CLIENT_SECRET|YOUTUBE_REFRESH_TOKEN"
        r"|INSTAGRAM_ACCESS_TOKEN|DISCORD_WEBHOOK_URL)"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9+/_.:-]{20,}",
        re.IGNORECASE,
    )
    matches = dangerous_pattern.findall(compose_text)
    assert not matches, f"docker-compose.yml 에 하드코딩된 시크릿 발견: {matches}"


def test_compose_env_file_reference(compose_text: str) -> None:
    """nutti 서비스가 env_file 로 환경변수를 로드해야 한다.

    env_file 은 .env 파일에서 시크릿을 주입하는 필수 메커니즘이다.
    이 항목이 없으면 .env 의 모든 시크릿 키가 컨테이너에 전달되지 않는다.
    """
    assert "env_file" in compose_text, (
        "docker-compose.yml nutti 서비스에 env_file 섹션이 없음 — "
        ".env 의 시크릿 키가 컨테이너에 전달되지 않음"
    )


def test_compose_dry_run_safe_default(compose_text: str) -> None:
    """compose environment 블록의 NUTTI_DRY_RUN 이 안전 기본값(true)이어야 한다.

    우선순위: environment 블록 > env_file(.env) > Dockerfile ENV.
    .env 가 없는 신규 VPS 배포 시 ${NUTTI_DRY_RUN:-false} 이면 컨테이너가 즉시
    live 모드로 기동되어 API 키 없는 첫 실행이 실패한다.
    안전 기본값 ${NUTTI_DRY_RUN:-true} 로 유지해야 하며, 실 운영 시 .env 에서
    NUTTI_DRY_RUN=false 를 명시한다.

    단순 'NUTTI_DRY_RUN' 문자열은 주석(line 35)에도 존재하므로 비주석 라인의
    ${...} 패턴과 기본값 방향을 모두 검증한다.
    """
    non_comment = "\n".join(_non_comment_lines(compose_text))
    # environment 블록에 변수 대입 패턴이 있어야 한다
    assert re.search(r"NUTTI_DRY_RUN\s*:\s*\$\{", non_comment), (
        "compose environment 블록에 NUTTI_DRY_RUN: ${...} 오버라이드가 없음 — "
        "environment 블록은 env_file 과 Dockerfile ENV 를 모두 오버라이드하므로 명시 필수"
    )
    # 기본값이 반드시 true(안전) 여야 한다. false 이면 .env 미생성 시 live 모드로 기동.
    assert re.search(r"NUTTI_DRY_RUN\s*:\s*\$\{NUTTI_DRY_RUN:-true\}", non_comment), (
        "NUTTI_DRY_RUN 기본값이 true 가 아님 — .env 없는 신규 배포에서 live 모드로 기동됨. "
        "안전 기본값 ${NUTTI_DRY_RUN:-true} 로 설정하고 .env 에서 false 를 명시해야 함"
    )


def test_compose_ofelia_command_uses_nutti_binary(compose_text: str) -> None:
    """Ofelia job-exec 커맨드 레이블이 비주석 라인에서 'nutti run' 을 명시해야 한다.

    docker exec 는 ENTRYPOINT 를 거치지 않고 PATH 에서 직접 실행 파일을 찾는다.
    'run' 만 지정하면 PATH 에 해당 바이너리가 없으므로 스케줄된 파이프라인이
    매일 09:00 KST 에 'executable not found' 로 실패한다.
    주석 내 'nutti run' 문자열(line 4)로는 통과하지 못하도록 레이블 키와 결합해 검증한다.
    """
    non_comment = "\n".join(_non_comment_lines(compose_text))
    assert re.search(r"ofelia\.job-exec\.run-pipeline\.command.*nutti run", non_comment), (
        "ofelia.job-exec.run-pipeline.command 가 비주석 라인에서 'nutti run' 을 포함하지 않음 — "
        "job-exec 는 ENTRYPOINT 미적용이므로 전체 명령 지정 필수"
    )


def test_compose_ofelia_docker_sock_writable(compose_text: str) -> None:
    """Ofelia docker.sock 마운트가 쓰기 가능(:ro 없음)해야 한다.

    Ofelia job-exec 모드는 Docker API 로 docker exec 를 호출하는 쓰기 연산이므로
    :ro(읽기 전용) 마운트는 모든 스케줄 실행을 permission denied 로 실패시킨다.
    """
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" not in compose_text, (
        "docker.sock 이 :ro 로 마운트되면 Ofelia job-exec 가 항상 실패함 — :ro 제거 필요"
    )


def test_compose_nutti_entrypoint_override(compose_text: str) -> None:
    """nutti 서비스가 sh 를 PID1 으로 하는 entrypoint 오버라이드를 가져야 한다.

    Dockerfile ENTRYPOINT=["nutti"] 상태에서 command 만 오버라이드하면 Docker 가
    'nutti sh -c ...' 로 연결해 Typer 가 'sh' 서브커맨드를 찾지 못하고 exit 2 로 종료한다.
    컨테이너가 즉시 종료되면 Ofelia 가 exec 할 대상이 없어 스케줄이 동작하지 않는다.
    """
    assert "/bin/sh" in compose_text, (
        "nutti 서비스 entrypoint 에 /bin/sh 가 없으면 컨테이너가 ENTRYPOINT+command 결합으로 "
        "즉시 종료되어 Ofelia exec 대상이 사라짐"
    )


def test_compose_nutti_idle_command(compose_text: str) -> None:
    """nutti 서비스 command 에 idle 프로세스(tail -f /dev/null 또는 sleep infinity)가 있어야 한다.

    Ofelia job-exec 방식은 컨테이너가 상시 기동 중이어야 동작한다.
    idle 명령 없이 종료되면 스케줄된 파이프라인이 절대 실행되지 않는다.
    """
    has_idle = "tail -f /dev/null" in compose_text or "sleep infinity" in compose_text
    assert has_idle, (
        "nutti 서비스 command 에 idle 프로세스(tail -f /dev/null 또는 sleep infinity)가 없으면 "
        "컨테이너가 종료되어 Ofelia job-exec 가 동작하지 않음"
    )


# ── 교차 검증: DEPLOY.md ↔ config.py / docker-compose.yml ───────────────────


@pytest.fixture(scope="module")
def deploy_md_text() -> str:
    """docs/DEPLOY.md 내용을 읽어 반환한다."""
    path = ROOT / "docs" / "DEPLOY.md"
    assert path.exists(), "docs/DEPLOY.md 가 없음"
    return path.read_text(encoding="utf-8")


def test_deploy_md_env_vars_match_config(deploy_md_text: str) -> None:
    """DEPLOY.md dotenv 코드블록의 환경변수 키가 Settings alias 집합에 존재해야 한다.

    pydantic extra='ignore' 로 인해 오타 키는 조용히 무시된다.
    이 테스트가 없으면 INSTAGRAM_BUSINESS_ACCOUNT_ID 같은 오기가 운영에서 침묵 실패한다.
    """
    from nutti.config import Settings

    # Settings 의 alias 집합 수집
    valid_aliases: set[str] = set()
    for field_info in Settings.model_fields.values():
        if field_info.alias:
            valid_aliases.add(str(field_info.alias).upper())

    # 시스템/인프라 변수 허용목록 (Settings alias 에 없어도 유효)
    system_allowlist = {
        "TZ",
        "NUTTI_STATE_PATH",
        "NUTTI_REVIEW_STORE_PATH",
        "NUTTI_MEDIA_DIR",
        "NUTTI_MASCOT_IMAGE",
        "NUTTI_DRY_RUN",
        # claude CLI(claude -p)가 직접 읽는 헤드리스 인증 토큰 — nutti Settings가 아니라
        # 컨테이너 내 claude CLI가 소비한다(옵션 B, 2026-06-30).
        "CLAUDE_CODE_OAUTH_TOKEN",
    }

    # dotenv 코드블록 파싱: ```dotenv ... ``` 블록 내 KEY= 패턴 추출
    dotenv_blocks = re.findall(r"```dotenv\n(.*?)```", deploy_md_text, re.DOTALL)
    found_keys: list[str] = []
    for block in dotenv_blocks:
        for line in block.splitlines():
            stripped = line.strip()
            # 주석·빈 줄 건너뜀
            if not stripped or stripped.startswith("#"):
                continue
            m = re.match(r"([A-Z0-9_]+)\s*=", stripped)
            if m:
                found_keys.append(m.group(1))

    assert found_keys, "DEPLOY.md 에 dotenv 코드블록이 없음"

    unknown_keys = [
        k for k in found_keys if k not in valid_aliases and k not in system_allowlist
    ]
    assert not unknown_keys, (
        f"DEPLOY.md dotenv 블록에 config.py Settings alias 에 없는 키 발견: {unknown_keys} — "
        "오타이거나 alias 와 불일치하면 pydantic 이 조용히 무시하여 운영 침묵 실패 발생"
    )


def test_deploy_md_service_names_exist_in_compose(
    deploy_md_text: str, compose_text: str
) -> None:
    """DEPLOY.md 에서 참조하는 서비스명이 docker-compose.yml services 에 존재해야 한다.

    docs 와 실제 compose 파일이 불일치하면 `docker compose logs <없는서비스>` 가
    오류를 반환해 운영자를 혼란에 빠뜨린다.
    yaml 의존 없이 정규식으로 services 블록의 최상위 서비스명을 추출한다.
    """
    # docker-compose.yml 의 services 블록에서 서비스명 파싱.
    # services: 섹션 이후 등장하는 "정확히 2칸 들여쓰기 + 단어 + 콜론" 패턴을 수집한다.
    services_start = compose_text.find("services:")
    assert services_start != -1, "docker-compose.yml 에 'services:' 키가 없음"
    after_services = compose_text[services_start:]
    svc_pattern = re.compile(r"^  ([\w-]+):\s*(?:#.*)?$", re.MULTILINE)
    defined_services: set[str] = set(svc_pattern.findall(after_services))

    assert defined_services, "docker-compose.yml 에서 services 키를 파싱하지 못함"

    # DEPLOY.md 에서 `docker compose logs <svc>` / `docker compose run --rm <svc>` 패턴 추출
    ref_pattern = re.compile(
        r"docker compose (?:logs(?: -f)?|run --rm(?:\s+-e\s+\S+)*)\s+(\w[\w-]*)"
    )
    referenced: set[str] = set(ref_pattern.findall(deploy_md_text))

    # nutti 는 서비스명이자 바이너리명으로 혼용 — 별도 검증, 여기서는 제외
    referenced.discard("nutti")
    # 파싱 오염(플래그 등) 제거
    svc_refs = {s for s in referenced if s not in {"true", "false", "d", "f"}}

    unknown_svcs = svc_refs - defined_services
    assert not unknown_svcs, (
        f"DEPLOY.md 가 docker-compose.yml 에 없는 서비스를 참조: {unknown_svcs} — "
        "서비스명이 바뀌었으면 DEPLOY.md 도 함께 업데이트해야 함"
    )


# ── runner 서비스 검증 (yaml.safe_load 기반) ────────────────────────────────


@pytest.fixture(scope="module")
def compose_data() -> dict:
    """docker-compose.yml 을 yaml.safe_load 로 파싱한 딕셔너리를 반환한다.

    yaml 앵커(<<: *nutti-base) 는 safe_load 가 머지 키를 처리하므로
    실제 서비스 딕셔너리 값으로 확인할 수 있다.
    """
    path = ROOT / "docker-compose.yml"
    assert path.exists(), "docker-compose.yml 이 프로젝트 루트에 없음"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_runner_service_exists(compose_data: dict) -> None:
    """runner 서비스가 docker-compose.yml services 에 정의되어야 한다."""
    services = compose_data.get("services", {})
    assert "runner" in services, (
        "runner 서비스가 없음 — `docker compose run --rm runner run '주제'` 수동 실행 불가"
    )


def test_runner_profiles_manual(compose_data: dict) -> None:
    """runner 서비스가 profiles: [manual] 로 선언되어 평소 up -d 에 포함되지 않아야 한다."""
    runner = compose_data["services"]["runner"]
    profiles = runner.get("profiles", [])
    assert "manual" in profiles, (
        f"runner profiles 에 'manual' 이 없음 (현재: {profiles}) — "
        "`docker compose up -d` 시 runner 가 불필요하게 기동됨"
    )


def test_runner_no_entrypoint_override(compose_data: dict) -> None:
    """runner 서비스에 entrypoint 오버라이드가 없어야 한다.

    nutti 서비스는 Ofelia 대기용으로 entrypoint 를 /bin/sh 로 오버라이드하지만,
    runner 는 Dockerfile ENTRYPOINT ["nutti"] 를 그대로 사용해야 인자가
    nutti 서브커맨드로 올바르게 전달된다.
    """
    runner = compose_data["services"]["runner"]
    assert "entrypoint" not in runner, (
        f"runner 에 entrypoint 오버라이드가 있음: {runner.get('entrypoint')} — "
        "Dockerfile ENTRYPOINT ['nutti'] 를 그대로 써야 run '주제' 가 동작함"
    )


def test_runner_inherits_volumes(compose_data: dict) -> None:
    """runner 서비스가 nutti 서비스와 동일한 볼륨 마운트를 상속해야 한다.

    yaml 앵커 머지(<<: *nutti-base) 로 볼륨이 공유되는지 실제 파싱 결과로 확인한다.
    """
    runner = compose_data["services"]["runner"]
    volumes = runner.get("volumes", [])
    # ./data:/app/data 와 ./secrets:/app/secrets:ro 가 모두 있어야 한다.
    volume_strings = [v if isinstance(v, str) else str(v) for v in volumes]
    assert any("data:/app/data" in v for v in volume_strings), (
        "runner volumes 에 ./data:/app/data 마운트가 없음"
    )
    assert any("secrets:/app/secrets" in v for v in volume_strings), (
        "runner volumes 에 ./secrets:/app/secrets:ro 마운트가 없음"
    )


def test_runner_inherits_env(compose_data: dict) -> None:
    """runner 서비스가 GOOGLE_SERVICE_ACCOUNT_JSON 환경변수를 상속해야 한다.

    yaml 앵커 머지로 environment 블록이 공유되는지 실제 파싱 결과로 확인한다.
    """
    runner = compose_data["services"]["runner"]
    env = runner.get("environment", {})
    # environment 는 dict 또는 list 형태일 수 있다.
    if isinstance(env, dict):
        assert "GOOGLE_SERVICE_ACCOUNT_JSON" in env, (
            "runner environment 에 GOOGLE_SERVICE_ACCOUNT_JSON 이 없음"
        )
    else:
        keys = [e.split("=")[0] if "=" in e else e for e in env]
        assert "GOOGLE_SERVICE_ACCOUNT_JSON" in keys, (
            "runner environment 에 GOOGLE_SERVICE_ACCOUNT_JSON 이 없음"
        )


def test_runner_restart_no(compose_data: dict) -> None:
    """runner 서비스의 restart 정책이 'no' 여야 한다 (one-shot 서비스)."""
    runner = compose_data["services"]["runner"]
    restart = runner.get("restart", "")
    assert restart == "no", (
        f"runner restart 가 'no' 가 아님 (현재: {restart!r}) — "
        "one-shot 실행 후 컨테이너가 자동 재시작되면 안 됨"
    )


# ── config 서브커맨드 CliRunner 검증 ─────────────────────────────────────────


def test_cli_config_exits_zero(monkeypatch) -> None:
    """nutti config 서브커맨드가 NUTTI_DRY_RUN=true 환경에서 exit_code 0 으로 종료해야 한다.

    Dockerfile HEALTHCHECK 가 `nutti config` 를 사용하므로 이 커맨드가 비정상 종료하면
    컨테이너가 unhealthy 로 마킹되어 Ofelia 가 job-exec 대상을 찾지 못한다.
    네트워크 호출 없이 typer.testing.CliRunner 로 검증한다.
    """
    from typer.testing import CliRunner

    from nutti.cli import app

    # dry_run 보장 — conftest 의 httpx 차단과 함께 외부 호출 완전 차단
    monkeypatch.setenv("NUTTI_DRY_RUN", "true")
    monkeypatch.setenv("NUTTI_ENV", "test")
    # get_settings() 캐시를 초기화해 새 환경변수 값이 반영되도록 한다.
    try:
        from nutti.config import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    except AttributeError:
        pass  # 캐시 없는 버전은 건너뜀

    runner = CliRunner()
    result = runner.invoke(app, ["config"])

    assert result.exit_code == 0, (
        f"nutti config 가 exit_code {result.exit_code} 로 종료됨 — "
        f"출력: {result.output!r}, 예외: {result.exception}"
    )
