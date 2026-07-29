"""Nutti CLI. N8n 스케줄러나 수동 실행에서 호출하는 진입점.

예)
    nutti run "강아지 닭가슴살 간식, 하루 적정량은?"
    nutti run                      # 주제 자동 생성(직전 성과 피드백 반영)
    nutti run "..." --reels
    nutti config
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from nutti.config import get_settings
from nutti.logging import configure_logging
from nutti.models import ContentFormat
from nutti.pipeline.cost import format_cost
from nutti.pipeline.cost_ledger import CostLedger, format_summary, summarize_records
from nutti.pipeline.orchestrator import GateRejected, Orchestrator

# Windows 콘솔/리다이렉트(cp949)에서 분석 텍스트의 유니코드(— 등)가 UnicodeEncodeError로
# 마지막 출력을 죽이는 것 방지(실측 2026-07-06: 업로드·상태저장 성공 후 최종 print에서
# exit 1). 인코딩 불가 문자만 ?로 대체 — 콘솔 표시 전용, 데이터 경로와 무관.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (OSError, ValueError):  # pragma: no cover - 특수 콘솔 방어
            pass

app = typer.Typer(help="Nutti 애견간식 콘텐츠 자동화 파이프라인")

# 동시 실행 방지 락(2026-07-29 실측 사고). 검수 게이트에서 대기 중인 런이 있는데 새 런을
# 띄우면 두 프로세스가 같은 봇 토큰으로 getUpdates 롱폴을 걸어 텔레그램이 409 Conflict로
# 한쪽을 죽인다(실측: 대본 카드까지 보낸 런이 통째로 소실). 게이트 충돌뿐 아니라 두 런이
# 동시에 영상을 만들면 과금도 두 배다.
_RUN_LOCK_NAME = "run.lock"
# 락이 이 시간보다 오래되면 죽은 런의 잔재로 보고 인수한다 — 검수 대기 상한
# (review_timeout_sec, 기본 1시간)보다 넉넉해야 정상 대기 중인 런을 뺏지 않는다.
_RUN_LOCK_STALE_MARGIN_SEC = 1800


def _acquire_run_lock(settings) -> Path | None:
    """실행 락을 잡는다. 이미 살아있는 런이 있으면 None을 반환한다."""
    import os
    import time

    lock = Path(settings.state_path).parent / _RUN_LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    stale_after = settings.review_timeout_sec + _RUN_LOCK_STALE_MARGIN_SEC
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = 0.0
        if age < stale_after:
            return None
        # 죽은 런의 잔재 — 인수한다(이 시점엔 게이트 대기 상한도 이미 지났다).
        lock.unlink(missing_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return lock


@app.command()
def run(
    topic: Optional[str] = typer.Argument(
        None, help="대본 주제(생략 시 직전 성과·최근 주제를 반영해 자동 생성)"
    ),
    reels: bool = typer.Option(
        False, "--reels", help="인스타 릴스용 영상·캡션을 텔레그램으로 핸드오프(수동 업로드)"
    ),
    feedback: str = typer.Option(
        "", "--feedback", help="이전 사이클 개선 포인트(생략 시 직전 분석을 자동 사용)"
    ),
) -> None:
    """대본→영상→업로드까지 한 사이클 실행.

    주제를 생략하면 직전 사이클의 성과 분석을 피드백으로 반영해 다음 주제를 자동 생성한다.
    실행 후 분석 결과는 상태에 저장되어 다음 사이클로 자동 연결된다(피드백 루프).
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    fmt = ContentFormat.REELS if reels else ContentFormat.SHORTS

    lock = _acquire_run_lock(settings)
    if lock is None:
        typer.secho(
            "이미 실행 중인 런이 있습니다 — 검수 게이트 대기 중일 수 있습니다. "
            "두 런이 같이 돌면 텔레그램 폴링이 409로 충돌해 한쪽이 죽고 영상 과금도 "
            "두 배가 됩니다. 그 런을 끝내거나 data/run.lock을 지운 뒤 다시 실행하세요.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=3)
    try:
        _run_cycle(settings, topic, feedback, fmt)
    finally:
        lock.unlink(missing_ok=True)


def _run_cycle(settings, topic: Optional[str], feedback: str, fmt: ContentFormat) -> None:
    """한 사이클 본체(락은 호출부가 관리한다)."""
    orchestrator = Orchestrator(settings)
    # 성과 수집: 숙성된(며칠 지난) 직전 업로드의 조회수를 걷어 이번 사이클 피드백으로
    # 저장한다. 업로드 직후엔 Analytics가 0이라, 수집은 항상 지난 사이클 영상을 대상으로
    # 지연 수행한다(collect_ready_feedback). resolve_inputs보다 먼저 호출해 방금 걷은
    # 피드백이 이번 대본에 곧바로 반영되게 한다.
    analysis = orchestrator.collect_ready_feedback()
    if analysis:
        typer.echo(f"[직전 업로드 성과 분석 → 피드백 저장]\n{analysis}\n")

    # 피드백 자동 연결 + (주제 미지정 시) 주제 자동 생성.
    topic, feedback = orchestrator.resolve_inputs(topic, feedback)
    typer.secho(f"주제: {topic}", fg=typer.colors.CYAN)
    if feedback:
        typer.echo(f"(직전 피드백 반영: {feedback[:60]}…)")

    try:
        result = orchestrator.run(topic, content_format=fmt, feedback=feedback)
    except GateRejected as exc:
        typer.secho(f"검수 중단: {exc}", fg=typer.colors.YELLOW)
        raise typer.Exit(code=2) from exc

    typer.secho(f"완료: run={result.id}", fg=typer.colors.GREEN)
    for up in result.uploads:
        typer.echo(f"  - {up.platform}: {up.url}")

    if result.cost is not None:
        typer.echo("")
        typer.secho(format_cost(result.cost), fg=typer.colors.MAGENTA)

    typer.echo("\n[이번 업로드는 성과 수집 대기 큐에 등록됨 — 며칠 뒤 다음 실행에서 분석]")


@app.command()
def cost(
    days: int = typer.Option(
        0, "--days", help="최근 N일 누적도 함께 표시(0=생략)"
    ),
) -> None:
    """누적 제작 비용 조회 — 오늘·이번 달·전체 실제 지출(+선택 최근 N일).

    각 `nutti run` 실행이 비용 원장에 기록되며, 여기서 일/월/전체로 합산한다.
    dry_run 실행은 실제 지출 0으로 집계되고, 라이브였다면 들었을 예상치는 별도 표시.
    """
    s = get_settings()
    ledger = CostLedger(s.cost_ledger_path)
    buckets = summarize_records(
        ledger.records(), now=datetime.now().astimezone(), days=(days or None)
    )
    typer.secho(format_summary(buckets, days=(days or None)), fg=typer.colors.MAGENTA)


@app.command()
def config() -> None:
    """현재 설정 요약 출력(민감정보는 마스킹)."""
    s = get_settings()
    typer.echo(f"env={s.env}  dry_run={s.dry_run}  model={s.script_model}")
    typer.echo(f"anthropic_key={'설정됨' if s.anthropic_api_key else '미설정'}")
    typer.echo(f"telegram={'설정됨' if s.telegram_bot_token else '미설정'}")
    typer.echo(f"calculator_url={s.calculator_url}")


if __name__ == "__main__":
    app()
