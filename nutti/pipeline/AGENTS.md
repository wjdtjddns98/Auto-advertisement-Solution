<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-04 | Updated: 2026-06-04 -->

# pipeline

## Purpose
5단계 파이프라인 오케스트레이션. 대본 → (팩트체크) → 검수① → 영상 → 검수② →
메타데이터 → 검수③ → 업로드 → 성과 수집/분석을 순서대로 엮는다.

## Key Files
| File | Description |
|------|-------------|
| `orchestrator.py` | `Orchestrator`(전체 흐름) + `GateRejected`/`FactCheckFailed` 예외. 게이트·AI·스튜디오·퍼블리셔·스토어를 조립 |

## For AI Agents

### Working In This Directory
- **팩트체크 정책(자동 거절/재생성)**: `run()`은 대본 생성 직후 `_fact_check()`를 호출한다.
  실패하면 issues를 피드백으로 대본을 **재생성**(`max_factcheck_retries`, 기본 1)하고, 한도를
  넘으면 `FactCheckFailed`로 **차단**한다. `Script.fact_checked`는 결과로 설정된다.
  dry_run은 항상 통과하므로 dry-run 흐름은 영향 없음.
- **검수 게이트**: 각 단계 사이에 `_gate()`로 `ReviewGate.request()` 호출. APPROVED가 아니면
  `GateRejected`로 중단. 게이트는 생성자에 주입 가능(테스트는 `AutoApproveGate`).
- **로깅 순서**: `log_script`는 팩트체크를 통과한 대본만 기록한다(거절 대본은 기록 안 함,
  `factcheck.rejected` 로그로 남음).
- 공개 흐름을 바꿀 때 `run()`의 단계 순서와 `current_stage` 전이를 유지할 것.

### Testing Requirements
- `tests/test_pipeline.py`의 dry-run 스모크 + 팩트체크 배선(호출/재생성/거절/경계) 통과.
- AI/스토어를 monkeypatch해 팩트체크 분기·로그 호출 횟수를 단언.

### Common Patterns
- 의존성(ai/studio/publisher/store/gates)은 생성자 주입으로 테스트 가능하게 구성.

## Dependencies

### Internal
- `nutti.integrations`(ai_text/video/publishing), `nutti.review.gates`,
  `nutti.storage.sheets`, `nutti.config`, `nutti.models`

### External
- (간접) 하위 클라이언트들의 외부 의존성

<!-- MANUAL: -->
