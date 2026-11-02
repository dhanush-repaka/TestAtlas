"""Evidence validation and conservative three-way triage. Never edits test content."""
from collections import Counter
import hashlib
import json

from kg.doc_gaps import known_code_names, canonical_cover
from kg.source_evidence import SourceEvidence, enforce_context


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def documents_fingerprint(docs):
    return fingerprint(sorted([{'id': d['id'], 'name': d.get('name'), 'content': d.get('content')} for d in docs], key=lambda d: d['id']))


def case_fingerprint(case):
    values = {k: case.get(k) for k in ('title', 'description', 'preconditions', 'steps', 'expected_result', 'source_doc_id', 'source_requirement_id', 'source_journey_id', 'created_via')}
    values['created_via'] = values['created_via'] or 'generated'
    # Older requirement-generated cases resolve their document at read time.
    if values['source_requirement_id']:
        values['source_doc_id'] = None
    return fingerprint(values)


def ambiguous_code_names(context):
    counts = Counter()
    for module in context.get('modules', []):
        for file in module.get('files', []):
            for name in file.get('functions', []):
                counts[name] += 1
            for cls in file.get('classes', []):
                counts[cls['name']] += 1
                for method in cls.get('methods', []):
                    counts[cls['name'] + '.' + method] += 1
    return {name for name, count in counts.items() if count > 1}


def case_texts(case):
    texts = [case.get(k) for k in ('title','description','preconditions','expected_result')]
    for step in case.get('steps') or []:
        texts.extend([step.get('action'),step.get('expected')] if isinstance(step,dict) else [step])
    return [text for text in texts if isinstance(text,str)]


def validate_conflict(entry, dimension, case, citations):
    """Require an inspectable contrast; citation existence alone is not a conflict."""
    conflict = entry.get('conflict')
    if not isinstance(conflict,dict):
        return None
    left,right,difference = (conflict.get(k) for k in ('left_quote','right_quote','difference'))
    if not all(isinstance(v,str) and v.strip() for v in (left,right,difference)):
        return None
    left_pool = case_texts(case) if dimension != 'implementation_alignment' else [c['quote'] for c in citations if c['kind']=='story']
    right_pool = [c['quote'] for c in citations if c['kind']==('story' if dimension=='requirement_alignment' else 'code')]
    if not any(left in value for value in left_pool) or not any(right in value for value in right_pool):
        return None
    normalise = lambda text: ''.join(ch.lower() for ch in text if ch.isalnum())
    if normalise(left)==normalise(right):
        return None
    return {'left_quote':left,'right_quote':right,'difference':difference.strip()}


