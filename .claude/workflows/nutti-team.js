export const meta = {
  name: 'nutti-team',
  description: 'Autonomous role-specialized team for the Nutti project: plan -> research -> build -> review/fix loop -> QA -> report. The PO passes a directive via args; the team executes end to end.',
  whenToUse: 'When the product owner gives a feature/work directive and wants the team to implement, review, verify, and report it autonomously.',
  phases: [
    { title: 'Plan', model: 'opus' },
    { title: 'Research', model: 'sonnet' },
    { title: 'Build', model: 'sonnet' },
    { title: 'Review', model: 'sonnet' },
    { title: 'Fix', model: 'sonnet' },
    { title: 'QA', model: 'sonnet' },
    { title: 'Report', model: 'haiku' },
  ],
}

const directive =
  typeof args === 'string' && args.trim()
    ? args.trim()
    : '(directive not provided as args — read .omc/po-request.md for the PO request, else stop and report that no directive was given)'

const PLAN_SCHEMA = {
  type: 'object',
  properties: {
    goal: { type: 'string' },
    scope: { type: 'string' },
    tasks: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, what: { type: 'string' }, why: { type: 'string' } }, required: ['file', 'what'] } },
    acceptance_criteria: { type: 'array', items: { type: 'string' } },
    research_topics: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
  },
  required: ['goal', 'scope', 'tasks', 'acceptance_criteria', 'research_topics'],
}
const RESEARCH_SCHEMA = { type: 'object', properties: { topic: { type: 'string' }, summary: { type: 'string' }, impl_notes: { type: 'string' }, sources: { type: 'array', items: { type: 'string' } }, uncertainties: { type: 'array', items: { type: 'string' } } }, required: ['topic', 'summary', 'impl_notes'] }
const BUILD_SCHEMA = { type: 'object', properties: { summary: { type: 'string' }, files_changed: { type: 'array', items: { type: 'string' } }, tests_added: { type: 'array', items: { type: 'string' } }, ruff_passed: { type: 'boolean' }, pytest_summary: { type: 'string' }, blocked: { type: 'array', items: { type: 'string' } } }, required: ['summary', 'files_changed', 'ruff_passed', 'pytest_summary'] }
const REVIEW_SCHEMA = { type: 'object', properties: { findings: { type: 'array', items: { type: 'object', properties: { title: { type: 'string' }, file: { type: 'string' }, line: { type: 'number' }, severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] }, kind: { type: 'string' }, description: { type: 'string' }, suggestion: { type: 'string' } }, required: ['title', 'file', 'severity', 'description'] } } }, required: ['findings'] }
const QA_SCHEMA = { type: 'object', properties: { verdict: { type: 'string', enum: ['PASS', 'FAIL'] }, evidence: { type: 'array', items: { type: 'string' } }, failures: { type: 'array', items: { type: 'string' } } }, required: ['verdict', 'evidence'] }
const REPORT_SCHEMA = { type: 'object', properties: { markdown: { type: 'string' } }, required: ['markdown'] }

// --- Phase 1: Plan ---
phase('Plan')
const plan = await agent(
  `You are the planner. The product owner's directive is:\n\n"${directive}"\n\nProduce a concrete, file-level implementation spec for the Nutti project.`,
  { label: 'plan', phase: 'Plan', model: 'opus', agentType: 'nutti-planner', schema: PLAN_SCHEMA }
)

// --- Phase 2: Research (only if the plan flagged topics) ---
let research = []
const topics = (plan.research_topics || []).filter((t) => t && t.trim())
if (topics.length) {
  phase('Research')
  research = (await parallel(
    topics.map((t) => () =>
      agent(`Research this for the Nutti integration work: ${t}`, { label: `research:${t.slice(0, 24)}`, phase: 'Research', model: 'sonnet', agentType: 'nutti-researcher', schema: RESEARCH_SCHEMA })
    )
  )).filter(Boolean)
}

const planText = JSON.stringify(plan)
const researchText = research.length ? JSON.stringify(research) : '(none)'

// --- Phase 3: Build ---
phase('Build')
const build = await agent(
  `You are the developer. Implement this plan for the Nutti project. Follow the dry_run contract and keep ruff + pytest green.\n\nPLAN:\n${planText}\n\nRESEARCH FINDINGS:\n${researchText}\n\nPO DIRECTIVE: ${directive}`,
  { label: 'build', phase: 'Build', model: 'sonnet', agentType: 'nutti-developer', schema: BUILD_SCHEMA }
)

// --- Phase 4-5: Review <-> Fix loop (bounded) ---
const allFindings = []
let round = 0
let open = []
do {
  round += 1
  const review = await agent(
    `You are the reviewer. Review the changes just made for the directive "${directive}". Focus on correctness, security, robustness, and test adequacy. Read the actual changed files (git diff origin/dev...HEAD).`,
    { label: `review:r${round}`, phase: 'Review', model: 'sonnet', agentType: 'nutti-reviewer', schema: REVIEW_SCHEMA }
  )
  open = (review.findings || []).filter((f) => f.severity === 'critical' || f.severity === 'high')
  allFindings.push(...(review.findings || []))
  if (open.length) {
    await agent(
      `You are the developer. Fix these confirmed review findings, then re-run ruff + pytest until green:\n${JSON.stringify(open)}`,
      { label: `fix:r${round}`, phase: 'Fix', model: 'sonnet', agentType: 'nutti-developer', schema: BUILD_SCHEMA }
    )
  }
} while (open.length && round < 2)

// --- Phase 6: QA ---
phase('QA')
const qa = await agent(
  `You are QA. Verify the implementation for the directive "${directive}" against these acceptance criteria: ${JSON.stringify(plan.acceptance_criteria)}. Run ruff + pytest and any dry_run smoke. Report PASS/FAIL with evidence.`,
  { label: 'qa', phase: 'QA', model: 'sonnet', agentType: 'nutti-qa', schema: QA_SCHEMA }
)

// --- Phase 7: Report ---
phase('Report')
const report = await agent(
  `You are the reporter. Write a Korean 작업 보고 + 다음 Todo for what was just done for the directive "${directive}". Use git diff/log for evidence and incorporate the QA verdict: ${JSON.stringify(qa)}. Return markdown only (do not write to Notion).`,
  { label: 'report', phase: 'Report', model: 'haiku', agentType: 'nutti-reporter', schema: REPORT_SCHEMA }
)

log(`nutti-team done: review rounds=${round}, high/crit findings handled=${allFindings.filter((f) => f.severity === 'critical' || f.severity === 'high').length}, QA=${qa.verdict}`)

return {
  directive,
  plan,
  research,
  build,
  review_findings: allFindings,
  qa,
  report_markdown: report.markdown,
}
