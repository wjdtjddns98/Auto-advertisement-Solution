export const meta = {
  name: 'nutti-team',
  description: 'Autonomous role-specialized team (economy): plan (opus) -> research -> parallel build (sonnet, complex workstream auto-up to opus) -> adversarial review (sonnet dims + haiku verify) <-> fix -> QA <-> fix loop -> report. Args directive. Add "opus" to args to force opus developers, "lite" to skip research/report and suppress auto-up.',
  whenToUse: 'When the PO gives a feature/work directive and wants the team to implement, adversarially review, verify, and report it autonomously at low cost.',
  phases: [
    { title: 'Plan', model: 'opus' },
    { title: 'Research', model: 'sonnet' },
    { title: 'Build', model: 'sonnet' },
    { title: 'Review', model: 'sonnet' },
    { title: 'Verify', model: 'haiku' },
    { title: 'Fix', model: 'sonnet' },
    { title: 'QA', model: 'sonnet' },
    { title: 'Report', model: 'haiku' },
  ],
}

const raw = typeof args === 'string' ? args.trim() : ''
const directive = raw || '(directive not provided — read .omc/po-request.md, else report no directive)'
// PO knobs in the directive text:
const forceOpus = /\bopus\b|오퍼스/i.test(raw) // force opus developers everywhere
const lite = /\blite\b|라이트|간단히|빠르게/i.test(raw) // skip research + report, suppress auto-up
const devModel = forceOpus ? 'opus' : 'sonnet' // economy default sonnet
// planner가 매긴 workstream complexity로 모델 자동 라우팅 (lite면 절약 유지)
const modelFor = (ws) => {
  if (forceOpus) return 'opus'
  if (!lite && ws && ws.complexity === 'complex') return 'opus'
  return 'sonnet'
}

const PLAN_SCHEMA = {
  type: 'object',
  properties: {
    goal: { type: 'string' }, scope: { type: 'string' },
    shared_prep: { type: 'object', properties: { needed: { type: 'boolean' }, files: { type: 'array', items: { type: 'string' } }, what: { type: 'string' } }, required: ['needed'] },
    workstreams: { type: 'array', items: { type: 'object', properties: { name: { type: 'string' }, files: { type: 'array', items: { type: 'string' } }, detail: { type: 'string' }, complexity: { type: 'string', enum: ['simple', 'standard', 'complex'] } }, required: ['name', 'files', 'detail', 'complexity'] } },
    acceptance_criteria: { type: 'array', items: { type: 'string' } },
    research_topics: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
    branch_name: { type: 'string' },
    commit_title: { type: 'string' },
  },
  required: ['goal', 'scope', 'workstreams', 'acceptance_criteria', 'research_topics', 'branch_name', 'commit_title'],
}
const RESEARCH_SCHEMA = { type: 'object', properties: { topic: { type: 'string' }, summary: { type: 'string' }, impl_notes: { type: 'string' }, sources: { type: 'array', items: { type: 'string' } } }, required: ['topic', 'summary', 'impl_notes'] }
const BUILD_SCHEMA = { type: 'object', properties: { summary: { type: 'string' }, files_changed: { type: 'array', items: { type: 'string' } }, tests_added: { type: 'array', items: { type: 'string' } }, blocked: { type: 'array', items: { type: 'string' } } }, required: ['summary', 'files_changed'] }
const FINDINGS_SCHEMA = { type: 'object', properties: { findings: { type: 'array', items: { type: 'object', properties: { title: { type: 'string' }, file: { type: 'string' }, line: { type: 'number' }, severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] }, description: { type: 'string' }, suggestion: { type: 'string' } }, required: ['title', 'file', 'severity', 'description'] } } }, required: ['findings'] }
const VERDICT_SCHEMA = { type: 'object', properties: { isReal: { type: 'boolean' }, severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] }, reasoning: { type: 'string' } }, required: ['isReal', 'reasoning'] }
const QA_SCHEMA = { type: 'object', properties: { verdict: { type: 'string', enum: ['PASS', 'FAIL'] }, evidence: { type: 'array', items: { type: 'string' } }, failures: { type: 'array', items: { type: 'string' } } }, required: ['verdict', 'evidence'] }
const REPORT_SCHEMA = { type: 'object', properties: { markdown: { type: 'string' } }, required: ['markdown'] }