def validate_report(raw, case, context, sources):
    """Quotes must occur in supplied evidence; names alone cannot establish behaviour."""
    docs = {d['id']: d for d in context.get('documents', [])}
    source_doc_id = case.get('source_doc_id') or case.get('assessment_source_doc_id')
    if source_doc_id:
        docs = {k: d for k, d in docs.items() if k == source_doc_id}
    canon = known_code_names(context)
    ambiguous = ambiguous_code_names(context)
    report = {}
    for dimension in ('requirement_alignment', 'code_evidence', 'implementation_alignment'):
        entry = raw.get(dimension) or {}
        if not isinstance(entry, dict):
            entry = {}
        status = entry.get('status')
        if status not in ('supported', 'contradicted', 'unknown'):
            status = 'unknown'
        citations = []
        supplied_citations = entry.get('citations')
        for citation in supplied_citations if isinstance(supplied_citations, list) else []:
            if not isinstance(citation, dict):
                continue
            quote = citation.get('quote')
            if not isinstance(quote, str) or not quote.strip():
                continue
            if citation.get('kind') == 'story':
                doc_id = citation.get('id')
                doc = docs.get(doc_id) if isinstance(doc_id, str) else None
                if doc and quote in (doc.get('content') or ''):
                    citations.append({'kind': 'story', 'id': doc['id'], 'quote': quote})
            elif citation.get('kind') == 'code':
                name = canonical_cover(citation.get('id'), canon) if isinstance(citation.get('id'), str) else None
                # The source dictionary uses the same qualified names as source_lookup.
                if name and name not in ambiguous and quote in sources.get(name, ''):
                    citations.append({'kind': 'code', 'id': name, 'quote': quote, **(sources.symbols.get(name,{}) if isinstance(sources,SourceEvidence) else {})})
        citations = ([c for c in citations if c['kind'] == 'story'][:6] +
                     [c for c in citations if c['kind'] == 'code'][:6])
        kinds = {c['kind'] for c in citations}
        needed = {'story'} if dimension == 'requirement_alignment' else {'code'} if dimension == 'code_evidence' else {'story', 'code'}
        missing_evidence = not needed.issubset(kinds)
        if missing_evidence:
            status = 'unknown'
        if dimension != 'requirement_alignment':
            status = enforce_context(status, raw, sources)
        model_status = entry.get('status')
        conflict = validate_conflict(entry, dimension, case, citations) if model_status == 'contradicted' else None
        validation_note = None
        if model_status == 'contradicted' and conflict is None:
            status = 'unknown'
            validation_note = 'The proposed contradiction did not identify valid opposing expectations. Reassessment is required.'
        rationale = entry.get('rationale')
        report[dimension] = {'status': status, 'rationale': ('Insufficient validated evidence. ' if missing_evidence else '') + (rationale.strip() if isinstance(rationale, str) else ''), 'citations': citations[:12], 'model_status':model_status, 'conflict':conflict, 'validation_note':validation_note}
    if isinstance(sources, SourceEvidence):
        report['source_context'] = sources.manifest()
        report['context_sufficient'] = raw.get('context_sufficient') is True
        report['missing_context'] = [v for v in raw.get('missing_context', []) if isinstance(v,str)] if isinstance(raw.get('missing_context'),list) else ['Model did not report context sufficiency']
    report['documents_fingerprint'] = documents_fingerprint(context.get('documents', []))
    report['case_fingerprint'] = case_fingerprint(case)
    report['sources_fingerprint'] = fingerprint(sources.manifest() if isinstance(sources, SourceEvidence) else sources)
    relevant_ids = {citation['id'] for key in ('requirement_alignment', 'implementation_alignment')
                    for citation in report[key]['citations'] if citation['kind'] == 'story'}
    report['story_versions'] = {doc_id: fingerprint(docs[doc_id].get('content')) for doc_id in sorted(relevant_ids)}
    previous = case.get('reassessment') or {}
    prior_versions = previous.get('story_versions') or {}
    changed_story = any(doc_id in prior_versions and prior_versions[doc_id] != version
                        for doc_id, version in report['story_versions'].items())
    report['validation_version'] = 4
    report.update(classify_report(report, changed_story=changed_story,
        previously_aligned=previous.get('requirement_alignment', {}).get('status') == 'supported'))
    return report


DIMENSIONS = ('requirement_alignment', 'code_evidence', 'implementation_alignment')
DIMENSION_LABELS = {'requirement_alignment':'Test ↔ requirements', 'code_evidence':'Test ↔ code',
                    'implementation_alignment':'Code ↔ requirements'}


