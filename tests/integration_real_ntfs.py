#!/usr/bin/env python3
"""Explicit rootless real-NTFS integration runner; never touches block devices.

Run manually after guestfish, virt-resize, and qemu-img are installed in an
isolated environment. All paths and disposable targets stay under the project
.sdd-test-sandbox directory. This script is intentionally not auto-discovered.
"""
from __future__ import annotations
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sdd

SOURCE_BYTES = 512 * sdd.MIB
TARGET_BYTES = 320 * sdd.MIB
START = 2048
SECTORS = SOURCE_BYTES // 512 - START - 2048


def bios_chs(lba):
    """Encode fixture CHS with the conventional 255-head/63-sector geometry."""
    cylinder, remainder = divmod(lba, 255 * 63)
    if cylinder > 1023:
        return bytes.fromhex('feffff')
    head, sector_zero = divmod(remainder, 63)
    return bytes((head, sector_zero + 1 | ((cylinder >> 2) & 0xc0), cylinder & 0xff))


def command(args, *, answer=None):
    p = subprocess.run(args, input=answer, text=True, capture_output=True, cwd=ROOT)
    if p.returncode:
        raise RuntimeError(f"Command failed ({p.returncode}): {args!r}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
    return p.stdout


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(sdd.CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def guestfish(path, *parts, read_only=False):
    return command(['guestfish', '--ro' if read_only else '--rw', '--format=raw',
                    '-a', str(path), 'run', ':', *map(str, parts)])


def make_mbr(path):
    mbr = bytearray(512)
    mbr[:440] = (b'SDD-TEST-BOOT-CODE-' * 25)[:440]
    mbr[440:444] = b'SDD1'
    mbr[446] = 0x80
    mbr[447:450] = bios_chs(START)
    mbr[450] = 0x07
    mbr[451:454] = bios_chs(START + SECTORS - 1)
    struct.pack_into('<II', mbr, 454, START, SECTORS)
    mbr[510:512] = b'\x55\xaa'
    with open(path, 'xb') as f:
        f.write(mbr)
        f.truncate(SOURCE_BYTES)


def make_files(case):
    data = case/'data'
    data.mkdir()
    files = {
        '/alpha/small.txt': b'SDD real NTFS integration\n',
        '/alpha/medium.bin': bytes(range(256)) * 512,
        '/beta/large.bin': bytes(range(256)) * (16*sdd.MIB//256),
        '/beta/uneven.bin': b'XYZ1234' * 700001,
    }
    manifest = {}
    paths = {}
    for guest_path, content in files.items():
        local = data/guest_path.lstrip('/').replace('/', '_')
        local.write_bytes(content)
        paths[guest_path] = local
        manifest[guest_path] = {'size': len(content), 'sha256': sha(local)}
    (case/'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest, paths


def extract_and_check(image, manifest, case, label):
    output = case/f'extracted-{label}'
    output.mkdir()
    commands = ['mount-ro', '/dev/sda1', '/']
    downloads = {}
    for index, guest_path in enumerate(manifest):
        local = output/f'{index}.bin'
        commands.extend([':', 'download', guest_path, str(local)])
        downloads[guest_path] = local
    guestfish(image, *commands, read_only=True)
    for guest_path, local in downloads.items():
        actual = {'size': local.stat().st_size, 'sha256': sha(local)}
        if actual != manifest[guest_path]:
            raise AssertionError(f'Content mismatch for {guest_path}: {actual}')
    return {name: sha(path) for name, path in downloads.items()}


def cleanup_scratch(case, scratch, target):
    """Remove only SDD's known scratch images and diagnostic log."""
    sdd.check_test_file_paths(case/'source.img', scratch, target, case)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    case_fd = os.open(case, flags)
    try:
        scratch_fd = os.open(scratch.name, flags, dir_fd=case_fd)
        try:
            children = os.listdir(scratch_fd)
            if len(children) != 1 or not children[0].startswith('sdd-'):
                raise AssertionError('Unexpected scratch contents; retaining for review')
            work = children[0]
            work_fd = os.open(work, flags, dir_fd=scratch_fd)
            try:
                if set(os.listdir(work_fd)) != {'source.img', 'fitted.img', 'operations.log'}:
                    raise AssertionError('Unexpected work files; retaining for review')
                for name in ('source.img', 'fitted.img', 'operations.log'):
                    st = os.stat(name, dir_fd=work_fd, follow_symlinks=False)
                    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
                        raise AssertionError('Unsafe scratch file; retaining for review')
                for name in ('source.img', 'fitted.img', 'operations.log'):
                    os.unlink(name, dir_fd=work_fd)
            finally:
                os.close(work_fd)
            os.rmdir(work, dir_fd=scratch_fd)
        finally:
            os.close(scratch_fd)
        os.rmdir(scratch.name, dir_fd=case_fd)
    finally:
        os.close(case_fd)


def main():
    if os.geteuid() == 0:
        raise SystemExit('Run unprivileged; test-file mode refuses root')
    missing = [x for x in ('guestfish', 'virt-resize', 'qemu-img') if shutil.which(x) is None]
    if missing:
        raise SystemExit('SKIP: missing isolated-environment tools: ' + ', '.join(missing))
    sandbox = ROOT/'.sdd-test-sandbox'
    if sandbox.is_symlink():
        raise SystemExit('Refusing symlinked test sandbox')
    sandbox.mkdir(exist_ok=True)
    if not sandbox.is_dir() or sandbox.resolve(strict=True) != sandbox:
        raise SystemExit('Test sandbox must be a real project directory')
    # libguestfs puts short-lived socket/PID files under XDG_RUNTIME_DIR.
    # Keep this path short enough for Unix-domain socket pathname limits.
    runtime = sandbox/'r'
    if runtime.is_symlink():
        raise SystemExit('Refusing symlinked test runtime directory')
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime_stat = runtime.stat()
    if (not stat.S_ISDIR(runtime_stat.st_mode) or runtime_stat.st_uid != os.getuid()
            or runtime_stat.st_mode & 0o077 or runtime.resolve(strict=True) != runtime
            or runtime_stat.st_dev != sandbox.stat().st_dev or len(str(runtime)) > 70):
        raise SystemExit('Test runtime directory must be private and project-local')
    if shutil.disk_usage(sandbox).free < 5 * sdd.GIB:
        raise SystemExit('At least 5 GiB free is required for this bounded integration test')
    case = Path(tempfile.mkdtemp(prefix='real-ntfs-', dir=sandbox))
    os.environ['LIBGUESTFS_BACKEND'] = 'direct'
    os.environ['XDG_RUNTIME_DIR'] = str(runtime)
    for name, subdir in [('TMPDIR', 'tmp'), ('LIBGUESTFS_TMPDIR', 'guestfs-tmp'),
                         ('LIBGUESTFS_CACHEDIR', 'guestfs-cache'), ('HOME', 'home'),
                         ('XDG_CACHE_HOME', 'xdg-cache'), ('XDG_CONFIG_HOME', 'xdg-config'),
                         ('XDG_DATA_HOME', 'xdg-data')]:
        directory = case/subdir
        directory.mkdir()
        os.environ[name] = str(directory)
    os.environ['TMP'] = os.environ['TMPDIR']
    os.environ['TEMP'] = os.environ['TMPDIR']
    print('Disposable test directory:', case, flush=True)
    try:
        raw = case/'source.img'
        make_mbr(raw)
        guestfish(raw, 'mkfs', 'ntfs', '/dev/sda1')
        manifest, paths = make_files(case)
        commands = ['mount', '/dev/sda1', '/', ':', 'mkdir', '/alpha', ':', 'mkdir', '/beta']
        for guest_path, local in paths.items():
            commands.extend([':', 'upload', str(local), guest_path])
        guestfish(raw, *commands)
        source_layout = sdd.image_info(raw)
        source_hash = sha(raw)
        extract_and_check(raw, manifest, case, 'source')
        raw.chmod(0o400)
        gz = case/'source.img.gz'
        with open(raw, 'rb') as source, gzip.open(gz, 'wb', compresslevel=1) as compressed:
            for block in iter(lambda: source.read(sdd.CHUNK), b''):
                compressed.write(block)
        gzip_hash = sha(gz)
        gz.chmod(0o400)
        results = {}
        for label, source in [('raw', raw), ('gzip', gz)]:
            target = case/f'target-{label}.img'
            with open(target, 'xb') as f:
                f.write(b'OLD-TARGET')
                f.truncate(TARGET_BYTES)
            scratch = case/f'scratch-{label}'
            scratch.mkdir(mode=0o700)
            base = [sys.executable, str(ROOT/'sdd.py'), 'restore', str(source), str(target),
                    '--scratch', str(scratch), '--test-file-target', '--test-case-dir', str(case)]
            command([sys.executable, str(ROOT/'sdd.py'), 'inspect', str(source)])
            command([*base, '--dry-run'])
            command(base, answer='y\n')
            output_layout = sdd.image_info(target)
            sdd.assert_preserved(source_layout, output_layout)
            if output_layout['bytes'] != TARGET_BYTES or output_layout['start'] + output_layout['sectors'] > TARGET_BYTES//512:
                raise AssertionError('Fitted partition does not fit target')
            checksums = extract_and_check(target, manifest, case, label)
            if sha(raw) != source_hash or sha(gz) != gzip_hash:
                raise AssertionError('Source image changed')
            results[label] = {'target_sha256': sha(target), 'files': checksums,
                              'partition_sectors': output_layout['sectors']}
            cleanup_scratch(case, scratch, target)
        report = {'source_sha256': source_hash, 'gzip_sha256': gzip_hash,
                  'manifest': manifest, 'results': results,
                  'guestfish_version': command(['guestfish', '--version']).strip(),
                  'virt_resize_version': command(['virt-resize', '--version']).strip(),
                  'qemu_img_version': command(['qemu-img', '--version']).splitlines()[0]}
        (case/'results.json').write_text(json.dumps(report, indent=2) + '\n')
        print('PASS: real NTFS raw and gzip restore, all file SHA-256 checks matched.')
        print('Results:', case/'results.json')
    except BaseException:
        print('FAIL: test data retained for review at', case, file=sys.stderr)
        raise


if __name__ == '__main__':
    main()
