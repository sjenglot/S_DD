import gzip
import errno
import io
import os
from pathlib import Path
import runpy
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sdd

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / '.sdd-test-sandbox'


def synthetic(path, *, size=32*sdd.MIB, start=2048, sectors=60000, typ=7, active=0x80, extra=False):
    mbr = bytearray(512)
    mbr[:440] = b'B' * 440
    mbr[440:444] = b'ABCD'
    mbr[446] = active
    mbr[450] = typ
    struct.pack_into('<II', mbr, 454, start, sectors)
    if extra:
        mbr[462+4] = 7
        struct.pack_into('<II', mbr, 462+8, 100, 100)
    mbr[510:512] = b'\x55\xaa'
    boot = bytearray(512)
    boot[3:11] = b'NTFS    '
    struct.pack_into('<H', boot, 11, 512)
    struct.pack_into('<I', boot, 28, start)
    struct.pack_into('<Q', boot, 40, sectors)
    boot[72:80] = b'SERIAL12'
    boot[510:512] = b'\x55\xaa'
    with open(path, 'xb') as f:
        f.write(mbr)
        f.seek(start*512)
        f.write(boot)
        f.truncate(size)
    return bytes(mbr), bytes(boot)


class SddTests(unittest.TestCase):
    def setUp(self):
        SANDBOX.mkdir(exist_ok=True)
        if SANDBOX.is_symlink() or not SANDBOX.is_dir():
            raise RuntimeError('Test sandbox must be a real directory')
        self.tmp = tempfile.TemporaryDirectory(dir=SANDBOX)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.scratch = self.dir/'scratch'
        self.scratch.mkdir(mode=0o700)

    def test_raw_and_gzip_inspection(self):
        raw = self.dir/'disk.img'
        synthetic(raw)
        with gzip.open(self.dir/'disk.img.gz', 'wb') as f:
            f.write(raw.read_bytes())
        a = sdd.image_info(raw)
        b = sdd.image_info(self.dir/'disk.img.gz')
        for key in ('bytes', 'start', 'sectors', 'boot', 'type', 'signature', 'boot_code', 'ntfs_serial'):
            self.assertEqual(a[key], b[key])
        self.assertEqual(a['bytes'], 32*sdd.MIB)

    def test_gzip_corruption_and_truncation(self):
        raw = self.dir/'disk.img'
        synthetic(raw)
        gz = self.dir/'disk.img.gz'
        with gzip.open(gz, 'wb') as f:
            f.write(raw.read_bytes())
        data = bytearray(gz.read_bytes())
        data[-8] ^= 1
        gz.write_bytes(data)
        with self.assertRaises(sdd.SddError):
            sdd.image_info(gz)
        gz.write_bytes(data[:-5])
        with self.assertRaises(sdd.SddError):
            sdd.image_info(gz)

    def test_unsupported_layouts(self):
        for kwargs in ({'extra': True}, {'typ': 0xee}, {'active': 0x40}, {'sectors': 1000000}):
            raw = self.dir/('disk'+str(len(list(self.dir.iterdir())))+'.img')
            synthetic(raw, **kwargs)
            with self.assertRaises(sdd.SddError):
                sdd.image_info(raw)
        hybrid = self.dir/'ambiguous.img'
        synthetic(hybrid)
        with open(hybrid, 'r+b') as f:
            f.seek(512)
            f.write(b'EFI PART')
        with self.assertRaisesRegex(sdd.SddError, 'GPT header'):
            sdd.image_info(hybrid)
        backup = self.dir/'backup-gpt.img'
        synthetic(backup)
        with open(backup, 'r+b') as f:
            f.seek(-512, os.SEEK_END)
            f.write(b'EFI PART')
        with self.assertRaisesRegex(sdd.SddError, 'GPT backup'):
            sdd.image_info(backup)

    def test_missing_ntfs(self):
        raw = self.dir/'disk.img'
        synthetic(raw)
        with open(raw, 'r+b') as f:
            f.seek(2048*512+3)
            f.write(b'UNKNOWN!')
        with self.assertRaises(sdd.SddError):
            sdd.image_info(raw)
        raw2 = self.dir/'hidden.img'
        synthetic(raw2)
        with open(raw2, 'r+b') as f:
            f.seek(2048*512+28)
            f.write(b'\x00\x00\x00\x00')
        with self.assertRaisesRegex(sdd.SddError, 'hidden-sector'):
            sdd.image_info(raw2)

    def test_capacity_and_scratch(self):
        layout = {'start': 2048}
        target = sdd.target_size(256*10**9, layout)
        self.assertLess(target, 256*10**9)
        self.assertEqual(target % sdd.MIB, 0)
        self.assertGreater(sdd.scratch_required(500*10**9, target), 500*10**9+target)
        with self.assertRaises(sdd.SddError):
            sdd.target_size(sdd.GIB, layout)
        measured = {'start': 2048, 'sectors': 500000, 'bytes': 256*sdd.MIB}
        allowance, margin, target_bytes = sdd.resize_plan(measured, 152*sdd.MIB, 4*sdd.MIB, True)
        self.assertGreater(target_bytes, 4*sdd.MIB)
        self.assertGreater(allowance, margin)
        with self.assertRaisesRegex(sdd.SddError, 'does not fit'):
            sdd.resize_plan(measured, 152*sdd.MIB, 200*sdd.MIB, True)

    def test_virt_resize_copies_pre_shrunk_ntfs_without_second_resize(self):
        # ntfsresize schedules a check; a second resize in virt-resize failed
        # in the first real NTFS run. The partition may grow around the
        # already-shrunk filesystem, but its NTFS length must stay unchanged.
        with patch.object(sdd, 'run_live') as command:
            sdd.fit_staged_image('/scratch/source.img', '/scratch/fitted.img')
        argv = command.call_args.args[0]
        self.assertEqual(argv[0], 'virt-resize')
        self.assertIn('--shrink', argv)
        self.assertIn('--no-expand-content', argv)
        self.assertNotIn('--ntfsresize-force', argv)
        shrunk = {'ntfs_sectors': 600056}
        fitted = {'ntfs_sectors': 600056, 'sectors': 634624}
        sdd.assert_shrunk_copy(shrunk, fitted, 310*sdd.MIB)
        with self.assertRaisesRegex(sdd.SddError, 'length changed'):
            sdd.assert_shrunk_copy(shrunk, {'ntfs_sectors': 600057, 'sectors': 634624}, 310*sdd.MIB)
        with self.assertRaisesRegex(sdd.SddError, 'exceeds target partition'):
            sdd.assert_shrunk_copy(shrunk, fitted, 290*sdd.MIB)

    def test_minimum_parse_and_loop_cleanup(self):
        def fake_run(cmd):
            if cmd[0] == 'losetup' and '--detach' not in cmd:
                return '/dev/loop9\n'
            if cmd[0] == 'ntfsresize':
                return 'You might resize at 123456789 bytes or 124 MB\n'
            return ''
        with patch.object(sdd, 'require'), patch.object(sdd, 'run', side_effect=fake_run) as mocked, patch.object(sdd.Path, 'exists', return_value=True):
            self.assertEqual(sdd.ntfs_minimum('/scratch/raw.img', {} )[0], 123456789)
            self.assertEqual(mocked.call_args_list[-1].args[0], ['losetup', '--detach', '/dev/loop9'])

    def test_minimum_unknown_output_refused_and_detached(self):
        def fake_run(cmd):
            return '/dev/loop9\n' if cmd[0] == 'losetup' and '--detach' not in cmd else 'unknown'
        with patch.object(sdd, 'require'), patch.object(sdd, 'run', side_effect=fake_run) as mocked, patch.object(sdd.Path, 'exists', return_value=True):
            with self.assertRaises(sdd.SddError):
                sdd.ntfs_minimum('/scratch/raw.img', {})
            self.assertEqual(mocked.call_args_list[-1].args[0], ['losetup', '--detach', '/dev/loop9'])

    def test_preserve_boot_fields_in_parser(self):
        raw = self.dir/'disk.img'
        mbr, boot = synthetic(raw)
        info = sdd.image_info(raw)
        self.assertEqual(info['boot_code'], mbr[:440])
        self.assertEqual(info['signature'], b'ABCD'.hex())
        self.assertEqual(info['ntfs_serial'], b'SERIAL12'.hex())
        sdd.assert_preserved(info, dict(info))
        altered = dict(info)
        altered['signature'] = '00000000'
        with self.assertRaisesRegex(sdd.SddError, 'signature'):
            sdd.assert_preserved(info, altered)
        altered = dict(info)
        altered['start_chs'] = 'ffffff'
        with self.assertRaisesRegex(sdd.SddError, 'start_chs'):
            sdd.assert_preserved(info, altered)
        fitted = self.dir/'fitted.img'
        fitted.write_bytes(raw.read_bytes())
        with open(fitted, 'r+b') as f:
            f.seek(458)
            f.write(b'\x01')  # partition entry is expected to change
        sdd.compare_boot_region(raw, fitted, info['start'])
        with open(fitted, 'r+b') as f:
            f.seek(600)
            f.write(b'X')
        with self.assertRaisesRegex(sdd.SddError, 'bootloader'):
            sdd.compare_boot_region(raw, fitted, info['start'])

    def test_virt_resize_start_chs_is_restored_without_hiding_lba_change(self):
        source = self.dir/'source-mbr.bin'
        fitted = self.dir/'fitted-mbr.bin'
        original = bytearray(512)
        original[:440] = b'B' * 440
        original[440:444] = b'ABCD'
        original[446] = 0x80
        original[447:450] = bytes.fromhex('202100')  # LBA 2048 with 255/63 geometry
        original[450] = 0x07
        original[451:454] = bytes.fromhex('24ff03')
        struct.pack_into('<II', original, 454, 2048, 60000)
        original[510:512] = b'\x55\xaa'
        source.write_bytes(original)
        output = bytearray(original)
        output[447:450] = bytes.fromhex('040104')
        output[451:454] = bytes.fromhex('fec2ff')
        struct.pack_into('<I', output, 458, 40000)
        fitted.write_bytes(output)
        sdd.preserve_start_chs(source, fitted)
        repaired = fitted.read_bytes()
        self.assertEqual(repaired[447:450], original[447:450])
        self.assertEqual(repaired[451:454], output[451:454])
        self.assertEqual(repaired[458:462], output[458:462])
        self.assertEqual([i for i in range(512) if repaired[i] != output[i]], [447, 448, 449])
        parsed = sdd.parse_mbr(repaired, 64*sdd.MIB)
        self.assertEqual(parsed['start_chs'], '202100')
        self.assertEqual(parsed['end_chs'], 'fec2ff')
        output[454] ^= 1
        fitted.write_bytes(output)
        with self.assertRaisesRegex(sdd.SddError, 'start LBA'):
            sdd.preserve_start_chs(source, fitted)
        self.assertEqual(fitted.read_bytes(), output)

    def test_real_ntfs_fixture_uses_valid_bios_chs(self):
        fixture = runpy.run_path(str(ROOT/'tests/integration_real_ntfs.py'))
        encode = fixture['bios_chs']
        self.assertEqual(encode(2048), bytes.fromhex('202100'))
        self.assertEqual(encode(1046527), bytes.fromhex('242341'))
        image = self.dir/'fixture.img'
        fixture['make_mbr'](image)
        with image.open('rb') as f:
            mbr = f.read(512)
        self.assertEqual(mbr[447:450], encode(fixture['START']))
        self.assertEqual(mbr[451:454], encode(fixture['START'] + fixture['SECTORS'] - 1))
        self.assertNotEqual(mbr[447:450], b'\x00'*3)

    def test_confirmation(self):
        disk = {'path': '/dev/by-id/test', 'model': 'Mock', 'serial': 'MOCK123', 'size': 123}
        with patch('builtins.input', side_effect=['bad', 'y']):
            sdd.confirm(disk)
        with patch('builtins.input', return_value='Y'):
            sdd.confirm(disk)
        for answer in ('', 'n', 'N'):
            with patch('builtins.input', return_value=answer), self.assertRaises(sdd.Cancelled):
                sdd.confirm(disk)
        with patch('builtins.input', side_effect=['yes', 'n']), self.assertRaises(sdd.Cancelled):
            sdd.confirm(disk)
        with patch('builtins.input', side_effect=EOFError), self.assertRaises(sdd.Cancelled):
            sdd.confirm(disk)
        with patch('builtins.input', side_effect=KeyboardInterrupt), self.assertRaises(sdd.Cancelled):
            sdd.confirm(disk)

    def test_disk_refusals(self):
        with patch.object(sdd, 'require'), patch.object(sdd.Path, 'resolve', return_value=Path('/dev/mock')), patch.object(sdd.Path, 'stat') as st:
            st.return_value.st_mode = 0o100644
            with self.assertRaises(sdd.SddError):
                sdd.disk_info('/dev/mock')
        data = {'blockdevices': [{'path': '/dev/mock', 'type': 'disk', 'model': 'Mock', 'serial': '123', 'mountpoints': [None], 'children': [{'path': '/dev/mock1', 'type': 'part', 'mountpoints': ['/']}]}]}
        def disk_run(cmd):
            if cmd[0] == 'lsblk':
                return __import__('json').dumps(data)
            return '0' if '--getro' in cmd else '512'
        with patch.object(sdd, 'require'), patch.object(sdd.Path, 'resolve', return_value=Path('/dev/mock')), patch.object(sdd.Path, 'stat') as st, patch.object(sdd, 'run', side_effect=disk_run):
            st.return_value.st_mode = 0o060000
            with self.assertRaisesRegex(sdd.SddError, 'mounted'):
                sdd.disk_info('/dev/mock')

    def test_lsblk_tree_top_level_siblings_disk_detection(self):
        rows = {'blockdevices': [
            {'path': '/dev/mock1', 'type': 'part', 'pkname': 'mock', 'mountpoints': ['/boot'], 'children': []},
            {'path': '/dev/mock', 'type': 'disk', 'pkname': None, 'model': 'Mock SSD', 'serial': 'SER-1',
             'size': 200*sdd.MIB, 'mountpoints': [None], 'children': []},
        ]}
        calls = []
        def disk_run(cmd):
            calls.append(cmd)
            if cmd[0] == 'lsblk': return __import__('json').dumps(rows)
            if '--getro' in cmd: return '0'
            if '--getss' in cmd: return '512'
            if '--getsize64' in cmd: return str(200*sdd.MIB)
            return ''
        with patch.object(sdd, 'require'), patch.object(sdd.Path, 'resolve', return_value=Path('/dev/mock')), \
             patch.object(sdd.Path, 'stat') as st, patch.object(sdd, 'run', side_effect=disk_run), \
             patch.object(sdd.Path, 'exists', return_value=False):
            st.return_value.st_mode = 0o060000
            st.return_value.st_rdev = 99
            with self.assertRaisesRegex(sdd.SddError, 'mounted'):
                sdd.disk_info('/dev/mock')
        self.assertIn('-T', calls[0])
        self.assertIn('PKNAME', calls[0][calls[0].index('-o')+1])

    def test_destination_verification_error(self):
        a = self.dir/'a'; b = self.dir/'b'
        a.write_bytes(b'abc'); b.write_bytes(b'axc')
        with self.assertRaises(sdd.SddError):
            sdd.compare_files(a, b, 3)

    def test_staged_copy_digest_and_interruption(self):
        raw = self.dir/'disk.img'
        synthetic(raw)
        digest = sdd.copy_image(raw, self.dir/'copy.img', False, raw.stat().st_size)
        self.assertEqual(digest, __import__('hashlib').sha256(raw.read_bytes()).hexdigest())
        gz = self.dir/'disk.img.gz'
        with gzip.open(gz, 'wb') as f:
            f.write(raw.read_bytes())
        gz.write_bytes(gz.read_bytes()[:-4])
        with self.assertRaises((EOFError, gzip.BadGzipFile)):
            sdd.copy_image(gz, self.dir/'partial.img', True)
        self.assertTrue((self.dir/'partial.img').exists())
        clean_gz = self.dir/'clean.img.gz'
        with gzip.open(clean_gz, 'wb') as f:
            f.write(raw.read_bytes())
        with patch.object(sdd, 'progress', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                sdd.copy_image(clean_gz, self.dir/'interrupted.img', True)
        self.assertTrue((self.dir/'interrupted.img').exists())
        self.assertLess((self.dir/'interrupted.img').stat().st_size, raw.stat().st_size)

    def test_disposable_destination_write_verify_and_interruption(self):
        image = self.dir/'fitted.img'
        disk = self.dir/'disposable-destination.img'
        image.write_bytes(b'A'*(5*sdd.MIB))
        disk.write_bytes(b'X'*(6*sdd.MIB))
        with patch.object(sdd, 'progress', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                sdd.write_destination(image, disk, 5*sdd.MIB, 6*sdd.MIB)
        self.assertEqual(disk.read_bytes()[:sdd.CHUNK], b'A'*sdd.CHUNK)
        self.assertEqual(disk.read_bytes()[sdd.CHUNK:sdd.CHUNK+1], b'X')
        with patch.object(sdd, 'progress'):
            sdd.write_destination(image, disk, 5*sdd.MIB, 6*sdd.MIB)
            sdd.compare_files(image, disk, 5*sdd.MIB)
        sdd.verify_tail_zero(disk, 5*sdd.MIB, 6*sdd.MIB)

    def test_shrink_sequence_and_loop_cleanup(self):
        calls = []
        def fake_run(cmd):
            calls.append(cmd)
            return '/dev/loop9\n' if cmd[:2] == ['losetup', '--find'] else ''
        with patch.object(sdd, 'run', side_effect=fake_run), patch.object(sdd, 'run_live', side_effect=lambda c, **kwargs: calls.append(c)), patch.object(sdd.Path, 'exists', return_value=True):
            sdd.shrink_staged_ntfs('/scratch/raw.img', 123456)
        self.assertEqual(calls[1][:2], ['ntfsresize', '--no-action'])
        self.assertEqual(calls[2][:2], ['ntfsresize', '--force'])
        self.assertEqual(calls[3], ['losetup', '--detach', '/dev/loop9'])

    def test_destination_ntfs_metadata_uses_read_only_loop(self):
        calls = []
        def fake_run(cmd):
            calls.append(cmd)
            if cmd[:2] == ['losetup', '--find']:
                return '/dev/loop9\n'
            if cmd[0] == 'ntfsresize':
                return 'You might resize at 1234 bytes'
            return ''
        with patch.object(sdd, 'run', side_effect=fake_run):
            sdd.verify_ntfs_metadata('/dev/mock', {'start': 2048, 'sectors': 1000})
        self.assertIn('--read-only', calls[0])
        self.assertEqual(calls[1][:3], ['ntfsresize', '--info', '--force'])
        self.assertEqual(calls[-1], ['losetup', '--detach', '/dev/loop9'])

    def test_guestfish_file_backend_commands_and_parse(self):
        with patch.object(sdd, 'run', return_value='123456\n') as command:
            self.assertEqual(sdd.guestfish_minimum('/scratch/raw.img')[0], 123456)
            self.assertEqual(command.call_args.args[0],
                             ['guestfish', '--ro', '--format=raw', '-a', '/scratch/raw.img',
                              'run', ':', 'vfs-minimum-size', '/dev/sda1'])
        with patch.object(sdd, 'run', return_value='not a number'):
            with self.assertRaises(sdd.SddError):
                sdd.guestfish_minimum('/scratch/raw.img')
        with patch.object(sdd, 'run_live') as command:
            sdd.guestfish_shrink('/scratch/raw.img', 500000)
            self.assertEqual(command.call_args.args[0][-3:],
                             ['ntfsresize', '/dev/sda1', 'size:500000'])

    def test_dry_run_does_not_stage_or_write(self):
        raw = self.dir/'disk.img'
        synthetic(raw)
        info = sdd.image_info(raw)
        info['bytes'] = 100*sdd.GIB
        disk = {'path': '/dev/mock', 'model': 'Mock', 'serial': 'MOCK123', 'size': 30*sdd.GIB, 'devnum': 1}
        args = types.SimpleNamespace(image=str(raw), disk='/dev/mock', scratch=str(self.dir), dry_run=True, test_file_target=False, test_case_dir=None)
        def mount_run(cmd):
            return 'ext4' if 'FSTYPE' in cmd else str(self.dir)
        usage = types.SimpleNamespace(free=200*sdd.GIB)
        with patch.object(sdd, 'image_info', return_value=info), patch.object(sdd, 'disk_info', return_value=disk), patch.object(sdd, 'refuse_overlap'), patch.object(sdd, 'require'), patch.object(sdd, 'run', side_effect=mount_run), patch.object(sdd.shutil, 'disk_usage', return_value=usage), patch.object(sdd, 'copy_image') as copied:
            sdd.restore(args)
            copied.assert_not_called()

    def test_scratch_shortage_refused_before_staging(self):
        raw = self.dir/'disk.img'
        synthetic(raw)
        info = sdd.image_info(raw)
        info['bytes'] = 100*sdd.GIB
        disk = {'path': '/dev/mock', 'model': 'Mock', 'serial': 'MOCK123', 'size': 30*sdd.GIB, 'devnum': 1}
        args = types.SimpleNamespace(image=str(raw), disk='/dev/mock', scratch=str(self.dir), dry_run=False, test_file_target=False, test_case_dir=None)
        usage = types.SimpleNamespace(free=1)
        def mount_run(cmd):
            return 'ext4' if 'FSTYPE' in cmd else str(self.dir)
        with patch.object(sdd, 'image_info', return_value=info), patch.object(sdd, 'disk_info', return_value=disk), patch.object(sdd, 'refuse_overlap'), patch.object(sdd, 'require'), patch.object(sdd, 'run', side_effect=mount_run), patch.object(sdd.shutil, 'disk_usage', return_value=usage), patch.object(sdd, 'copy_image') as copied:
            with self.assertRaisesRegex(sdd.SddError, 'Insufficient scratch'):
                sdd.restore(args)
            copied.assert_not_called()

    def test_test_file_target_is_explicit_and_project_local(self):
        target = self.dir/'disposable.img'
        with open(target, 'xb') as f:
            f.truncate(128*sdd.MIB)
        info = sdd.test_file_info(target, self.dir)
        self.assertEqual(info['size'], 128*sdd.MIB)
        self.assertEqual(info['file_id'][1], target.stat().st_ino)
        with self.assertRaises(sdd.SddError):
            sdd.disk_info(target)
        link = self.dir/'link.img'
        link.symlink_to(target)
        with self.assertRaisesRegex(sdd.SddError, 'symlink'):
            sdd.test_file_info(link, self.dir)
        with self.assertRaisesRegex(sdd.SddError, 'same file'):
            sdd.check_test_file_paths(target, self.scratch, target, self.dir)
        with self.assertRaisesRegex(sdd.SddError, 'outside'):
            sdd.test_file_info(ROOT/'sdd.py', self.dir)

    def test_test_mode_rejects_aliases_and_cross_case_paths(self):
        source = self.dir/'source.img'
        target = self.dir/'target.img'
        synthetic(source)
        with open(target, 'xb') as f:
            f.truncate(128*sdd.MIB)
        sdd.check_test_file_paths(source, self.scratch, target, self.dir)
        nested = self.dir/'nested'
        nested.symlink_to(self.dir, target_is_directory=True)
        with self.assertRaisesRegex(sdd.SddError, 'symlink'):
            sdd.test_file_info(nested/'target.img', self.dir)
        source_link = self.dir/'source-link.img'
        source_link.symlink_to(source)
        with self.assertRaisesRegex(sdd.SddError, 'symlink'):
            sdd.check_test_file_paths(source_link, self.scratch, target, self.dir)
        with self.assertRaisesRegex(sdd.SddError, 'outside'):
            sdd.check_test_file_paths(source, self.scratch, ROOT/'sdd.py', self.dir)
        other = tempfile.TemporaryDirectory(dir=SANDBOX)
        self.addCleanup(other.cleanup)
        other_target = Path(other.name)/'target.img'
        with open(other_target, 'xb') as f:
            f.truncate(128*sdd.MIB)
        with self.assertRaisesRegex(sdd.SddError, 'outside'):
            sdd.check_test_file_paths(source, self.scratch, other_target, self.dir)
        with self.assertRaisesRegex(sdd.SddError, 'required'):
            sdd.test_file_info(target)

    def test_test_mode_rejects_nonregular_target_and_oversized_source(self):
        source = self.dir/'source.img'
        target = self.dir/'target.img'
        with open(source, 'xb') as f:
            f.truncate(sdd.TEST_MAX_SOURCE + 1)
        with open(target, 'xb') as f:
            f.truncate(128*sdd.MIB)
        with self.assertRaisesRegex(sdd.SddError, '2 GiB limit'):
            sdd.check_test_file_paths(source, self.scratch, target, self.dir)
        with patch.object(sdd.stat, 'S_ISREG', return_value=False):
            with self.assertRaisesRegex(sdd.SddError, 'regular file'):
                sdd.test_file_info(target, self.dir)

    def test_actual_cli_file_preflight_stops_before_write_without_ntfs_tools(self):
        # This is a real CLI safety test, not a genuine NTFS integration test.
        raw = self.dir/'preflight.img'
        synthetic(raw, size=256*sdd.MIB, sectors=500000)
        gz = self.dir/'preflight.img.gz'
        with gzip.open(gz, 'wb', compresslevel=1) as f, open(raw, 'rb') as source:
            while block := source.read(sdd.CHUNK):
                f.write(block)
        target = self.dir/'disposable.img'
        with open(target, 'xb') as f:
            f.write(b'UNCHANGED')
            f.truncate(160*sdd.MIB)
        def identity():
            with open(target, 'rb') as f:
                return target.stat().st_size, f.read(9)
        before = identity()
        def digest(path):
            h = __import__('hashlib').sha256()
            with open(path, 'rb') as f:
                for block in iter(lambda: f.read(sdd.CHUNK), b''):
                    h.update(block)
            return h.hexdigest()
        source_hashes = (digest(raw), digest(gz))
        script = ROOT/'sdd.py'
        for source in (raw, gz):
            common = [sys.executable, str(script), 'restore', str(source), str(target),
                      '--scratch', str(self.scratch), '--test-file-target', '--test-case-dir', str(self.dir)]
            dry = subprocess.run([*common, '--dry-run'], capture_output=True, text=True)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            # Force dependency preflight to fail even when NTFS tools are installed.
            execute = subprocess.run(common, capture_output=True, text=True,
                                     env={**os.environ, 'PATH': ''})
            self.assertNotEqual(execute.returncode, 0)
            self.assertIn('Missing required tools', execute.stderr)
            self.assertEqual(identity(), before)
        bad = self.dir/'corrupt.img.gz'
        damaged = bytearray(gz.read_bytes())
        damaged[-8] ^= 1
        bad.write_bytes(damaged)
        refused = subprocess.run([sys.executable, str(script), 'restore', str(bad), str(target),
                                  '--scratch', str(self.scratch), '--test-file-target', '--test-case-dir', str(self.dir), '--dry-run'],
                                 capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('gzip integrity failure', refused.stderr)
        self.assertEqual(identity(), before)
        small = self.dir/'small-target.img'
        with open(small, 'xb') as f:
            f.write(b'UNCHANGED')
            f.truncate(64*sdd.MIB)
        refused = subprocess.run([sys.executable, str(script), 'restore', str(raw), str(small),
                                  '--scratch', str(self.scratch), '--test-file-target', '--test-case-dir', str(self.dir), '--dry-run'],
                                 capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('too small', refused.stderr)
        with open(small, 'rb') as f:
            self.assertEqual(f.read(9), b'UNCHANGED')
        self.assertEqual((digest(raw), digest(gz)), source_hashes)

    def test_resize_failure_and_scratch_enospc_leave_target_unchanged(self):
        raw = self.dir/'failure.img'
        synthetic(raw, size=256*sdd.MIB, sectors=500000)
        target = self.dir/'disposable.img'
        with open(target, 'xb') as f:
            f.write(b'UNCHANGED')
            f.truncate(160*sdd.MIB)
        args = types.SimpleNamespace(image=str(raw), disk=str(target), scratch=str(self.scratch),
                                     dry_run=False, test_file_target=True, test_case_dir=str(self.dir))
        def identity():
            st = target.stat()
            with open(target, 'rb') as f:
                return st.st_ino, st.st_mtime_ns, st.st_size, f.read(9)
        before = identity()
        def command_output(cmd):
            return '4194304\n' if cmd[0] == 'guestfish' else 'ntfs\n'
        with patch.object(sdd, 'require'), patch.object(sdd, 'run', side_effect=command_output), \
             patch.object(sdd, 'run_with_output_status', side_effect=lambda cmd, **kwargs: command_output(cmd)), \
             patch.object(sdd, 'guestfish_shrink', side_effect=sdd.SddError('resize failed')), \
             patch.object(sdd, 'write_destination') as writer, patch.object(sdd, 'progress'):
            with self.assertRaisesRegex(sdd.SddError, 'resize failed'):
                sdd.restore(args)
            writer.assert_not_called()
        self.assertEqual(identity(), before)
        with patch.object(sdd, 'require'), patch.object(sdd, 'run', side_effect=command_output), \
             patch.object(sdd, 'run_with_output_status', side_effect=lambda cmd, **kwargs: command_output(cmd)), \
             patch.object(sdd, 'copy_image', side_effect=OSError(errno.ENOSPC, 'no space')), \
             patch.object(sdd, 'write_destination') as writer:
            with self.assertRaises(OSError) as raised:
                sdd.restore(args)
            self.assertEqual(raised.exception.errno, errno.ENOSPC)
            writer.assert_not_called()
        self.assertEqual(identity(), before)

    def test_cli(self):
        script = ROOT/'sdd.py'
        for args, code in ((['--help'], 0), (['--version'], 0), (['inspect', '/missing.img'], 1)):
            p = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True)
            self.assertEqual(p.returncode, code, p.stderr)
        self.assertIn('restore', subprocess.run([sys.executable, str(script), '--help'], capture_output=True, text=True).stdout)

    def test_cli_version_resume_and_interactive_menu(self):
        script = ROOT/'sdd.py'
        version = subprocess.run([sys.executable, str(script), '--version'], capture_output=True, text=True)
        self.assertEqual(version.returncode, 0)
        self.assertEqual(version.stdout.strip(), sdd.VERSION)
        self.assertIn('resume', subprocess.run([sys.executable, str(script), '--help'], capture_output=True, text=True).stdout)
        class Tty(io.StringIO):
            def isatty(self): return True
        with patch.object(sdd.sys, 'stdin', Tty('1\nsource.img\n/dev/disk/by-id/test\n/mnt/scratch\n')), \
             patch.object(sdd, 'restore') as restore:
            self.assertEqual(sdd.main([]), 0)
            self.assertEqual(restore.call_args.args[0].image, 'source.img')

    def test_checkpoint_resume_validates_hash_layout_and_confines_file(self):
        raw = self.dir/'source.img'
        _, _ = synthetic(raw)
        job = self.scratch/'sdd-job'
        job.mkdir(mode=0o700)
        fitted = job/'fitted.img'
        synthetic(fitted)
        layout = sdd.image_info(fitted)
        disk = {'path': '/dev/mock', 'model': 'Mock', 'serial': 'SER', 'size': 100*sdd.MIB}
        sdd.write_checkpoint(job, source=raw, scratch=self.scratch, target=fitted.stat().st_size,
                             disk=disk, destination_requested='/dev/disk/by-id/mock',
                             layout=layout, test_mode=False, test_case_dir=None)
        data, actual, observed = sdd.read_checkpoint(job)
        self.assertEqual(actual, fitted)
        self.assertEqual(observed['signature'], layout['signature'])
        with fitted.open('r+b') as stream:
            stream.seek(1000); stream.write(b'changed')
        with self.assertRaisesRegex(sdd.SddError, 'checksum'):
            sdd.read_checkpoint(job)

    def test_checkpoint_refuses_symlink_and_external_fitted_path(self):
        job = self.scratch/'sdd-job'
        job.mkdir(mode=0o700)
        outside = self.dir/'outside.img'
        synthetic(outside)
        (job/'fitted.img').symlink_to(outside)
        (job/'job.json').write_text(__import__('json').dumps({'version': sdd.CHECKPOINT_VERSION,
                                                                'fitted': str(outside)}))
        with self.assertRaisesRegex(sdd.SddError, 'fitted image'):
            sdd.read_checkpoint(job)

    def test_resume_cancellation_preserves_target_then_same_job_verifies_write(self):
        source = self.dir/'source.img'
        synthetic(source)
        target = self.dir/'resume-target.img'
        with target.open('xb') as stream:
            stream.write(b'KEEP-ME')
            stream.truncate(128*sdd.MIB)
        job = self.scratch/'sdd-resume'
        job.mkdir(mode=0o700)
        fitted = job/'fitted.img'
        synthetic(fitted)
        fitted_info = sdd.image_info(fitted)
        target_info = sdd.test_file_info(target, self.dir)
        sdd.write_checkpoint(job, source=source, scratch=self.scratch,
                             target=fitted.stat().st_size, disk=target_info,
                             destination_requested=str(target), layout=fitted_info,
                             test_mode=True, test_case_dir=str(self.dir))
        before = target.read_bytes()[:7]
        args = types.SimpleNamespace(job=str(job))
        with patch.object(sdd, 'require'), patch.object(sdd, 'guestfish_verify'), \
             patch.object(sdd, 'confirm', side_effect=sdd.Cancelled('declined')):
            with self.assertRaises(sdd.Cancelled):
                sdd.resume(args)
        self.assertEqual(target.read_bytes()[:7], before)
        self.assertTrue((job/'job.json').is_file())
        args = types.SimpleNamespace(job=str(job))
        with patch.object(sdd, 'require'), patch.object(sdd, 'guestfish_verify'), \
             patch.object(sdd, 'confirm', return_value=None):
            sdd.resume(args)
        with target.open('rb') as actual, fitted.open('rb') as expected:
            self.assertEqual(actual.read(fitted.stat().st_size), expected.read())

    def test_s_dd_entry_point_and_doctor(self):
        metadata = (ROOT/'pyproject.toml').read_text()
        self.assertIn('s_dd = "sdd:main"', metadata)
        self.assertNotIn('\nsdd = "sdd:main"', metadata)
        help_result = subprocess.run([sys.executable, str(ROOT/'sdd.py'), '--help'],
                                     capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('usage: s_dd', help_result.stdout)
        self.assertIn('doctor', help_result.stdout)
        with patch.object(sdd.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(sdd.SddError, 'Missing software'):
                sdd.doctor(types.SimpleNamespace())
        with patch.object(sdd.shutil, 'which', return_value='/usr/bin/fake'), \
             patch.object(sdd, 'run') as launch:
            sdd.doctor(types.SimpleNamespace())
            launch.assert_not_called()

    def test_progress_tty_and_redirected_counters(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        tty = Tty()
        sdd._last_status.clear()
        with patch.object(sdd.sys, 'stderr', tty), patch.object(sdd.time, 'monotonic', return_value=10):
            sdd.progress('Copy', 5*sdd.MIB, 10*sdd.MIB, 0)
        self.assertIn('50.0%', tty.getvalue())
        self.assertIn('5,242,880 bytes', tty.getvalue())
        self.assertIn('ETA', tty.getvalue())
        self.assertIn('\r', tty.getvalue())
        redirected = io.StringIO()
        sdd._last_status.clear()
        with patch.object(sdd.sys, 'stderr', redirected), patch.object(sdd.time, 'monotonic', return_value=10):
            sdd.progress('Copy', 5*sdd.MIB, 10*sdd.MIB, 0)
            sdd.progress('Copy', 6*sdd.MIB, 10*sdd.MIB, 0)
            sdd.progress('Copy', 10*sdd.MIB, 10*sdd.MIB, 0)
        self.assertEqual(redirected.getvalue().count('Copy:'), 2)
        self.assertIn('100.0%', redirected.getvalue())
        self.assertNotIn('\r', redirected.getvalue())

    def test_subprocess_status_failure_and_interrupt(self):
        log = self.dir/'operations.log'
        with self.assertRaisesRegex(sdd.SddError, r'failed \(7\)'):
            sdd.run_with_status([sys.executable, '-c', 'import sys; print("failure detail"); sys.exit(7)'],
                                label='Safe helper', log_path=log)
        self.assertIn(b'failure detail', log.read_bytes())
        proc = unittest.mock.Mock()
        proc.pid = 12345
        proc.poll.return_value = None
        proc.wait.side_effect = [KeyboardInterrupt(), 0]
        with patch.object(sdd.subprocess, 'Popen', return_value=proc), \
             patch.object(sdd.os, 'killpg') as kill:
            with self.assertRaises(KeyboardInterrupt):
                sdd.run_with_status(['fake-helper'], label='Interrupted', log_path=log)
        kill.assert_called_once_with(12345, sdd.signal.SIGTERM)

    def test_spinner_during_failing_helper(self):
        log = self.dir/'operations.log'
        proc = unittest.mock.Mock()
        proc.wait.side_effect = [subprocess.TimeoutExpired('fake-helper', 0.2), 9]
        tty = io.StringIO()
        tty.isatty = lambda: True
        with patch.object(sdd.subprocess, 'Popen', return_value=proc), \
             patch.object(sdd.sys, 'stderr', tty):
            with self.assertRaisesRegex(sdd.SddError, r'failed \(9\)'):
                sdd.run_with_status(['fake-helper'], label='Fitting image', log_path=log)
        self.assertIn('Fitting image', tty.getvalue())
        self.assertIn('\r', tty.getvalue())

    def test_fitted_validation_failure_never_opens_target_for_write(self):
        raw = self.dir/'source.img'
        synthetic(raw, size=256*sdd.MIB, sectors=500000)
        target = self.dir/'target.img'
        with target.open('xb') as f:
            f.write(b'UNCHANGED')
            f.truncate(160*sdd.MIB)
        args = types.SimpleNamespace(image=str(raw), disk=str(target), scratch=str(self.scratch),
                                     dry_run=False, test_file_target=True, test_case_dir=str(self.dir))
        base = sdd.image_info(raw)
        shrunk = dict(base, ntfs_sectors=200000)
        result = dict(shrunk, sectors=300000)
        with patch.object(sdd, 'require'), patch.object(sdd, 'run', return_value='ntfs\n'), \
             patch.object(sdd, 'image_info', side_effect=[base, base, shrunk, result]), \
             patch.object(sdd, 'copy_image', return_value='ignored'), \
             patch.object(sdd, 'guestfish_minimum', return_value=(4*sdd.MIB, 'minimum')), \
             patch.object(sdd, 'guestfish_shrink'), patch.object(sdd, 'fit_staged_image'), \
             patch.object(sdd, 'preserve_start_chs'), \
             patch.object(sdd, 'assert_shrunk_copy', side_effect=sdd.SddError('bad fitted NTFS')), \
             patch.object(sdd, 'write_destination') as writer:
            with self.assertRaisesRegex(sdd.SddError, 'bad fitted NTFS'):
                sdd.restore(args)
            writer.assert_not_called()
        with target.open('rb') as f:
            self.assertEqual(f.read(9), b'UNCHANGED')

if __name__ == '__main__':
    unittest.main()