// --- Plan ---
phase('Plan')
const plan = await agent(
  `You are the planner. PO directive:\n\n"${directive}"\n\nProduce a concrete, file-level spec for the Nutti project. Split into WORKSTREAMS with DISJOINT file ownership (parallel build, no conflict). Shared files (models.py/config.py) go in shared_prep (done once first), NOT in any workstream. 1-4 workstreams; 1 if indivisible. Rate each workstream's complexity (simple|standard|complex — complex routes a stronger model). Suggest branch_name (feat/...|fix/...|docs/..., ASCII kebab-case) and commit_title (Conventional Commit).`,
  { label: 'plan', phase: 'Plan', model: 'opus', agentType: 'nutti-planner', schema: PLAN_SCHEMA }
)
if (!plan) {
  log('nutti-team aborted: planner returned null (agent died or was skipped)')
  return { directive, error: 'planner failed — re-run the workflow or check the directive' }
}

// --- Research (skip in lite) ---
let research = []
const topics = lite ? [] : (plan.research_topics || []).filter((t) => t && t.trim())
if (topics.length) {
  phase('Research')
  research = (await parallel(topics.map((t) => () => agent(`Research for Nutti: ${t}`, { label: `research:${t.slice(0, 20)}`, phase: 'Research', model: 'sonnet', agentType: 'nutti-researcher', schema: RESEARCH_SCHEMA })))).filter(Boolean)
}
const planText = JSON.stringify(plan)
const researchText = research.length ? JSON.stringify(research) : '(none)'

// --- Build (shared prep -> parallel workstreams) ---
phase('Build')
if (plan.shared_prep && plan.shared_prep.needed) {
  await agent(`Developer SHARED PREP (sequential, first). Modify only shared files ${JSON.stringify(plan.shared_prep.files || [])}. What: ${plan.shared_prep.what || ''}\nPLAN: ${planText}`, { label: 'build:shared-prep', phase: 'Build', model: devModel, agentType: 'nutti-developer', schema: BUILD_SCHEMA })
}
const workstreams = plan.workstreams && plan.workstreams.length ? plan.workstreams : [{ name: 'main', files: [], detail: plan.goal }]
const parallelMode = workstreams.length > 1
let builds
if (parallelMode) {
  builds = (await parallel(workstreams.map((ws) => () => agent(
    `You are ONE of ${workstreams.length} developers IN PARALLEL. Implement "${ws.name}". Touch ONLY: ${JSON.stringify(ws.files)} (+ their tests). Run \`ruff check\` on YOUR files; do NOT block on full pytest (QA runs it after join). Add targeted tests.\nPLAN: ${planText}\nRESEARCH: ${researchText}`,
    { label: `build:${ws.name}`.slice(0, 40), phase: 'Build', model: modelFor(ws), agentType: 'nutti-developer', schema: BUILD_SCHEMA })))).filter(Boolean)
} else {
  const ws = workstreams[0]
  builds = [await agent(`You are the developer (solo). Implement "${ws.name}". Files: ${JSON.stringify(ws.files)}. Detail: ${ws.detail}\nRun ruff + full pytest to green.\nPLAN: ${planText}\nRESEARCH: ${researchText}\nPO: ${directive}`, { label: `build:${ws.name}`.slice(0, 40), phase: 'Build', model: modelFor(ws), agentType: 'nutti-developer', schema: BUILD_SCHEMA })]
}
// 수정 루프용 모델: complex workstream이 하나라도 있으면 수정도 같은 급으로
const fixModel = forceOpus || (!lite && workstreams.some((ws) => ws.complexity === 'complex')) ? 'opus' : 'sonnet'

