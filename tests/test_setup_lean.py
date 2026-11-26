import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import setup_lean as setup


def member(name, kind=tarfile.REGTYPE, link=''):
    value = tarfile.TarInfo(name)
    value.type = kind
    value.linkname = link
    return value


class SetupTests(unittest.TestCase):
    def test_supported_platform_and_release_selection(self):
        self.assertEqual(setup.architecture('Linux', 'arm64'), 'aarch64')
        self.assertEqual(setup.architecture('Linux', 'amd64'), 'x86_64')
        self.assertEqual(setup.release_name('4.33.1', 'aarch64'), 'lean-4.33.1-linux_aarch64')
        for system, machine in [('Darwin', 'arm64'), ('Linux', 'riscv64')]:
            with self.subTest(system=system, machine=machine):
                with self.assertRaises(setup.Invalid):
                    setup.architecture(system, machine)

    def test_hash_uses_bounded_reads(self):
        class Bounded(io.BytesIO):
            def read(self, size=-1):
                if not 0 < size <= setup.CHUNK:
                    raise AssertionError('unbounded artifact read')
                return super().read(size)
        data = b'content' * (setup.CHUNK // 3)
        with patch('builtins.open', return_value=Bounded(data)):
            self.assertEqual(setup.digest('release'), hashlib.sha256(data).hexdigest())

    def test_cached_archive_is_checked_without_download_or_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            path = base / 'lean-4.33.1-linux_aarch64.tar.zst'
            path.write_bytes(b'cached bytes')
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.dict(setup.RELEASE_SHA256, {('4.33.1', 'aarch64'): expected}):
                with patch.object(setup, 'urlopen', side_effect=AssertionError('network should not run')):
                    self.assertEqual(setup.download_release(base, '4.33.1', 'aarch64'), path)
            with self.assertRaisesRegex(setup.Invalid, 'checksum mismatch'):
                setup.download_release(base, '4.33.1', 'aarch64')
            self.assertEqual(path.read_bytes(), b'cached bytes')

    def test_failed_download_removes_only_owned_partial_file(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            sentinel = base / 'keep'
            sentinel.write_text('user data')
            with patch.object(setup, 'urlopen', return_value=io.BytesIO(b'corrupt release')):
                with self.assertRaisesRegex(setup.Invalid, 'checksum mismatch'):
                    setup.download_release(base, '4.33.1', 'aarch64')
            self.assertEqual([p.name for p in base.iterdir()], ['keep'])
            self.assertEqual(sentinel.read_text(), 'user data')

    def test_archive_topology_accepts_local_links_and_rejects_escape(self):
        release = 'lean-4.33.1-linux_aarch64'
        setup.validate_members([member(release, tarfile.DIRTYPE),
                                member(release + '/lib/libLean.so'),
                                member(release + '/lib/libLean.so.1', tarfile.SYMTYPE, 'libLean.so')], release)
        cases = [
            [member('/tmp/outside')],
            [member(release + '/../../outside')],
            [member('wrong-release/bin/lean')],
            [member(release + '/bin/evil', tarfile.SYMTYPE, '/etc/passwd')],
            [member(release + '/bin/evil', tarfile.SYMTYPE, '../../../outside')],
            [member(release + '/dev', tarfile.CHRTYPE)],
            [member(release + '/lib', tarfile.SYMTYPE, 'other'), member(release + '/lib/file')],
            [member(release + '/one'), member(release + '/one')],
            [member(release + '/evil', tarfile.LNKTYPE, 'outside/file')],
        ]
        for case in cases:
            with self.subTest(names=[m.name for m in case]):
                with self.assertRaises(setup.Invalid):
                    setup.validate_members(case, release)

    def test_runtime_config_binds_actual_required_and_available_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            build, judge = base / 'build', base / 'judge'
            tools = [build / 'bin/lean', build / 'bin/lake', judge / 'bin/lean', judge / 'bin/lake',
                     judge / 'bin/leanchecker', judge / 'bin/leanchecker-paranoid',
                     base / 'exporter', base / 'bwrap']
            for path in tools:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(path.name.encode())
                path.chmod(0o755)
            value = setup.emit_config(base, build, judge, base / 'exporter', base / 'source',
                                      base / 'bwrap', 'aarch64')
            self.assertEqual(value['version'], 1)
            self.assertEqual(value['architecture'], 'aarch64')
            self.assertEqual(value['exporter_commit'], setup.EXPORTER_COMMIT)
            self.assertEqual(value['release_sha256'], {
                version: setup.RELEASE_SHA256[(version, 'aarch64')] for version in setup.VERSIONS})
            self.assertEqual(value['runtime_payload'], {
                'build': setup.payload_digest(build), 'judge': setup.payload_digest(judge)})
            self.assertEqual(value['tool_digests']['build_lean'], hashlib.sha256(b'lean').hexdigest())
            self.assertIn('leanchecker_paranoid', value['tool_digests'])
            self.assertNotIn('nanoda', value['tool_digests'])
            (judge / 'bin/leanchecker').unlink()
            with self.assertRaisesRegex(setup.Invalid, 'missing executable: leanchecker'):
                setup.emit_config(base, build, judge, base / 'exporter', base / 'source',
                                  base / 'bwrap', 'aarch64')

    def test_payload_binds_compiled_and_native_libraries_without_loading_them(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix = Path(temp) / 'toolchain'
            compiled = prefix / 'lib/lean/Init.olean'
            native = prefix / 'lib/libLean.so'
            compiled.parent.mkdir(parents=True)
            compiled.write_bytes(b'compiled definitions')
            native.write_bytes(b'native library')
            original = setup.payload_digest(prefix)
            compiled.write_bytes(b'modified compiled definitions')
            self.assertNotEqual(setup.payload_digest(prefix), original)
            compiled.write_bytes(b'compiled definitions')
            self.assertEqual(setup.payload_digest(prefix), original)
            native.write_bytes(b'modified native library')
            self.assertNotEqual(setup.payload_digest(prefix), original)

    def test_payload_refuses_symlink_escape_and_accepts_contained_library_link(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            prefix = base / 'toolchain'
            prefix.mkdir()
            inside = prefix / 'library'
            inside.write_bytes(b'library')
            link = prefix / 'library-link'
            link.symlink_to('library')
            self.assertEqual(len(setup.payload_digest(prefix)), 64)
            link.unlink()
            outside = base / 'outside'
            outside.write_bytes(b'not part of runtime')
            link.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'escapes tree'):
                setup.payload_digest(prefix)

    def test_existing_destination_and_missing_prerequisites_fail_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with patch.object(setup, 'architecture', return_value='aarch64'):
                with patch.object(setup, 'urlopen', side_effect=AssertionError('unexpected network')):
                    with self.assertRaisesRegex(setup.Invalid, 'already exists'):
                        setup.setup(base)
                    with patch.object(setup.shutil, 'which', return_value=None):
                        with self.assertRaisesRegex(setup.Invalid, 'missing prerequisite: git'):
                            setup.setup(base / 'new-runtime')
            self.assertFalse((base / 'new-runtime').exists())

    def test_wrong_exporter_pin_cleans_only_owned_temporary_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / 'new-runtime'
            sentinel = base / 'keep'
            sentinel.write_text('untouched')
            def fake_extract(archive, directory, release):
                prefix = directory / release
                prefix.mkdir()
                return prefix
            with patch.object(setup, 'architecture', return_value='aarch64'), \
                    patch.object(setup.shutil, 'which', return_value='/usr/bin/true'), \
                    patch.object(setup, 'download_release', return_value=base / 'archive'), \
                    patch.object(setup, 'extract_release', side_effect=fake_extract), \
                    patch.object(setup.subprocess, 'run'), \
                    patch.object(setup.subprocess, 'check_output', return_value='0' * 40):
                with self.assertRaisesRegex(setup.Invalid, 'wrong exporter commit'):
                    setup.setup(root)
            self.assertEqual([p.name for p in base.iterdir()], ['keep'])
            self.assertEqual(sentinel.read_text(), 'untouched')

    def test_relocated_exporter_failure_does_not_publish_a_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / 'new-runtime'
            marker = base / 'keep'
            marker.write_text('user data')

            def executable(path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fixture executable')
                path.chmod(0o755)

            def fake_extract(archive, directory, release):
                prefix = directory / release
                for name in ('lean', 'lake', 'leanchecker'):
                    executable(prefix / 'bin' / name)
                return prefix

            def fake_run(argv, **kwargs):
                if argv[:2] == ['git', 'clone']:
                    Path(argv[-1]).mkdir()
                elif len(argv) > 1 and argv[1] == 'build':
                    executable(Path(kwargs['cwd']) / '.lake/build/bin/lean4export')
                elif argv[1:] == ['Init', '--', 'Nat.add']:
                    raise subprocess.CalledProcessError(1, argv)

            def fake_output(argv, **kwargs):
                if argv[0] == 'git':
                    return setup.EXPORTER_COMMIT + '\n'
                version = '4.33.1' if '4.33.1' in argv[0] else '4.36.0'
                return 'Lean (version ' + version + ')\n'

            with patch.object(setup, 'architecture', return_value='aarch64'), \
                    patch.object(setup.shutil, 'which', return_value='/usr/bin/true'), \
                    patch.object(setup, 'download_release', return_value=base / 'archive'), \
                    patch.object(setup, 'extract_release', side_effect=fake_extract), \
                    patch.object(setup.subprocess, 'run', side_effect=fake_run), \
                    patch.object(setup.subprocess, 'check_output', side_effect=fake_output):
                with self.assertRaises(subprocess.CalledProcessError):
                    setup.setup(root)
            self.assertFalse(root.exists())
            self.assertEqual([p.name for p in base.iterdir()], ['keep'])


if __name__ == '__main__':
    unittest.main()
