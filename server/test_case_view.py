"""Shared test-suite view metadata and export filtering; no AI or execution."""
from . import evidence_rules, test_case_reassessment


def enrich_cases(cases: list[dict], latest_run_id: str | None, documents=None) -> list[dict]:
    result = []
    for case in cases:
        stale = evidence_rules.is_stale(case, latest_run_id)
        if case.get('assessment_kind') == 'inferred_from_requirement':
            stale = stale or evidence_rules.is_stale({'run_id': case.get('inferred_run_id')}, latest_run_id)
        report = case.get('reassessment')
        if report:
            stale = stale or report.get('case_fingerprint') != test_case_reassessment.case_fingerprint(case)
            if documents is not None:
                stale = stale or report.get('documents_fingerprint') != test_case_reassessment.documents_fingerprint(documents)
        result.append({**case, 'is_stale': stale, **({'reassessment':test_case_reassessment.display_report(report)} if report else {})})
    return result


def assessment_state(case: dict) -> str:
    if case.get('is_stale'):
        return 'stale'
    if case.get('reassessment'):
        outcome = case['reassessment']['outcome']
        return 'supported' if outcome == 'aligned' else 'inconclusive' if outcome in ('needs_clarification', 'unvalidated_conflict_claims') else 'conflict'
    if not case.get('status'):
        return 'unassessed'
    if not case.get('covers'):
        return 'insufficient'
    return {'supported': 'supported', 'contradicted': 'conflict'}.get(case['status'], 'inconclusive')


def filter_cases(cases: list[dict], *, module='', category='', source='', story='', q='', assessment='') -> list[dict]:
    result = []
    for case in cases:
        imported = case.get('created_via') == 'imported'
        method = ('imported' if imported else 'journey' if case.get('source_journey_id') else
                  'story' if case.get('source_requirement_id') or case.get('source_doc_id') else 'code')
        search = ' '.join(str(v) for v in [case.get('title'), case.get('description'), case.get('target'),
                                         case.get('module'), *(case.get('covers') or [])] if v).lower()
        state = assessment_state(case)
        if ((not module or case.get('module') == module) and
            (not category or case.get('category') == category) and
            (not source or (not imported if source == 'generated' else method == source)) and
            (not story or case.get('source_doc_id') == story) and
            (not q or q.strip().lower() in search) and
            (not assessment or ((case.get('review_state') or {}).get('state') == 'disputed' if assessment == 'disputed' else state in ('insufficient', 'inconclusive', 'unassessed') if assessment == 'insufficient' else state == assessment))):
            result.append(case)
    return result


def review_state(case: dict, review: dict | None) -> dict:
    """A reviewer decision is independent of, and may predate, the AI assessment."""
    if not review:
        return {'state':'unreviewed', 'label':'Not reviewed'}
    from datetime import datetime
    try:
        earlier = bool(case.get('assessed_at') and datetime.fromisoformat(review['reviewed_at']) < datetime.fromisoformat(case['assessed_at']))
    except (KeyError, ValueError, TypeError):
        earlier = False
    if earlier:
        return {'state':'previous_assessment', 'label':'Review belongs to an earlier assessment'}
    states = {'rejected_ai_judgment':('disputed','Disputed by reviewer'), 'needs_more_info':('needs_information','Reviewer needs more information'),
              'agreed_uncertain':('confirmed','Reviewer agrees evidence is insufficient'),
              'confirmed_supported':('confirmed','Reviewer confirmed support'), 'confirmed_contradicted':('confirmed','Reviewer confirmed conflict')}
    state,label = states.get(review.get('decision'),('unreviewed','Not reviewed'))
    return {'state':state,'label':label}
