# Nutti 에이전트 팀 (PO 전용 가이드)

당신은 **기획자(PO)** 역할만 합니다. 지시만 내리면 역할별 에이전트 팀이 **완전 자동**으로
설계 → 리서치 → 개발 → 리뷰 → QA → 보고까지 수행합니다.

## 팀 구성 (`.claude/agents/`)

| 역할 | 에이전트 | 모델 | 하는 일 |
|------|----------|------|---------|
| 🧭 기획 보조 | `nutti-planner` | opus | PO 지시 → 구체 스펙·작업분해·수용기준·리서치 주제 |
| 🔬 리서치 | `nutti-researcher` | sonnet | 외부 API/SDK 조사 → 출처 포함 구현 노트 |
| 💻 개발 | `nutti-developer` | sonnet | 스펙대로 구현 + 테스트, ruff/pytest green |
| 🔍 코드리뷰 | `nutti-reviewer` | sonnet | 적대적 리뷰(결함·보안·회귀·테스트) |
| ✅ QA | `nutti-qa` | sonnet | 테스트·수용기준 검증, 증거 기반 PASS/FAIL |
| 📝 보고서 | `nutti-reporter` | haiku | 한국어 작업 보고 + 다음 Todo |

## 쓰는 법 (PO는 이 한 줄)

```
Workflow({ name: 'nutti-team', args: '<원하는 기능/작업 한 줄>' })
```

예) `Workflow({ name: 'nutti-team', args: 'storage/sheets.py의 Google Sheets 실연동 구현(드라이런 유지)' })`

파이프라인:
```
기획(planner) → [리서치(researcher)·필요시 병렬] → 개발(developer)
   → 리뷰(reviewer) ↔ 수정(developer) 루프(최대 2회) → QA(qa) → 보고(reporter)
```

결과로 `plan / research / build / review_findings / qa / report_markdown`가 돌아옵니다.
그 뒤 **리드(메인 세션)가** 표준 워크플로대로 처리: ruff/pytest 최종 확인 → 브랜치 →
PR → **dev 자동 머지**(정책상 dev는 자율, main은 PO 승인) → 보고서를 Notion 일일 업무보고
DB에 기록(원하면).

## 개별 호출도 가능

특정 역할만 쓰고 싶으면 Agent 툴로 직접:
`Agent(subagent_type: 'nutti-researcher', prompt: 'Hedra Character-3 API 조사')` 등.

## 비용/모델
역할별로 모델이 티어링돼 있습니다(기획/깊은 추론=opus, 표준=sonnet, 보고=haiku) — 토큰 절감.
복잡한 구현이 필요하면 PO가 "개발은 opus로" 같이 지시하면 그 단계만 상향할 수 있습니다.

## 한계
- 실제 외부 API 호출 검증은 **키가 있어야** 가능 — 키 전엔 dry_run·테스트까지만 검증됩니다.
- 비개발(미팅·단가 등) 업무는 자동화 대상이 아닙니다.
