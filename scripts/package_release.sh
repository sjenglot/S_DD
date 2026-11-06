#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
output="${project_dir}/dist/S_DD-v1.1.0-source.zip"

python3 - "$project_dir" "$output" <<'PY'
from pathlib import Path
import os
import stat
import sys
import tempfile
import zipfile

root = Path(sys.argv[1]).resolve(strict=True)
output = Path(sys.argv[2])
files = (
    'sdd.py', 'sdd-shrink.py', 'pyproject.toml', 'README.md', 'LICENSE', 'IMPLEMENTATION_REPORT.md',
    'CHANGELOG.md', 'CONTRIBUTING.md', '.gitignore',
    'tests/test_cli.py', 'tests/integration_real_ntfs.py', 'tests/libguestfs_selftest.py',
    'docs/ARCHITECTURE.md', 'docs/HARDWARE_TEST.md', 'docs/TROUBLESHOOTING.md',
    'docs/UBUNTU_FIRST_RUN.md', 'docs/UBUNTU_QUICK_REFERENCE.md',
    'scripts/package_release.sh', 'scripts/install_user.sh', 'scripts/install_system.sh',
)
for relative in files:
    path = root / relative
    if path.is_symlink() or path.resolve(strict=True) != path or not stat.S_ISREG(path.stat().st_mode):
        raise SystemExit(f'Unsafe package input: {relative}')
    if str(root).encode() in path.read_bytes():
        raise SystemExit(f'Host-specific project path found in {relative}')

if output.parent.is_symlink():
    raise SystemExit('Refusing symlinked release directory')
output.parent.mkdir(exist_ok=True)
if output.parent.resolve(strict=True).parent != root:
    raise SystemExit('Release directory escaped project')
fd, temporary = tempfile.mkstemp(prefix='.s_dd_release-', suffix='.zip', dir=output.parent)
os.close(fd)
try:
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in files:
            name = 'S_DD-v1.1.0/' + relative
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((0o755 if relative.endswith('.sh') else 0o644) << 16)
            archive.writestr(info, (root / relative).read_bytes(), compress_type=zipfile.ZIP_DEFLATED,
                             compresslevel=9)
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(files):
            raise SystemExit('Release ZIP validation failed')
        if any(name.startswith('/') or '..' in Path(name).parts for name in archive.namelist()):
            raise SystemExit('Unsafe release ZIP member')
    os.replace(temporary, output)
    os.chmod(output, 0o644)
except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise
print(output)
print(f'{len(files)} reviewed files; ZIP integrity PASS')
PY
