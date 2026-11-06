#!/usr/bin/env python3
"""SDD: scratch-assisted restore of a single MBR/NTFS disk image."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time

VERSION = '1.1.0'
CHUNK = 4 * 1024 * 1024
MIB = 1024 * 1024
GIB = 1024 * MIB
TEST_ROOT = Path(__file__).resolve().parent / '.sdd-test-sandbox'
TEST_MAX_SOURCE = 2 * GIB

class SddError(Exception):
    pass


class Cancelled(SddError):
    """A user declined before the destination was touched."""


def run(cmd, *, input=None):
    p = subprocess.run(cmd, input=input, text=True, capture_output=True)
    if p.returncode:
        raise SddError(f"{' '.join(map(str, cmd))} failed ({p.returncode}): {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def run_live(cmd, *, label=None, log_path=None):
    return run_with_status(cmd, label=label, log_path=log_path)


def run_with_status(cmd, *, label=None, log_path=None):
    """Run a utility with elapsed-time status; capture its full output in scratch."""
    label = label or Path(str(cmd[0])).name
    started = time.monotonic()
    if log_path is None:
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, text=True, capture_output=True)
        if result.returncode:
            raise SddError(f"{label} failed ({result.returncode}): {(result.stderr or result.stdout).strip()}")
        return
    with open(log_path, 'ab') as log:
        log.write(('\nCOMMAND: ' + ' '.join(map(str, cmd)) + '\n').encode())
        log.flush()
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            tick = 0
            while True:
                try:
                    code = proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    activity(label, started, tick)
                    tick += 1
        except BaseException:
            stop_child(proc)
            clear_activity()
            raise
    clear_activity()
    elapsed = time.monotonic() - started
    if code:
        with open(log_path, 'rb') as log:
            log.seek(max(0, log.seek(0, os.SEEK_END) - 4000))
            detail = log.read().decode(errors='replace').strip()
        raise SddError(f'{label} failed ({code}) after {elapsed:.0f}s; log: {log_path}\n{detail}')
    print(f'{label}: complete in {elapsed:.0f}s (log: {log_path})', flush=True)


def stop_child(proc):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def run_with_output_status(cmd, *, label, log_path=None, touched=False):
    """Capture a long query's output while showing elapsed time."""
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        tick = 0
        while True:
            try:
                out, err = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                activity(label, started, tick, touched=touched)
                tick += 1
    except BaseException:
        stop_child(proc)
        clear_activity()
        raise
    clear_activity()
    if log_path:
        with open(log_path, 'a') as log:
            log.write('\nCOMMAND: ' + ' '.join(map(str, cmd)) + '\n' + out + err)
    if proc.returncode:
        raise SddError(f'{label} failed ({proc.returncode}): {(err or out).strip()[-4000:]}')
    print(f'{label}: complete in {time.monotonic()-started:.0f}s', flush=True)
    return out


def require(*names):
    missing = [x for x in names if shutil.which(x) is None]
    if missing:
        raise SddError('Missing required tools: ' + ', '.join(missing))


def physical_size(path):
    return int(run(['blockdev', '--getsize64', str(path)]).strip())


def parse_mbr(sector, image_size):
    if len(sector) < 512 or sector[510:512] != b'\x55\xaa':
        raise SddError('Missing MBR signature or truncated image')
    entries = []
    for i in range(4):
        e = sector[446+i*16:462+i*16]
        boot, typ = e[0], e[4]
        start, count = struct.unpack_from('<II', e, 8)
        if typ or start or count or boot:
            entries.append((boot, typ, start, count, e[1:4].hex(), e[5:8].hex()))
    if len(entries) != 1:
        raise SddError('Only exactly one primary MBR partition is supported')
    boot, typ, start, count, start_chs, end_chs = entries[0]
    if boot not in (0, 0x80) or typ != 0x07 or start < 1 or count < 1:
        raise SddError('Unsupported boot flag, partition type, or geometry')
    if (start + count) * 512 > image_size:
        raise SddError('Partition extends past image end')
    return {'boot': boot, 'type': typ, 'start': start, 'sectors': count,
            'start_chs': start_chs, 'end_chs': end_chs,
            'signature': sector[440:444].hex(), 'boot_code': sector[:440]}


def parse_ntfs(boot, sectors, start=None):
    if len(boot) < 512 or boot[3:11] != b'NTFS    ' or boot[510:512] != b'\x55\xaa':
        raise SddError('Partition does not have an NTFS boot sector')
    bps = struct.unpack_from('<H', boot, 11)[0]
    hidden = struct.unpack_from('<I', boot, 28)[0]
    total = struct.unpack_from('<Q', boot, 40)[0]
    if bps != 512 or not total or total > sectors:
        raise SddError('Unsupported NTFS sector size or invalid NTFS length')
    if start is not None and hidden != start:
        raise SddError('NTFS hidden-sector boot metadata disagrees with partition start')
    return {'ntfs_sectors': total, 'ntfs_serial': boot[72:80].hex(), 'ntfs_hidden': hidden}


def assert_preserved(source, result):
    for key in ('boot', 'type', 'start', 'start_chs', 'signature', 'boot_code', 'ntfs_serial', 'ntfs_hidden'):
        if result[key] != source[key]:
            raise SddError(f'Fitted image changed boot-critical field: {key}')