# Order: test ↔ requirements, test ↔ code, code ↔ requirements.
# Every combination is explicit; U never cancels a validated C.
COMBINATION_RULES = {
    'SSS': ('aligned', 'All three comparisons align', 'Run the test to validate runtime behaviour; source alignment alone is not execution proof.'),
    'SSC': ('conflicts_found', 'Implementation conflict · comparisons need review', 'The code conflicts with requirements although the test is reported to match both. Check the exact behaviour and comparison scope; preserve the implementation conflict.'),
    'SSU': ('needs_clarification', 'Test aligns · implementation evidence incomplete', 'Test alignment is supported, but direct code-to-requirement evidence is missing. Inspect that comparison before drawing an overall conclusion.'),
    'SCS': ('conflicts_found', 'Test and code conflict · comparisons need review', 'The test and code conflict although both are reported to align with requirements. Review scenario, state and comparison scope; preserve the test-to-code conflict.'),
    'SCC': ('potential_regression', 'Implementation conflicts with the expected behaviour', 'The test matches requirements; code conflicts with both. Investigate the implementation and preserve the required expectation.'),
    'SCU': ('conflicts_found', 'Required test expectation conflicts with code', 'The test matches requirements and conflicts with code. Investigate that mismatch; direct implementation-to-requirement evidence remains unresolved.'),
    'SUS': ('needs_clarification', 'Requirements align · test-to-code evidence missing', 'Both test and code align with the cited requirement, but their direct comparison is unresolved. Assess whether code produces the test expectation.'),
    'SUC': ('conflicts_found', 'Implementation conflicts with requirements', 'The test matches requirements and code conflicts with them. Investigate the implementation; the direct test-to-code comparison remains unresolved.'),
    'SUU': ('needs_clarification', 'Test matches requirements · code assessment incomplete', 'Retain the required expectation. Gather evidence for test-to-code and code-to-requirement comparisons.'),
    'CSS': ('conflicts_found', 'Test requirement conflict · comparisons need review', 'The test conflicts with requirements although code is reported to match both. Review the exact expectations and comparison scope; preserve the test requirement conflict.'),
    'CSC': ('implementation_and_test_conflict', 'Code and test conflict with requirements', 'The test matches current code, but both conflict with the requirement. Review the implementation and test expectation together.'),
    'CSU': ('conflicts_found', 'Test matches code but conflicts with requirements', 'Review the test expectation against requirements. Direct code-to-requirement evidence is unresolved; do not assume its result.'),
    'CCS': ('test_case_needs_update', 'Test expectation conflicts with code and requirements', 'Code matches requirements, while the test conflicts with both. Review a test expectation update against the cited requirement.'),
    'CCC': ('conflicts_found', 'Conflicts found in all three comparisons', 'Review all three opposing expectations. Multiple conflicts do not require clarification by themselves; establish the intended behaviour and address each mismatch.'),
    'CCU': ('conflicts_found', 'Test conflicts with requirements and code', 'Review both test mismatches. Code-to-requirement alignment is unresolved, so do not assume whether code or test should change.'),
    'CUS': ('conflicts_found', 'Test conflicts with requirements · code aligns', 'Review the test requirement conflict. Assess the missing direct test-to-code comparison before attributing that mismatch.'),
    'CUC': ('conflicts_found', 'Test and code each conflict with requirements', 'Review both requirement conflicts independently. Their direct relationship remains unresolved; neither conflict is cancelled by that gap.'),
    'CUU': ('conflicts_found', 'Test conflicts with requirements · code evidence missing', 'Review the test expectation against requirements. Obtain code evidence separately; its absence does not remove the test requirement conflict.'),
    'USS': ('needs_clarification', 'Code aligns · direct test requirement evidence missing', 'Code matches requirements and the test matches code. Assess the test directly against requirements rather than inferring coverage through code.'),
    'USC': ('conflicts_found', 'Code conflicts with requirements · test matches code', 'Investigate the implementation conflict. Assess the test directly against requirements before deciding how its expectation should change.'),
    'USU': ('needs_clarification', 'Test matches code · requirement evidence incomplete', 'Current behaviour matches the test, but its business correctness is unresolved. Gather requirement evidence for both comparisons.'),
    'UCS': ('conflicts_found', 'Test conflicts with code · code matches requirements', 'Review the test-to-code mismatch. Assess the test directly against requirements before deciding whether its expectation is wrong.'),
    'UCC': ('conflicts_found', 'Code conflicts with test and requirements', 'Investigate both implementation mismatches. Direct test-to-requirement evidence remains unresolved; do not assume the test is correct.'),
    'UCU': ('conflicts_found', 'Test conflicts with code · requirements unresolved', 'Investigate the observed expectation mismatch. Gather requirements before deciding which behaviour is intended.'),
    'UUS': ('needs_clarification', 'Code matches requirements · test assessment incomplete', 'Assess the test against requirements and code. Code alignment alone does not establish test sufficiency.'),
    'UUC': ('conflicts_found', 'Code conflicts with requirements · test assessment incomplete', 'Investigate the implementation conflict. Assess the test separately; missing test evidence does not remove the code requirement conflict.'),
    'UUU': ('needs_clarification', 'Insufficient evidence in all three comparisons', 'Gather the required story, code and scenario evidence identified below. No comparison currently supports a definitive conclusion.'),
}


