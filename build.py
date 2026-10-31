"""Build reproducible manual-install ZIP without personal state or bytecode."""
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

root = Path(__file__).resolve().parent
component = root / 'custom_components' / 'jomoo_zs800i'
version = json.loads((component / 'manifest.json').read_text(encoding='utf-8'))['version']
dist = root / 'dist'
dist.mkdir(exist_ok=True)
output = dist / f'jomoo_zs800i-{version}.zip'
with ZipFile(output, 'w', compression=ZIP_DEFLATED) as zf:
    for path in sorted(component.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix not in {'.py', '.json'}:
            continue
        info = ZipInfo(path.relative_to(root).as_posix(), (2026, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        zf.writestr(info, path.read_bytes())
checksum = hashlib.sha256(output.read_bytes()).hexdigest()
output.with_suffix('.zip.sha256').write_text(f'{checksum}  {output.name}\n', encoding='ascii')
print(output)
print(checksum)
