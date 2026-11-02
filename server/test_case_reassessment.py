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
    r, c, i = (report[k]['status'] for k in ('requirement_alignment', 'code_evidence', 'implementation_alignment'))
    same_requirement = any(a['id'] == b['id'] and (a['quote'] in b['quote'] or b['quote'] in a['quote'])
                           for a in report['requirement_alignment']['citations'] if a['kind'] == 'story'
                           for b in report['implementation_alignment']['citations'] if b['kind'] == 'story')
    if not same_requirement:
        outcome = 'needs_clarification'
    elif r == 'supported' and c == 'contradicted' and i == 'contradicted':
        outcome = 'potential_regression'
    elif r == 'contradicted' and c == 'contradicted' and i == 'supported':
        outcome = 'outdated_test_case' if changed_story and previous.get('requirement_alignment', {}).get('status') == 'supported' else 'test_case_needs_update'
    elif r == c == i == 'supported':
        outcome = 'aligned'
    else:
        outcome = 'needs_clarification'
    report['validation_version'] = 2
    report['outcome'] = outcome
    return report