def classify_report(report, *, changed_story=False, previously_aligned=False):
    """Preserve independently validated conflicts even when attribution is incomplete."""
    findings = {key: report.get(key) or {} for key in DIMENSIONS}
    legacy_claims = []
    statuses = {}
    for key, finding in findings.items():
        status = finding.get('status', 'unknown')
        # Old model labels without an inspectable contrast are claims, not validated conflicts.
        if status == 'contradicted' and not finding.get('conflict'):
            legacy_claims.append(key)
            status = 'unknown'
        statuses[key] = status
    conflicts = [key for key in DIMENSIONS if statuses[key] == 'contradicted']
    unresolved = [key for key in DIMENSIONS if statuses[key] not in ('supported','contradicted')]
    questions = [f"{DIMENSION_LABELS[key]}: {findings[key].get('validation_note') or findings[key].get('rationale') or 'No sufficient validated evidence was recorded.'}" for key in unresolved]
    same_requirement = any(a['id'] == b['id'] and (a['quote'] in b['quote'] or b['quote'] in a['quote'])
        for a in findings['requirement_alignment'].get('citations', []) if a['kind'] == 'story'
        for b in findings['implementation_alignment'].get('citations', []) if b['kind'] == 'story')
    if not same_requirement:
        questions.append('The test and implementation findings do not cite the same acceptance criterion; review them separately.')
    combination = ''.join({'supported':'S','contradicted':'C'}.get(statuses[key],'U') for key in DIMENSIONS)
    outcome, label, action = COMBINATION_RULES[combination]
    if not same_requirement:
        outcome = 'conflicts_found' if conflicts else 'needs_clarification'
        label = 'Conflicts found · different comparison scope' if conflicts else 'Comparison scope needs review'
        action = 'Review each comparison independently. They do not cite the same acceptance criterion, so no combined alignment or defect attribution is established.'
    elif combination == 'CCS' and changed_story and previously_aligned:
        outcome, label = 'outdated_test_case', 'Outdated test case'
        action = 'The cited requirement changed since the prior aligned assessment. Review a test update against the current requirement.'
    if legacy_claims and not conflicts:
        outcome = 'unvalidated_conflict_claims'
        label = 'Earlier conflict claims · reassess'
        action = 'Reassess the earlier claims to record validated opposing expectations. This is an assessment-quality gap, not necessarily missing business information.'
    return {'outcome':outcome, 'outcome_label':label, 'recommended_action':action,
            'status_combination':combination, 'conflict_dimensions':conflicts,
            'unresolved_dimensions':unresolved, 'clarification_reasons':questions}



def display_report(report):
    """Reclassify saved evidence without rejudging it or rewriting assessment history."""
    result = {**report, **classify_report(report)}
    if report.get('outcome') == 'outdated_test_case' and result['outcome'] == 'test_case_needs_update':
        result['outcome'] = 'outdated_test_case'
        result['outcome_label'] = 'Outdated test case'
        result['recommended_action'] = 'The cited requirement changed since the prior aligned assessment. Review a test update against the current requirement.'
    if result['outcome'] != report.get('outcome'):
        result['recorded_outcome'] = report.get('outcome')
    return result
