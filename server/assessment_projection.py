"""Canonical, non-mutating case assessment projection shared by every consumer."""
from . import test_case_reassessment as triage

PROJECTION_VERSION = 1


def assessment_version(case, documents=None, requirements=None):
    inferred=[]
    if requirements is not None and not case.get('status') and not case.get('reassessment'):
        names=set(case.get('covers') or [])
        inferred=[{key:req.get(key) for key in ('id','status','source','assessed_at','covers','run_id')}
                  for req in requirements if names.intersection(req.get('covers') or [])]
    return triage.fingerprint({"projection_version":PROJECTION_VERSION,
        "case":triage.case_fingerprint(case),
        "documents":triage.documents_fingerprint(documents) if documents is not None else None,
        "inferred":sorted(inferred,key=lambda req:req['id']),
        **{key:case.get(key) for key in ('id','assessed_at','run_id','status','rationale','covers','reassessment')}})


def requirement_version(requirement, documents):
    return triage.fingerprint({'requirement':requirement,
        'documents':triage.documents_fingerprint(documents), 'projection_version':PROJECTION_VERSION})


def requirement_is_stale(requirement, documents):
    doc=next((doc for doc in documents if doc['id']==requirement.get('doc_id')),None)
    if not requirement.get('doc_id'):
        return bool(requirement.get('retired'))
    if not doc:
        return True
    recorded=requirement.get('document_fingerprint')
    if recorded:
        return recorded != triage.documents_fingerprint([doc]) or bool(requirement.get('retired'))
    # No fabricated snapshot for legacy rows: an edited document invalidates
    # a judgment predating that edit; source freshness remains explicitly unrecorded.
    return doc.get('updated_at','') > (requirement.get('assessed_at') or requirement.get('created_at') or '')


def project(case):
    report = triage.display_report(case['reassessment']) if case.get('reassessment') else None
    if report:
        outcome = report['outcome']
        state = 'supported' if outcome == 'aligned' else 'inconclusive' if outcome in ('needs_clarification','unvalidated_conflict_claims') else 'conflict'
        label = report['outcome_label']
    else:
        outcome = None
        state = 'unassessed' if not case.get('status') else 'insufficient' if not case.get('covers') else {'supported':'supported','contradicted':'conflict'}.get(case['status'],'inconclusive')
        label = {'supported':'AI-supported','conflict':'Potential conflict','inconclusive':'Inconclusive','insufficient':'Insufficient evidence','unassessed':'Not assessed'}[state]
    level = {'supported':'ai_supported','conflict':'ai_conflict','inconclusive':'inconclusive'}.get(state,'insufficient_evidence')
    if case.get('assessment_kind') == 'inferred_from_requirement':
        label = 'Inferred support' if state == 'supported' else 'Inferred conflict' if state == 'conflict' else 'Inferred: ' + label
        level = 'inconclusive'
    reviewable = state != 'unassessed' and case.get('assessment_kind') != 'inferred_from_requirement'
    agreement = {'supported':'confirmed_supported','conflict':'confirmed_contradicted'}.get(state,'agreed_uncertain') if reviewable else None
    return {'version':case.get('_recorded_assessment_version') or (case.get('assessment') or {}).get('version') or assessment_version(case), 'state':state, 'label':label, 'outcome':outcome,
            'ai_level':level, 'agreement_decision':agreement, 'reviewable':reviewable,
            'semantic_verification':'not_independently_verified', 'report':report}
