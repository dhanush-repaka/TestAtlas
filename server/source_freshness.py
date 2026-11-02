"""Check recorded file hashes at read/review boundaries; no background watcher."""
import hashlib
from pathlib import Path


def check_source_context(context, source_root, cache=None):
    if not context or not context.get('files'):
        return {'state':'unrecorded','scope':'recorded_evidence_files','changed_files':[],
                'message':'No source hashes recorded. Freshness against current files is unknown; reassess.'}
    if source_root is None:
        return {'state':'not_checked','scope':'recorded_evidence_files','changed_files':[],
                'message':'Source files were not checked at this boundary.'}
    cache = {} if cache is None else cache
    root = Path(source_root).resolve()
    changed=[]
    for entry in context['files']:
        name=entry.get('path','')
        path=(root/name).resolve()
        if not name or not path.is_relative_to(root) or not entry.get('sha256'):
            changed.append(name or '(invalid recorded path)')
            continue
        key=str(path)
        if key not in cache:
            try:
                # A changed oversized file must not cause an unbounded read.
                cache[key]=hashlib.sha256(path.read_text(encoding='utf-8').encode()).hexdigest() if path.stat().st_size <= 2_000_000 else None
            except (OSError, UnicodeError):
                cache[key]=None
        if cache[key] != entry['sha256']:
            changed.append(name)
    return {'state':'changed' if changed else 'unchanged', 'scope':'recorded_evidence_files',
            'changed_files':changed, 'message': 'Recorded evidence files changed or became unavailable. Run analysis and reassess.' if changed else
            'Recorded evidence files match their saved hashes. Unexamined files, dependencies and runtime settings are not verified.'}
