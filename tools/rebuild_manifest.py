"""Rebuild SHA256SUMS after filling in handover documents; never includes runtime data."""
import hashlib
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.venv', 'venv', 'data', '.nicegui', '__pycache__', '.pytest_cache', 'pytest-of-root'}
rows = []
for p in sorted(ROOT.rglob('*')):
    rel = p.relative_to(ROOT)
    if not p.is_file() or (rel.parts[0] == 'data' or any(x in SKIP - {'data'} for x in rel.parts)):
        continue
    if p.name in {'SHA256SUMS', '.env'} or p.suffix in {'.pyc', '.log', '.zip'}:
        continue
    if p.name.startswith('.env.') and p.name != '.env.example':
        continue
    rows.append(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {rel.as_posix()}')
(ROOT / 'SHA256SUMS').write_text('\n'.join(rows) + '\n', encoding='utf-8')
print(f'SHA256SUMS: {len(rows)} files')