def image_info(path, max_bytes=None):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.suffix not in ('.img', '.gz'):
        raise SddError('Source must be a regular .img or .img.gz file')
    if path.suffix == '.gz' and not path.name.endswith('.img.gz'):
        raise SddError('Compressed source must end in .img.gz')
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise SddError('Test source exceeds 2 GiB limit')
    opener = gzip.open if path.suffix == '.gz' else open
    count = 0
    digest = hashlib.sha256() if path.suffix == '.gz' else None
    header = bytearray()
    tail = b''
    t0 = time.monotonic()
    try:
        if path.suffix == '.img':
            count = path.stat().st_size
            with open(path, 'rb') as f:
                header.extend(f.read(min(MIB, count)))
                if count >= 512:
                    f.seek(-512, os.SEEK_END)
                    tail = f.read(512)
        else:
            with opener(path, 'rb') as f:
                while True:
                    b = f.read(CHUNK)
                    if not b:
                        break
                    if len(header) < MIB:
                        header.extend(b[:MIB-len(header)])
                    digest.update(b)
                    tail = (tail + b)[-512:]
                    count += len(b)
                    if max_bytes is not None and count > max_bytes:
                        raise SddError('Decompressed test source exceeds 2 GiB limit')
                    if count % (256*MIB) < CHUNK:
                        progress('Validate gzip', count, None, t0)
    except (gzip.BadGzipFile, EOFError, OSError, zlib_error()) as e:
        raise SddError(f'Image read or gzip integrity failure: {e}') from e
    if path.suffix == '.gz':
        print(file=sys.stderr)
    if header[512:520] == b'EFI PART':
        raise SddError('GPT header detected at LBA 1; ambiguous MBR/GPT layout is unsupported')
    if tail[:8] == b'EFI PART':
        raise SddError('GPT backup header detected at image end; ambiguous MBR/GPT layout is unsupported')
    layout = parse_mbr(header[:512], count)
    offset = layout['start'] * 512
    if offset + 512 > len(header):
        if path.suffix == '.gz':
            # A gzip stream is sequential; retain only a bounded header and stream again.
            with gzip.open(path, 'rb') as f:
                remaining = offset
                while remaining:
                    b = f.read(min(CHUNK, remaining))
                    if not b:
                        raise SddError('Truncated partition start')
                    remaining -= len(b)
                boot = f.read(512)
        else:
            with open(path, 'rb') as f:
                f.seek(offset)
                boot = f.read(512)
    else:
        boot = header[offset:offset+512]
    layout.update(parse_ntfs(boot, layout['sectors'], layout['start']))
    layout.update({'path': str(path), 'bytes': count, 'compressed_bytes': path.stat().st_size,
                   'sha256': digest.hexdigest() if digest else None})
    return layout


def zlib_error():
    import zlib
    return zlib.error


_last_status = {}


def clear_activity():
    if sys.stderr.isatty():
        print('\r\033[2K', end='', file=sys.stderr, flush=True)


def activity(label, started, tick, *, touched=False):
    elapsed = time.monotonic() - started
    if sys.stderr.isatty():
        glyph = '|/-\\'[tick % 4]
        print(f'\r\033[2K{glyph} {label}: {elapsed:.0f}s elapsed', end='', file=sys.stderr, flush=True)
    elif tick == 0 or (elapsed - _last_status.get(('activity', label), 0)) >= 10:
        print(f'{label}: {elapsed:.0f}s elapsed (destination {"touched" if touched else "untouched"})', file=sys.stderr, flush=True)
        _last_status[('activity', label)] = elapsed


def progress(label, done, total, t0):
    elapsed = max(time.monotonic()-t0, .001)
    rate = done/elapsed
    percent = f'{done/total*100:5.1f}% ' if total else ''
    amount = f'{done/GIB:.2f}/{total/GIB:.2f} GiB' if total else f'{done/GIB:.2f} GiB'
    eta = f' ETA {(total-done)/rate:.0f}s' if total and rate and done < total else ''
    message = f'{label}: {percent}{amount} ({done:,} bytes), {rate/MIB:.1f} MiB/s, {elapsed:.0f}s{eta}'
    if sys.stderr.isatty():
        print('\r\033[2K' + message, end='' if not total or done < total else '\n', file=sys.stderr, flush=True)
    elif done >= (total or float('inf')) or elapsed - _last_status.get(('progress', label), -10) >= 10:
        print(message, file=sys.stderr, flush=True)
        _last_status[('progress', label)] = elapsed


def stage_notice(number, label, *, touched=False):
    print(f'\n[{number}/5] {label} | destination touched: {"YES" if touched else "no"}', flush=True)


def disk_info(path):
    require('lsblk', 'blockdev')
    p = Path(path).resolve(strict=True)
    if not stat.S_ISBLK(p.stat().st_mode):
        raise SddError('Destination must be a whole physical block disk')
    try:
        data = json.loads(run(['lsblk', '-J', '-b', '-T', '-o',
                               'PATH,TYPE,SIZE,MODEL,SERIAL,MOUNTPOINTS,PKNAME', str(p)]))['blockdevices']
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        raise SddError('Could not parse lsblk disk topology') from e
    # lsblk -T may return the selected disk and descendants as top-level rows.
    # Identify the exact physical disk by its resolved path, never by row order.
    def flatten(rows):
        for node in rows:
            yield node
            yield from flatten(node.get('children') or [])
    candidates = [n for n in flatten(data) if n.get('type') == 'disk' and n.get('path')
                  and Path(n['path']).resolve() == p]
    if len(candidates) != 1:
        raise SddError('Destination must be a whole physical disk, not a partition or virtual device')
    d = candidates[0]
    # Some lsblk versions emit children as siblings even with --tree. PKNAME
    # makes mount/swap/holder checks independent of JSON row ordering.
    d = dict(d)
    all_nodes = list(flatten(data))
    disk_name = Path(d['path']).name
    by_name = {Path(n['path']).name: n for n in all_nodes if n.get('path')}
    descendants = []
    descendant_ids = set()
    def collect_nested(node):
        for child in node.get('children') or []:
            if id(child) not in descendant_ids:
                descendants.append(child)
                descendant_ids.add(id(child))
                collect_nested(child)
    collect_nested(d)
    for node in all_nodes:
        if node is candidates[0] or id(node) in descendant_ids or not node.get('path'):
            continue
        parent = node.get('pkname')
        seen = set()
        while parent and parent not in seen:
            if parent == disk_name:
                descendants.append(node)
                descendant_ids.add(id(node))
                break
            seen.add(parent)
            parent_node = by_name.get(parent)
            parent = parent_node.get('pkname') if parent_node else None
    d['children'] = descendants
    if not d.get('serial') or not d.get('model'):
        raise SddError('Destination model and serial must be available for confirmation')
    if run(['blockdev', '--getro', str(p)]).strip() != '0':
        raise SddError('Destination is read-only')
    if run(['blockdev', '--getss', str(p)]).strip() != '512':
        raise SddError('Only destinations with 512-byte logical sectors are supported')
    def tree_nodes(node):
        yield node
        for child in node.get('children') or []:
            yield from tree_nodes(child)
    nodes = list(tree_nodes(d))
    if any(any(x for x in (n.get('mountpoints') or [])) for n in nodes):
        raise SddError('Destination or a descendant is mounted; this includes system disks')
    for n in nodes:
        name = Path(n['path']).name
        holders = Path('/sys/class/block') / name / 'holders'
        if holders.exists() and any(holders.iterdir()):
            raise SddError('Destination has active device-mapper, RAID, or other holders')
    swaps = Path('/proc/swaps').read_text().splitlines()[1:]
    paths = {str(Path(n['path']).resolve()) for n in nodes if n.get('path')}
    if any(str(Path(line.split()[0]).resolve()) in paths for line in swaps if line.split()):
        raise SddError('Destination or a descendant is active swap')
    d['path'] = str(p)
    d['size'] = physical_size(p)
    d['devnum'] = p.stat().st_rdev
    return d