// --- Adversarial Review <-> Fix loop (sonnet dims + haiku refute-verify) ---
const DIMENSIONS = ['correctness', 'security', 'robustness', 'test-adequacy']
const allFindings = []
let round = 0
let openConfirmed = []
do {
  round += 1
  const reviewed = await pipeline(
    DIMENSIONS,
    (d) => agent(`Review the changes for "${directive}" (git diff + working tree) on the ${d} dimension. Read the actual changed files.`, { label: `review:${d}:r${round}`, phase: 'Review', model: 'sonnet', agentType: 'nutti-reviewer', schema: FINDINGS_SCHEMA }),
    (rev, d) => parallel((rev.findings || []).map((f) => () =>
      agent(`Adversarially VERIFY (try to REFUTE) this ${d} finding. Read ${f.file}. Finding: "${f.title}" — ${f.description}. Real & actionable, not intended design? Default isReal=false if uncertain.`, { label: `verify:${d}`, phase: 'Verify', model: 'haiku', agentType: 'nutti-reviewer', schema: VERDICT_SCHEMA })
        .then((v) => ({ ...f, verdict: v }))))
  )
  const confirmed = reviewed.flat().filter(Boolean).filter((f) => f.verdict && f.verdict.isReal)
  allFindings.push(...confirmed)
  openConfirmed = confirmed.filter((f) => (f.verdict.severity || f.severity) === 'critical' || (f.verdict.severity || f.severity) === 'high')
  if (openConfirmed.length) {
    await agent(`Developer (solo fix). Fix these confirmed findings, then run ruff + full pytest to green:\n${JSON.stringify(openConfirmed)}`, { label: `fix:r${round}`, phase: 'Fix', model: fixModel, agentType: 'nutti-developer', schema: BUILD_SCHEMA })
  }
} while (openConfirmed.length && round < 2)

// --- QA <-> Fix loop (FAIL이면 수정 후 재QA, 최대 2회 수정) ---
phase('QA')
let qa = null
let qaRound = 0
do {
  qaRound += 1
  qa = await agent(`QA. Verify the joined result for "${directive}" vs acceptance criteria: ${JSON.stringify(plan.acceptance_criteria)}. Run ruff + full pytest + dry_run smoke. PASS/FAIL with evidence. On FAIL, list each failure with the reproduce command + error so a fix-developer can act directly.`, { label: `qa:r${qaRound}`, phase: 'QA', model: 'sonnet', agentType: 'nutti-qa', schema: QA_SCHEMA })
  if (!qa) qa = { verdict: 'FAIL', evidence: [], failures: ['QA agent returned null'] }
  if (qa.verdict === 'FAIL' && qaRound <= 2) {
    log(`QA FAIL (round ${qaRound}) — fixing: ${(qa.failures || []).join(' | ').slice(0, 200)}`)
    await agent(`Developer (solo fix). QA FAILED for "${directive}". Reproduce and fix each failure at its root cause (never weaken/delete tests), then run ruff + full pytest to green:\n${JSON.stringify(qa.failures || [])}`, { label: `qa-fix:r${qaRound}`, phase: 'Fix', model: fixModel, agentType: 'nutti-developer', schema: BUILD_SCHEMA })
  }
} while (qa.verdict === 'FAIL' && qaRound <= 2)

// --- Report (skip in lite) ---
let reportMarkdown = '(lite: report skipped)'
if (!lite) {
  phase('Report')
  const report = await agent(`Reporter. Korean 작업 보고 + 다음 Todo for "${directive}". Use git diff/log + QA verdict: ${JSON.stringify(qa)}. Markdown only.`, { label: 'report', phase: 'Report', model: 'haiku', agentType: 'nutti-reporter', schema: REPORT_SCHEMA })
  reportMarkdown = report.markdown
}

log(`nutti-team done: dev=${devModel}(auto-up=${!lite && !forceOpus}), lite=${lite}, workstreams=${workstreams.length}, review rounds=${round}, confirmed=${allFindings.length}, QA=${qa.verdict} (${qaRound} run${qaRound > 1 ? 's' : ''})`)

return {
  directive,
  mode: { devModel, lite, forceOpus },
  plan,
  research,
  builds,
  confirmed_findings: allFindings,
  qa,
  report_markdown: reportMarkdown,
  // 리드(메인 세션)의 git 자동화용 제안 — nutti-day 루프가 그대로 사용
  git: { branch_name: plan.branch_name || '', commit_title: plan.commit_title || '' },
}
