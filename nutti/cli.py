"""Nutti CLI. N8n 스케줄러나 수동 실행에서 호출하는 진입점.

예)
    nutti run "강아지 닭가슴살 간식, 하루 적정량은?"
    nutti run                      # 주제 자동 생성(직전 성과 피드백 반영)
    nutti run "..." --reels
    nutti config
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import typer

from nutti.config import get_settings
from nutti.logging import configure_logging
from nutti.models import ContentFormat
from nutti.pipeline.cost import format_cost
from nutti.pipeline.cost_ledger import CostLedger, format_summary, summarize_records
from nutti.pipeline.orchestrator import GateRejected, Orchestrator

app = typer.Typer(help="Nutti 애견간식 콘텐츠 자동화 파이프라인")


@app.command()
def run(
    topic: Optional[str] = typer.Argument(
        None, help="대본 주제(생략 시 직전 성과·최근 주제를 반영해 자동 생성)"
    ),
    reels: bool = typer.Option(False, "--reels", help="인스타 릴스도 함께 업로드"),
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

    orchestrator = Orchestrator(settings)
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

    analysis = orchestrator.collect_and_analyze(result)
    typer.echo(f"\n[성과 분석 → 다음 사이클 피드백으로 저장됨]\n{analysis}")


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