def test_case_root(case_dir):
    if not case_dir:
        raise SddError('--test-case-dir is required with --test-file-target')
    project = Path(__file__).resolve().parent
    root = project / '.sdd-test-sandbox'
    if root.is_symlink() or not root.is_dir() or root.resolve(strict=True) != root:
        raise SddError('Test sandbox must be a real project-local directory')
    case = Path(case_dir)
    if case.is_symlink() or not case.is_dir() or case.parent.resolve(strict=True) != root:
        raise SddError('Test case must be a real direct child of the test sandbox')
    st = case.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SddError('Test case must be owned by this user and private (mode 0700)')
    return case.resolve(strict=True)


def test_path(path, case, kind):
    p = Path(path)
    try:
        rel = p.relative_to(case)
    except ValueError as e:
        raise SddError('Test path is outside dedicated case directory') from e
    if not rel.parts:
        raise SddError('Test resource cannot be the case directory')
    current = case
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise SddError('Test path contains a symlink')
    resolved = p.resolve(strict=True)
    if not resolved.is_relative_to(case):
        raise SddError('Test path escapes dedicated case directory')
    st = resolved.stat()
    if st.st_dev != case.stat().st_dev:
        raise SddError('Test resource is on a different filesystem from its case')
    if st.st_uid != os.getuid() or (kind == 'file' and (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1)) or (kind == 'dir' and not stat.S_ISDIR(st.st_mode)):
        raise SddError('Test resource must be owned, single-link regular file or directory as required')
    if kind == 'dir' and st.st_mode & 0o077:
        raise SddError('Test scratch directory must be private (mode 0700)')
    return resolved


def test_file_info(path, case_dir=None):
    """Identity for an explicitly disposable project-local regular file."""
    p = test_path(path, test_case_root(case_dir), 'file')
    st = p.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise SddError('Test target must be a single-link regular file')
    if not 64*MIB <= st.st_size <= 2*GIB:
        raise SddError('Test target must be 64 MiB to 2 GiB')
    return {'path': str(p), 'model': 'SDD disposable test file',
            'serial': f'TESTFILE-{st.st_ino}', 'size': st.st_size,
            'devnum': 0, 'file_id': (st.st_dev, st.st_ino)}


def target_info(args):
    return test_file_info(args.disk, args.test_case_dir) if args.test_file_target else disk_info(args.disk)


def check_test_file_paths(source, scratch, target, case_dir):
    case = test_case_root(case_dir)
    source = test_path(source, case, 'file')
    scratch = test_path(scratch, case, 'dir')
    target = test_path(target, case, 'file')
    if source.is_relative_to(scratch) or target.is_relative_to(scratch):
        raise SddError('Test source and target must be outside scratch')
    if os.path.samefile(source, target):
        raise SddError('Test source and target refer to the same file')
    if source.stat().st_size > TEST_MAX_SOURCE:
        raise SddError('Test source exceeds 2 GiB limit')


def refuse_overlap(source, scratch, disk):
    # A file on a destination partition normally gives a matching device number.
    disk_dev = os.stat(disk).st_rdev
    for label, p in [('source', source), ('scratch', scratch)]:
        st = os.stat(p)
        if stat.S_ISBLK(st.st_mode) and st.st_rdev == disk_dev:
            raise SddError(f'{label} aliases the destination')
        mount = run(['findmnt', '-n', '-o', 'SOURCE', '--target', str(p)]).strip().split('[')[0]
        if mount.startswith('/dev/'):
            try:
                node = json.loads(run(['lsblk', '-J', '-o', 'PATH,TYPE,PKNAME', mount]))['blockdevices'][0]
                names = {str(Path(mount).resolve())}
                while node.get('pkname'):
                    parent = '/dev/' + node['pkname']
                    names.add(str(Path(parent).resolve()))
                    node = json.loads(run(['lsblk', '-J', '-o', 'PATH,TYPE,PKNAME', parent]))['blockdevices'][0]
                if str(Path(disk).resolve()) in names:
                    raise SddError(f'{label} is stored on the destination disk')
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                raise SddError(f'Cannot establish {label} storage ancestry') from e
        elif mount.startswith('/dev'):
            raise SddError(f'Cannot establish {label} storage ancestry')


