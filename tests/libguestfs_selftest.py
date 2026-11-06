#!/usr/bin/env python3
"""Unprivileged, project-confined libguestfs appliance preflight.

Run only with explicit approval. This script never attaches a disk image.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / '.sdd-test-sandbox'


def private_dir(path, parent):
    if path.is_symlink() or path.parent.resolve(strict=True) != parent:
        raise RuntimeError(f'Unsafe test directory: {path}')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or path.resolve(strict=True) != path
            or info.st_dev != parent.stat().st_dev):
        raise RuntimeError(f'Test directory is not private and project-local: {path}')
    return path


def main():
    if os.geteuid() == 0:
        raise RuntimeError('Run unprivileged')
    if SANDBOX.is_symlink() or not SANDBOX.is_dir() or SANDBOX.resolve(strict=True) != SANDBOX:
        raise RuntimeError('Test sandbox must be a real project-local directory')
    if shutil.disk_usage(SANDBOX).free < 5 * 1024**3:
        raise RuntimeError('At least 5 GiB free is required')
    runtime = private_dir(SANDBOX / 'r', SANDBOX)
    if len(str(runtime)) > 70:
        raise RuntimeError('Runtime path is too long for libguestfs sockets')
    case = Path(tempfile.mkdtemp(prefix='libguestfs-preflight-', dir=SANDBOX))
    if case.is_symlink() or case.stat().st_uid != os.getuid() or stat.S_IMODE(case.stat().st_mode) != 0o700:
        raise RuntimeError('Preflight case is not private')
    names = {'TMPDIR': 'tmp', 'LIBGUESTFS_TMPDIR': 'guestfs-tmp',
             'LIBGUESTFS_CACHEDIR': 'guestfs-cache', 'HOME': 'home',
             'XDG_CACHE_HOME': 'xdg-cache', 'XDG_CONFIG_HOME': 'xdg-config',
             'XDG_DATA_HOME': 'xdg-data'}
    env = os.environ.copy()
    for name, child in names.items():
        env[name] = str(private_dir(case / child, case))
    env['TMP'] = env['TEMP'] = env['TMPDIR']
    env['XDG_RUNTIME_DIR'] = str(runtime)
    env['LIBGUESTFS_BACKEND'] = 'direct'
    print(f'Preflight files: {case}', flush=True)
    print(f'Runtime sockets: {runtime}', flush=True)
    result = subprocess.run(['libguestfs-test-tool'], cwd=ROOT, env=env,
                            timeout=180, check=False)
    print(f'libguestfs-test-tool exit code: {result.returncode}', flush=True)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
