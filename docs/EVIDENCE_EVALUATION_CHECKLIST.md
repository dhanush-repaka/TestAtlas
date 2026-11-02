# TestAtlas evidence evaluation checklist

This evaluates semantic accuracy; passing implementation tests is a different result.

1. Freeze one real project version, its configuration assumptions, story versions, TestAtlas settings and model version. Run analysis, Story Coverage and case reassessment. Retain every source manifest and original model finding.
2. Select 30–50 criteria with a mix of positive and negative requirements, configuration-dependent behaviour, callers that bypass guards, cross-module helpers, external integrations, missing implementation and implementation missed by initial retrieval. Include supported Python and TypeScript/JavaScript examples. Include ambiguous requirements and deliberately unavailable evidence.
3. Have a developer and a QA reviewer independently label each criterion supported, contradicted or unable to assess, with source locations and stated runtime assumptions. Resolve disagreements before comparing with the model.
4. Record model result, reference result, exact evidence retrieved, evidence missed, known retrieval limitations, human review minutes and whether the result is static-only or backed by an actual test execution.
5. Count false support (model supported where reviewers find contradiction or insufficient evidence), missed evidence, false conflict and correctly uncertain results. Report denominators and sample size; do not hide unknowns by dropping them from the evaluation.
6. Treat any reproducible false support caused by missing context as a release blocker for confidence claims. Diagnose retrieval versus reasoning versus ambiguous requirements separately. Add a reproducible regression, then evaluate again on held-out examples.
7. Decide pilot acceptance thresholds with the team before examining results. This checklist does not invent a universally safe accuracy threshold or certify compliance/release readiness.

Suggested review record per criterion:

- Story/criterion identifier and version:
- Code commit / analysis run:
- Test case identifier and expected behaviour:
- Required code/configuration/integration evidence:
- Retrieved evidence and unresolved context:
- Developer reference result and reasoning:
- Independent QA result and reasoning:
- Agreed reference result:
- Model result / model version:
- False support, false conflict, missed evidence or correct uncertainty:
- Actual execution result, if available (otherwise “not executed”):
- Follow-up and regression reference:

Current status: mechanical retrieval/gating regressions pass. No independent real-project semantic accuracy score has been established by this implementation session.