def scratch_required(original, fitted):
    # Both full raw images coexist; allowance covers metadata and filesystem overhead.
    return original + fitted + max(2*GIB, (original+fitted)//20)


def target_size(capacity, layout, test_file=False):
    # Keep the production disk's 1 GiB clearance; use a small file-only scale.
    clearance = 8*MIB if test_file else GIB
    minimum = 64*MIB if test_file else 2*GIB
    target = ((capacity-clearance)//MIB)*MIB
    if target < minimum or target <= layout['start']*512 + clearance:
        raise SddError('Destination is too small for a conservative NTFS target')
    return target


def resize_plan(layout, target, minimum, test_file=False):
    old_end = (layout['start'] + layout['sectors']) * 512
    old_trailing = layout['bytes'] - old_end
    allowance = target - layout['start']*512 - max(MIB, old_trailing) - MIB
    margin = max(32*MIB if test_file else 2*GIB, minimum//5)
    ntfs_target = allowance - 16*MIB
    if minimum <= 0 or minimum + margin > allowance or ntfs_target <= minimum:
        raise SddError('NTFS minimum plus free-space margin does not fit destination')
    return allowance, margin, ntfs_target


def ntfs_minimum(raw, layout, log_path=None):
    require('losetup', 'ntfsresize')
    loop = run(['losetup', '--find', '--show', '--read-only', '--partscan', raw]).strip()
    try:
        partition = wait_for_loop_partition(loop)
        check_cmd = ['ntfsresize', '--check', partition]
        info_cmd = ['ntfsresize', '--info', '--no-action', partition]
        if log_path:
            run_with_output_status(check_cmd, label='Checking NTFS', log_path=log_path)
            report = run_with_output_status(info_cmd, label='Measuring NTFS minimum', log_path=log_path)
        else:
            run(check_cmd)
            report = run(info_cmd)
        match = re.search(r'You might resize at\s+(\d+)\s+bytes', report, re.I)
        if not match:
            raise SddError('Could not parse ntfsresize minimum; refusing to estimate: ' + report[-1200:])
        return int(match.group(1)), report
    finally:
        run(['losetup', '--detach', loop])


def shrink_staged_ntfs(raw, ntfs_bytes, log_path=None):
    loop = run(['losetup', '--find', '--show', '--partscan', raw]).strip()
    try:
        partition = wait_for_loop_partition(loop)
        option = f'--size={ntfs_bytes}'
        run(['ntfsresize', '--no-action', option, partition])
        # The read-only test above rejects dirty/hibernated source filesystems.
        # --force here suppresses the interactive prompt; it acts only on the scratch copy.
        run_live(['ntfsresize', '--force', option, partition], label='Shrinking NTFS', log_path=log_path)
    finally:
        run(['losetup', '--detach', loop])


def wait_for_loop_partition(loop):
    partition = loop + 'p1'
    deadline = time.monotonic() + 5
    while not Path(partition).exists():
        if time.monotonic() >= deadline:
            raise SddError('Loop partition node did not appear')
        time.sleep(.1)
    return partition


def verify_ntfs_metadata(path, layout, log_path=None, touched=False):
    offset = layout['start'] * 512
    length = layout['sectors'] * 512
    loop = run(['losetup', '--find', '--show', '--read-only', '--offset', str(offset),
                '--sizelimit', str(length), str(path)]).strip()
    try:
        # A resized NTFS volume is intentionally marked for Windows chkdsk.
        cmd = ['ntfsresize', '--info', '--force', loop]
        report = (run_with_output_status(cmd, label='Inspecting NTFS metadata', log_path=log_path,
                                         touched=touched) if log_path else run(cmd))
        if not re.search(r'You might resize at\s+\d+\s+bytes', report, re.I):
            raise SddError('NTFS metadata inspection gave no recognizable result')
    finally:
        run(['losetup', '--detach', loop])


def guestfish_base(path, read_only):
    return ['guestfish', '--ro' if read_only else '--rw', '--format=raw', '-a', str(path), 'run', ':']


def guestfish_minimum(path, log_path=None):
    cmd = guestfish_base(path, True) + ['vfs-minimum-size', '/dev/sda1']
    report = (run_with_output_status(cmd, label='Measuring NTFS minimum', log_path=log_path)
              if log_path else run(cmd)).strip()
    if not re.fullmatch(r'\d+', report):
        raise SddError('Could not parse libguestfs NTFS minimum: ' + report[-1200:])
    minimum = int(report)
    if minimum <= 0:
        raise SddError('Libguestfs reported a nonpositive NTFS minimum')
    return minimum, report


def guestfish_shrink(path, ntfs_bytes, log_path=None):
    run_live(guestfish_base(path, False) + ['ntfsresize', '/dev/sda1', f'size:{ntfs_bytes}'],
             label='Shrinking NTFS', log_path=log_path)


def guestfish_verify(path, log_path=None, touched=False):
    cmd = guestfish_base(path, True) + ['vfs-type', '/dev/sda1']
    kind = (run_with_output_status(cmd, label='Checking NTFS type', log_path=log_path, touched=touched)
            if log_path else run(cmd)).strip()
    if kind != 'ntfs':
        raise SddError(f'Libguestfs did not recognize destination NTFS: {kind!r}')


def fit_staged_image(raw, fitted, log_path=None):
    # ntfsresize marks the staged volume for a Windows consistency check.
    # virt-resize must copy that already-shrunk filesystem without issuing a
    # second NTFS resize, which would reject the check-required volume.
    run_live(['virt-resize', '--format', 'raw', '--output-format', 'raw',
              '--align-first', 'never', '--no-extra-partition',
              '--no-expand-content', '--shrink', '/dev/sda1',
              str(raw), str(fitted)], label='Fitting disk image', log_path=log_path)


def preserve_start_chs(staged, fitted):
    """Keep the source BIOS start address when virt-resize rewrites the MBR.

    The first partition's LBA is unchanged, so its original start CHS remains
    the safest available legacy BIOS address. The end CHS follows the new end.
    """
    staged_path = Path(staged)
    if staged_path.is_symlink() or not stat.S_ISREG(staged_path.stat().st_mode):
        raise SddError('Staged image is not a regular file')
    with open(staged_path, 'rb') as source:
        original = source.read(512)
    anchor = os.open(fitted, os.O_PATH | os.O_NOFOLLOW)
    try:
        identity = os.fstat(anchor)
        if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
            raise SddError('Fitted image is not a single-link regular file')
        fd = os.open(f'/proc/self/fd/{anchor}', os.O_RDWR)
        try:
            output = os.pread(fd, 512, 0)
            if (len(original) != 512 or len(output) != 512
                    or original[510:512] != b'\x55\xaa' or output[510:512] != b'\x55\xaa'
                    or original[:447] != output[:447]
                    or original[450] != output[450]
                    or original[454:458] != output[454:458]):
                raise SddError('Fitted MBR changed fixed boot fields or partition start LBA')
            if output[447:450] != original[447:450]:
                if os.pwrite(fd, original[447:450], 447) != 3:
                    raise SddError('Could not preserve starting CHS in fitted MBR')
                os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.close(anchor)


def assert_shrunk_copy(shrunk, fitted, allowance):
    if shrunk['ntfs_sectors'] * 512 > allowance:
        raise SddError('Staged NTFS filesystem exceeds target partition allowance')
    if fitted['ntfs_sectors'] != shrunk['ntfs_sectors']:
        raise SddError('Fitted NTFS length changed during partition copy')
    if fitted['ntfs_sectors'] > fitted['sectors']:
        raise SddError('Fitted NTFS filesystem exceeds partition boundary')


def copy_image(src, dst, compressed, expected=None):
    opener = gzip.open if compressed else open
    copied = 0
    digest = hashlib.sha256()
    t0 = time.monotonic()
    with opener(src, 'rb') as inp, open(dst, 'xb') as out:
        while True:
            b = inp.read(CHUNK)
            if not b:
                break
            out.write(b)
            digest.update(b)
            copied += len(b)
            if expected is not None and copied > expected:
                raise SddError('Source image grew during staging')
            progress('Stage image', copied, expected, t0)
        out.flush(); os.fsync(out.fileno())
    print(file=sys.stderr)
    if expected is not None and copied != expected:
        raise SddError('Source image changed during staging')
    return digest.hexdigest()


def compare_files(image, disk, length):
    done = 0
    t0 = time.monotonic()
    with open(image, 'rb', buffering=0) as a, open(disk, 'rb', buffering=0) as b:
        while done < length:
            n = min(CHUNK, length-done)
            x, y = a.read(n), b.read(n)
            if len(x) != n or x != y:
                raise SddError(f'Destination byte verification failed near offset {done:,}')
            done += n
            progress('Verify destination', done, length, t0)
    print(file=sys.stderr)


def compare_boot_region(source, fitted, start_lba):
    # virt-resize must edit the partition entry, so compare the remainder of
    # the MBR and every pre-partition sector where BIOS loader stages may live.
    with open(source, 'rb') as a, open(fitted, 'rb') as b:
        if a.read(446) != b.read(446):
            raise SddError('MBR boot code or disk identity changed in fitted image')
        a.seek(512); b.seek(512)
        remaining = start_lba * 512 - 512
        while remaining:
            n = min(CHUNK, remaining)
            if a.read(n) != b.read(n):
                raise SddError('Pre-partition bootloader sectors changed in fitted image')
            remaining -= n


def write_destination(image, disk, fitted_bytes, disk_bytes, expected_dev=None, expected_file=None):
    # O_PATH pins the already validated test inode without opening a substituted
    # block-device alias for writing. /proc/self/fd then opens that pinned inode.
    pinned = None
    if expected_file is not None:
        pinned = os.open(disk, os.O_PATH | os.O_NOFOLLOW)
        st = os.fstat(pinned)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != expected_file:
            os.close(pinned)
            raise SddError('Test target changed before destination write')
        disk = f'/proc/self/fd/{pinned}'
    try:
        _write_destination_open(image, disk, fitted_bytes, disk_bytes, expected_dev, expected_file)
    finally:
        if pinned is not None:
            os.close(pinned)


def _write_destination_open(image, disk, fitted_bytes, disk_bytes, expected_dev, expected_file):
    with open(image, 'rb', buffering=0) as inp, open(disk, 'r+b', buffering=0) as out:
        if expected_dev is not None and os.fstat(out.fileno()).st_rdev != expected_dev:
            raise SddError('Opened destination device does not match preflight')
        if expected_file is not None:
            st = os.fstat(out.fileno())
            if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != expected_file:
                raise SddError('Opened test target does not match preflight')
        done = 0; t0 = time.monotonic()
        while done < fitted_bytes:
            b = inp.read(min(CHUNK, fitted_bytes-done))
            if not b:
                raise SddError('Fitted image ended early')
            if out.write(b) != len(b):
                raise SddError('Short destination write')
            done += len(b)
            progress('Write destination', done, fitted_bytes, t0)
        # Clear stale backup GPT or other signatures beyond the new raw image.
        zero = bytes(CHUNK)
        tail = fitted_bytes
        t0 = time.monotonic()
        while tail < disk_bytes:
            amount = min(CHUNK, disk_bytes-tail)
            if out.write(zero[:amount]) != amount:
                raise SddError('Short write while clearing destination tail')
            tail += amount
            progress('Clear destination tail', tail-fitted_bytes, disk_bytes-fitted_bytes, t0)
        os.fsync(out.fileno())
    print(file=sys.stderr)


def verify_tail_zero(disk, start, end):
    with open(disk, 'rb', buffering=0) as f:
        f.seek(start)
        position = start
        while position < end:
            n = min(CHUNK, end-position)
            b = f.read(n)
            if len(b) != n or any(b):
                raise SddError(f'Destination tail was not cleared near byte {position:,}')
            position += n


def confirm(disk):
    print(f"\nWARNING: ALL DATA ON THE DESTINATION WILL BE DESTROYED.\n\n"
          f"Device: {disk['path']}\nModel: {disk['model']}\nSerial: {disk['serial']}\n"
          f"Capacity: {disk['size']:,} bytes")
    while True:
        try:
            answer = input('Erase this destination drive? [y/N]: ').strip().lower()
        except (EOFError, KeyboardInterrupt) as e:
            raise Cancelled('Confirmation cancelled.') from e
        if answer == 'y':
            return
        if answer in ('', 'n'):
            raise Cancelled('Confirmation declined.')
        print('Please enter y or n.')


CHECKPOINT_VERSION = 1
LAYOUT_FIELDS = ('boot', 'type', 'start', 'sectors', 'start_chs', 'end_chs',
                 'signature', 'boot_code', 'ntfs_sectors', 'ntfs_serial', 'ntfs_hidden')


def hash_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_checkpoint(work, *, source, scratch, target, disk, destination_requested, layout, test_mode, test_case_dir):
    """Atomically record a validated fitted image for a later explicit resume."""
    fitted = work / 'fitted.img'
    if fitted.is_symlink() or not fitted.is_file() or fitted.stat().st_nlink != 1:
        raise SddError('Cannot checkpoint an unsafe fitted image')
    record = {
        'version': CHECKPOINT_VERSION, 'fitted': str(fitted.resolve(strict=True)),
        'fitted_bytes': fitted.stat().st_size, 'fitted_sha256': hash_file(fitted),
        'source': str(Path(source).resolve(strict=True)), 'scratch': str(Path(scratch).resolve(strict=True)),
        'target_bytes': target,
        'destination': {**{k: disk[k] for k in ('path', 'model', 'serial', 'size')},
                        'requested_path': os.path.abspath(destination_requested)},
        'test_file_target': bool(test_mode), 'test_case_dir': str(Path(test_case_dir).resolve(strict=True)) if test_case_dir else None,
        'layout': {k: (layout[k].hex() if k == 'boot_code' else layout[k]) for k in LAYOUT_FIELDS},
    }
    temp = work / '.job.json.tmp'
    final = work / 'job.json'
    if temp.exists() or temp.is_symlink() or final.exists() or final.is_symlink():
        raise SddError('Refusing to overwrite an existing restore checkpoint')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, final)
        dirfd = os.open(work, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def read_checkpoint(job):
    path = Path(job)
    if path.is_symlink():
        raise SddError('Resume job directory cannot be a symlink')
    root = path.resolve(strict=True)
    st = root.stat()
    if not root.is_dir() or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SddError('Resume job must be an owned private directory (mode 0700)')
    checkpoint = root / 'job.json'
    if checkpoint.is_symlink() or not checkpoint.is_file() or checkpoint.stat().st_nlink != 1:
        raise SddError('Resume job has no safe checkpoint')
    try:
        data = json.loads(checkpoint.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SddError(f'Cannot read resume checkpoint: {e}') from e
    if data.get('version') != CHECKPOINT_VERSION:
        raise SddError('Unsupported or incomplete resume checkpoint')
    fitted = Path(data.get('fitted', ''))
    if fitted.parent != root or fitted.name != 'fitted.img' or fitted.is_symlink() or not fitted.is_file():
        raise SddError('Checkpoint fitted image is missing or escapes its job directory')
    st = fitted.stat()
    if st.st_nlink != 1 or st.st_size != data.get('fitted_bytes'):
        raise SddError('Checkpoint fitted image size or file identity is invalid')
    if hash_file(fitted) != data.get('fitted_sha256'):
        raise SddError('Checkpoint fitted image checksum mismatch')
    try:
        layout = dict(data['layout'])
        layout['boot_code'] = bytes.fromhex(layout['boot_code'])
        fitted_info = image_info(fitted)
    except (KeyError, ValueError, TypeError) as e:
        raise SddError(f'Invalid resume checkpoint layout: {e}') from e
    for key in LAYOUT_FIELDS:
        if fitted_info.get(key) != layout.get(key):
            raise SddError(f'Resumed fitted image differs from checkpoint: {key}')
    if fitted.stat().st_size != data.get('target_bytes'):
        raise SddError('Fitted image length does not match checkpoint target')
    return data, fitted, fitted_info


def safe_operation_log(job):
    log = Path(job) / 'operations.log'
    if log.is_symlink():
        raise SddError('Resume operation log cannot be a symlink')
    try:
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise SddError(f'Cannot safely open resume operation log: {e}') from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
            raise SddError('Resume operation log must be an owned single-link regular file')
    finally:
        os.close(fd)
    return log


def write_and_verify(args, disk, identity, fitted, fitted_info, target, source, scratch, operation_log, write_state=None):
    if args.test_file_target:
        check_test_file_paths(source, scratch, disk['path'], args.test_case_dir)
    else:
        refuse_overlap(source, scratch, disk['path'])
    stage_notice(5, 'Confirming, writing, and verifying destination')
    confirm(disk)
    disk = target_info(args)
    current = (disk['path'], disk['model'], disk['serial'], disk['size'], disk['devnum'], disk.get('file_id'))
    if current != identity:
        raise SddError('Destination identity changed after confirmation')
    if args.test_file_target:
        check_test_file_paths(source, scratch, disk['path'], args.test_case_dir)
    else:
        refuse_overlap(source, scratch, disk['path'])
    if disk['size'] < target:
        raise SddError('Destination capacity changed')
    if write_state is not None:
        write_state['started'] = True
    print('DESTINATION WRITE BEGINS NOW. Interruption may leave the target unusable.', flush=True)
    write_destination(fitted, disk['path'], target, disk['size'], disk['devnum'], disk.get('file_id'))
    print('Destination touched: YES. Reading back written bytes and NTFS metadata.', flush=True)
    compare_files(fitted, disk['path'], target)
    verify_tail_zero(disk['path'], target, disk['size'])
    with open(disk['path'], 'rb', buffering=0) as f:
        sector = f.read(512)
        f.seek(fitted_info['start']*512)
        boot = f.read(512)
    verified = parse_mbr(sector, disk['size'])
    verified.update(parse_ntfs(boot, verified['sectors'], verified['start']))
    for key in ('boot', 'type', 'start', 'start_chs', 'sectors', 'signature', 'boot_code', 'ntfs_serial', 'ntfs_hidden'):
        if verified[key] != fitted_info[key]:
            raise SddError(f'Destination structure verification failed: {key}')
    if args.test_file_target:
        guestfish_verify(disk['path'], operation_log, touched=True)
    else:
        verify_ntfs_metadata(disk['path'], verified, operation_log, touched=True)
    print('PASS: destination bytes and MBR/NTFS boot metadata verified. Windows boot and full filesystem consistency remain unverified.')


def restore(args):
    stage_notice(1, 'Inspecting source and destination')
    if not args.scratch:
        raise SddError('Scratch storage is required: supply --scratch with a mounted directory on a large drive')
    if args.test_file_target:
        check_test_file_paths(args.image, args.scratch, args.disk, args.test_case_dir)
    elif args.test_case_dir:
        raise SddError('--test-case-dir requires --test-file-target')
    scratch = Path(args.scratch).resolve(strict=True)
    if not scratch.is_dir() or scratch == Path('/'):
        raise SddError('Scratch must be an existing explicit directory')
    src = Path(args.image).resolve(strict=True)
    if args.test_file_target:
        disk = target_info(args)
        check_test_file_paths(src, scratch, disk['path'], args.test_case_dir)
    info = image_info(src, TEST_MAX_SOURCE if args.test_file_target else None)
    if not args.test_file_target:
        disk = target_info(args)
    require('findmnt')
    if not args.test_file_target:
        refuse_overlap(src, scratch, disk['path'])
        mount_target = run(['findmnt', '-n', '-o', 'TARGET', '--target', str(scratch)]).strip()
        if mount_target == '/':
            raise SddError('Scratch must be on a separately mounted filesystem, not the system root')
        scratch_fs = run(['findmnt', '-n', '-o', 'FSTYPE', '--target', str(scratch)]).strip()
        if scratch_fs not in ('ext4', 'xfs', 'btrfs'):
            raise SddError('Scratch must be on a local ext4, xfs, or btrfs filesystem')
    initial_identity = (disk['path'], disk['model'], disk['serial'], disk['size'], disk['devnum'], disk.get('file_id'))
    target = target_size(disk['size'], info, args.test_file_target)
    if target >= info['bytes']:
        raise SddError('This V1 restore path is for images larger than the destination; image already fits')
    need = scratch_required(info['bytes'], target)
    free = shutil.disk_usage(scratch).free
    if free < need:
        raise SddError(f'Insufficient scratch: need {need:,} bytes, have {free:,} bytes')
    print(f"Source: {src}\nOriginal: {info['bytes']:,} bytes; MBR NTFS partition at LBA {info['start']}\n"
          f"Destination: {disk['path']} ({disk['model']}, serial {disk['serial']}, {disk['size']:,} bytes)\n"
          f"Scratch: {scratch}, required {need:,} bytes, available {free:,} bytes\n"
          f"Fitted image target: {target:,} bytes")
    if args.dry_run:
        backend = 'libguestfs image access' if args.test_file_target else 'root loop access'
        print(f'Dry run: static layout and capacity checked. NTFS minimum requires scratch staging and {backend}; no write performed.')
        return
    if args.test_file_target and os.geteuid() == 0:
        raise SddError('Test-file mode must run unprivileged')
    if not args.test_file_target and os.geteuid() != 0:
        raise SddError('Physical restore requires root for loop inspection and destination access')
    if args.test_file_target:
        require('guestfish', 'virt-resize', 'qemu-img')
    else:
        require('losetup', 'ntfsresize', 'virt-resize', 'qemu-img')
    if 'ntfs' not in run(['virt-resize', '--machine-readable']).splitlines():
        raise SddError('virt-resize lacks NTFS support')
    work = Path(tempfile.mkdtemp(prefix='sdd-', dir=scratch))
    raw = work/'source.img'; fitted = work/'fitted.img'
    operation_log = work/'operations.log'
    stage = 'staging'
    write_state = {'started': False}
    try:
        stage_notice(2, 'Staging/decompressing original image')
        source_before = src.stat()
        staged_hash = copy_image(src, raw, src.suffix == '.gz', info['bytes'])
        source_after = src.stat()
        if (source_before.st_dev, source_before.st_ino, source_before.st_size,
            source_before.st_mtime_ns, source_before.st_ctime_ns) != (
            source_after.st_dev, source_after.st_ino, source_after.st_size,
            source_after.st_mtime_ns, source_after.st_ctime_ns):
            raise SddError('Source file changed during staging')
        if info['sha256'] and staged_hash != info['sha256']:
            raise SddError('Staged image digest differs from validated gzip')
        staged = image_info(raw)
        if staged['boot_code'] != info['boot_code'] or staged['signature'] != info['signature']:
            raise SddError('Staged MBR differs from source')
        stage_notice(3, 'Measuring and shrinking NTFS filesystem')
        minimum, report = (guestfish_minimum(raw, operation_log) if args.test_file_target
                           else ntfs_minimum(str(raw), staged, operation_log))
        available_partition, margin, ntfs_target = resize_plan(staged, target, minimum, args.test_file_target)
        print(f'NTFS minimum reported by filesystem utility: {minimum:,} bytes\n'
              f'Target partition allowance: {available_partition:,} bytes; safety margin: {margin:,} bytes')
        stage = 'NTFS resize in scratch'
        if args.test_file_target:
            guestfish_shrink(raw, ntfs_target, operation_log)
        else:
            shrink_staged_ntfs(str(raw), ntfs_target, operation_log)
        shrunk = image_info(raw)
        if shrunk['ntfs_sectors'] * 512 > available_partition:
            raise SddError('Staged NTFS filesystem exceeds target partition allowance')
        run(['qemu-img', 'create', '-f', 'raw', str(fitted), str(target)])
        stage = 'fitting'
        stage_notice(4, 'Building and validating smaller disk image')
        print('Resizing with virt-resize; output is in scratch.', flush=True)
        # Copy the pre-partition sectors and edit the MBR partition entry.
        # The NTFS filesystem has already been shrunk; do not resize it again.
        fit_staged_image(raw, fitted, operation_log)
        preserve_start_chs(raw, fitted)
        result = image_info(fitted)
        assert_shrunk_copy(shrunk, result, available_partition)
        print(f"Fitted NTFS partition: {result['sectors']*512:,} bytes at LBA {result['start']}")
        assert_preserved(info, result)
        compare_boot_region(raw, fitted, info['start'])
        if result['sectors']*512 < minimum or (result['start']+result['sectors'])*512 > target:
            raise SddError('Fitted partition boundary is invalid')
        if args.test_file_target:
            guestfish_verify(fitted, operation_log)
        else:
            verify_ntfs_metadata(fitted, result, operation_log)
        disk = target_info(args)
        if (disk['path'], disk['model'], disk['serial'], disk['size'], disk['devnum'], disk.get('file_id')) != initial_identity:
            raise SddError('Destination identity changed during preparation')
        if args.test_file_target:
            check_test_file_paths(src, scratch, disk['path'], args.test_case_dir)
        else:
            refuse_overlap(src, scratch, disk['path'])
        if disk['size'] < target:
            raise SddError('Destination capacity changed')
        write_checkpoint(work, source=src, scratch=scratch, target=target, disk=disk,
                         destination_requested=args.disk,
                         layout=result, test_mode=args.test_file_target,
                         test_case_dir=args.test_case_dir)
        resume_prefix = 's_dd resume' if args.test_file_target else 'sudo s_dd resume'
        print(f'Validated restore job saved at: {work}\nResume later with: {resume_prefix} {work}', flush=True)
        disk = target_info(args)
        if (disk['path'], disk['model'], disk['serial'], disk['size'], disk['devnum'], disk.get('file_id')) != initial_identity:
            raise SddError('Destination identity changed during preparation')
        stage = 'destination write'
        write_and_verify(args, disk, initial_identity, fitted, result, target, src, scratch, operation_log, write_state)
    except Cancelled as e:
        prefix = 's_dd resume' if args.test_file_target else 'sudo s_dd resume'
        raise Cancelled(f'{e} Destination not modified. Resume available: {prefix} {work}') from e
    except BaseException:
        print(f'FAIL during {stage}. Destination write started: {write_state["started"]}. Scratch and logs retained at {work}.', file=sys.stderr)
        raise
    else:
        print(f'Scratch retained at {work} for review. Remove it manually after hardware validation.')


def resume(args):
    data, fitted, fitted_info = read_checkpoint(args.job)
    test_mode = data.get('test_file_target') is True
    if test_mode and os.geteuid() == 0:
        raise SddError('Test-file mode must run unprivileged')
    if not test_mode and os.geteuid() != 0:
        raise SddError('Physical restore requires root for destination access')
    args.test_file_target = test_mode
    args.test_case_dir = data.get('test_case_dir')
    args.disk = data['destination'].get('requested_path', data['destination']['path'])
    args.image = data['source']
    args.scratch = data['scratch']
    source = Path(args.image).resolve(strict=True)
    scratch = Path(args.scratch).resolve(strict=True)
    job = Path(args.job).resolve(strict=True)
    if not scratch.is_dir() or not job.is_relative_to(scratch):
        raise SddError('Resume job is not inside its recorded scratch directory')
    if test_mode:
        check_test_file_paths(source, scratch, args.disk, args.test_case_dir)
    require('findmnt')
    disk = target_info(args)
    expected = data['destination']
    if any(disk.get(k) != expected.get(k) for k in ('model', 'serial', 'size')):
        raise SddError('Resume destination does not match the original job identity; refusing to redirect the restore')
    if test_mode:
        check_test_file_paths(source, scratch, disk['path'], args.test_case_dir)
    else:
        refuse_overlap(source, scratch, disk['path'])
        if run(['findmnt', '-n', '-o', 'TARGET', '--target', str(scratch)]).strip() == '/':
            raise SddError('Scratch must be on a separately mounted filesystem')
    identity = (disk['path'], disk['model'], disk['serial'], disk['size'], disk['devnum'], disk.get('file_id'))
    operation_log = safe_operation_log(job)
    write_state = {'started': False}
    print(f'Resuming validated fitted image: {fitted}\nDestination: {disk["path"]} ({disk["model"]}, serial {disk["serial"]}, {disk["size"]:,} bytes)')
    try:
        # Recheck NTFS using the selected backend immediately before the prompt.
        if test_mode:
            guestfish_verify(fitted, operation_log)
        else:
            verify_ntfs_metadata(fitted, fitted_info, operation_log)
        write_and_verify(args, disk, identity, fitted, fitted_info,
                         data['target_bytes'], source, scratch, operation_log, write_state)
    except Cancelled as e:
        prefix = 's_dd resume' if test_mode else 'sudo s_dd resume'
        raise Cancelled(f'{e} Destination write not started. Job retained; resume with: {prefix} {job}') from e
    except BaseException:
        print(f'FAIL. Destination write started: {write_state["started"]}; job retained at {job}.', file=sys.stderr)
        raise


def inspect(args):
    x = image_info(args.image)
    print(f"Source: {x['path']}\nImage bytes: {x['bytes']:,}\nPartition table: MBR\n"
          f"Partition: type 0x{x['type']:02x}, active {bool(x['boot'])}, start LBA {x['start']}, sectors {x['sectors']:,}\n"
          f"Filesystem: NTFS, {x['ntfs_sectors']:,} sectors, serial {x['ntfs_serial']}\n"
          'NTFS minimum and allocated data require ntfsresize on staged image; bootability is not established.')


def doctor(args):
    """Read-only executable check; does not launch libguestfs or touch disks."""
    groups = {
        'Restore': ('lsblk', 'blockdev', 'findmnt', 'losetup', 'ntfsresize',
                    'virt-resize', 'qemu-img'),
        'File test and inspection helpers': ('guestfish', 'mkntfs', 'sfdisk'),
    }
    missing = []
    print(f'Python: {sys.version.split()[0]}')
    for group, names in groups.items():
        print(group + ':')
        for name in names:
            location = shutil.which(name)
            print(f'  {name}: {location or "MISSING"}')
            if not location:
                missing.append(name)
    if missing:
        raise SddError('Missing software: ' + ', '.join(missing))
    print('PASS: required executables found. NTFS capability, appliance startup, and hardware remain untested by doctor.')


def interactive_menu():
    print(f'S_DD — Shrink DD\nVersion {VERSION}\n\n[1] Restore disk image\n[2] Exit')
    try:
        choice = input('\nSelect operation: ').strip()
        if choice in ('2', ''):
            print('Exited. No destination was modified.')
            return 0
        if choice != '1':
            print('Please choose 1 or 2.', file=sys.stderr)
            return 2
        def read_path(prompt):
            value = input(prompt).strip()
            return str(Path(value).expanduser()) if value else ''
        source = read_path('Enter source image path:\n> ')
        destination = read_path('Enter destination drive path:\n> ')
        scratch = read_path('Enter scratch directory (existing mounted local filesystem):\n> ')
        if not source or not destination or not scratch:
            raise Cancelled('Cancelled: all three paths are required; destination not modified.')
        restore(argparse.Namespace(image=source, disk=destination, scratch=scratch,
                                   dry_run=False, test_file_target=False, test_case_dir=None))
        return 0
    except Cancelled as e:
        print(str(e), file=sys.stderr)
        return 0
    except (EOFError, KeyboardInterrupt):
        print('\nCancelled: destination not modified.', file=sys.stderr)
        return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='s_dd', description='S_DD — Shrink DD Rev 1.1 experimental restore utility')
    p.add_argument('--version', action='version', version=VERSION)
    sub = p.add_subparsers(dest='command')
    i = sub.add_parser('inspect', help='Validate gzip and inspect MBR/NTFS image')
    i.add_argument('image'); i.set_defaults(func=inspect)
    d = sub.add_parser('doctor', help='Check local executables without opening a disk or appliance')
    d.set_defaults(func=doctor)
    r = sub.add_parser('restore', help='Scratch-assisted restore to a smaller physical disk')
    r.add_argument('image'); r.add_argument('disk'); r.add_argument('--scratch')
    r.add_argument('--dry-run', action='store_true')
    r.add_argument('--test-file-target', action='store_true',
                   help='explicit disposable regular-file target inside project test sandbox')
    r.add_argument('--test-case-dir', help='required private case directory for file-target tests')
    r.set_defaults(func=restore)
    rs = sub.add_parser('resume', help='resume a validated fitted-image job without repeating preparation')
    rs.add_argument('job', help='private sdd-* job directory shown after fitting')
    rs.set_defaults(func=resume)
    args = p.parse_args(argv)
    if args.command is None:
        if not sys.stdin.isatty():
            p.print_help()
            return 2
        return interactive_menu()
    try:
        args.func(args)
        return 0
    except Cancelled as e:
        print(f'\nCANCELLED: {e}', file=sys.stderr)
        return 2
    except (SddError, OSError, subprocess.SubprocessError, EOFError, KeyboardInterrupt) as e:
        print(f'\nS_DD FAIL: {e}', file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
