import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import lean_verify as verify


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.build = self.base / 'build'
        self.judge = self.base / 'judge'
        exporter = self.base / 'exporter-source/lean4export'
        exporter.parent.mkdir()
        binaries = {'build_lean': self.build / 'bin/lean', 'build_lake': self.build / 'bin/lake',
                    'judge_lean': self.judge / 'bin/lean', 'judge_lake': self.judge / 'bin/lake',
                    'leanchecker': self.judge / 'bin/leanchecker', 'exporter': exporter,
                    'bwrap': self.base / 'bwrap'}
        for name, path in binaries.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#!/bin/sh\necho ' + name + '\n')
            path.chmod(0o700)
        self.value = {'version': 1, 'build_prefix': str(self.build), 'judge_prefix': str(self.judge),
                      'exporter': str(exporter), 'exporter_source': str(exporter.parent),
                      'exporter_commit': verify.EXPORTER_COMMIT, 'bwrap': str(binaries['bwrap']),
                      'tool_digests': {name: verify.proofloom.digest(path) for name, path in binaries.items()},
                      'architecture': 'aarch64',
                      'runtime_payload': {'build': verify.setup_lean.payload_digest(self.build),
                                          'judge': verify.setup_lean.payload_digest(self.judge)},
                      'release_sha256': {version: verify.setup_lean.RELEASE_SHA256[(version, 'aarch64')]
                                         for version in ('4.33.1', '4.36.0')}}
        self.config = self.base / 'runtime.json'
        self.config.write_text(json.dumps(self.value))

    def runtime(self):
        with patch.object(verify.proofloom, 'git', return_value=verify.EXPORTER_COMMIT), \
                patch.object(verify.platform, 'machine', return_value='aarch64'):
            return verify.load_runtime(self.config)

    def test_runtime_binds_real_executables_and_exporter_revision(self):
        runtime = self.runtime()
        self.assertEqual(runtime['_tools']['build_lean'], self.build / 'bin/lean')
        (self.build / 'bin/lean').write_text('modified executable')
        with patch.object(verify.proofloom, 'git', return_value=verify.EXPORTER_COMMIT), \
                patch.object(verify.platform, 'machine', return_value='aarch64'):
            with self.assertRaisesRegex(verify.proofloom.Invalid, 'changed'):
                verify.load_runtime(self.config)

    def test_wrong_exporter_pin_missing_binary_and_unknown_config_fail(self):
        for mutate in (lambda v: v.update(exporter_commit='0' * 40),
                       lambda v: v.update(exporter='/missing'),
                       lambda v: v.update(system_roots=['/'])):
            value = json.loads(json.dumps(self.value))
            mutate(value)
            self.config.write_text(json.dumps(value))
            with self.assertRaises((OSError, ValueError)):
                self.runtime()

    def test_runtime_rejects_modified_core_library_and_unofficial_release_digest(self):
        library = self.build / 'lib/lean/Init.olean'
        library.parent.mkdir(parents=True)
        library.write_bytes(b'original compiled core')
        self.value['runtime_payload']['build'] = verify.setup_lean.payload_digest(self.build)
        self.config.write_text(json.dumps(self.value))
        self.runtime()
        library.write_bytes(b'modified compiled core')
        with self.assertRaisesRegex(ValueError, 'compiled core changed'):
            self.runtime()
        library.write_bytes(b'original compiled core')
        self.value['release_sha256']['4.33.1'] = 'a' * 64
        self.config.write_text(json.dumps(self.value))
        with self.assertRaisesRegex(ValueError, 'official pin'):
            self.runtime()

    def test_sandbox_has_only_explicit_mounts_and_treats_paths_as_data(self):
        runtime = self.runtime()
        work = self.base / "work ' $(touch unwanted)"
        work.mkdir()
        command = [str(runtime['_tools']['build_lake']), 'build', 'Main']
        argv = verify.sandbox(runtime, work, command)
        self.assertIn('--clearenv', argv)
        self.assertIn('--unshare-all', argv)
        self.assertNotIn('/', argv)
        self.assertEqual(argv[-3:], command)
        self.assertIn(str(work.resolve()), argv)
        self.assertNotIn(str(self.base), argv)
        self.assertNotIn(str(self.judge), argv)
        self.assertIn('LEAN_NUM_THREADS', argv)
        self.assertFalse((self.base / 'unwanted').exists())

    def test_tool_mount_cannot_expose_host_home_or_root(self):
        for path in ('/', str(Path.home())):
            with self.assertRaisesRegex(ValueError, 'dedicated'):
                verify.tool_mount(path)

    def test_full_resource_guard_is_real_and_small_smoke_is_allowed(self):
        with patch.object(verify, 'available_memory_mib', return_value=6144):
            with self.assertRaisesRegex(verify.proofloom.Invalid, '256 GiB'):
                verify.budgets(180, verify.FULL_MEMORY_MIB, 1, full=True)
        self.assertEqual(verify.budgets(180, 2048, 1)['memory_mib'], 2048)
        for timeout in (0, -1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                verify.budgets(timeout, 2048, 1)

    def test_resource_scope_binds_memory_swap_cpu_without_operator_shell(self):
        with patch.object(verify.sys, 'platform', 'linux'), \
                patch.object(verify.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name):
            argv, unit = verify.memory_scope(['/bin/echo', 'space $()'], verify.budgets(30, 2048, 2))
        self.assertIn('MemoryMax=2147483648', argv)
        self.assertIn('MemorySwapMax=0', argv)
        self.assertIn('CPUQuota=200%', argv)
        self.assertTrue(unit.endswith('.scope'))
        self.assertEqual(argv[-2:], ['/bin/echo', 'space $()'])

    def test_output_refusal_preserves_inputs_and_existing_data(self):
        existing = self.base / 'existing'
        existing.mkdir()
        (existing / 'keep').write_text('data')
        with self.assertRaises(ValueError):
            verify.new_output(existing)
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            verify.new_output(existing / 'child', (existing,))
        self.assertEqual((existing / 'keep').read_text(), 'data')
        self.assertFalse((existing / 'child').exists())

    def test_build_workspace_ignores_unbound_root_cache_and_copies_bound_inputs(self):
        source = self.base / 'candidate'
        (source / 'src').mkdir(parents=True)
        (source / 'src/Main.lean').write_text('theorem original : True := True.intro\n')
        for name in verify.proofloom.CONFIGS:
            (source / name).write_text(name + '\n')
        root_cache = source / '.lake/build/lib/lean'
        root_cache.mkdir(parents=True)
        (root_cache / 'Main.olean').write_text('unbound stale proof')
        packages = source / '.lake/packages/mathlib'
        packages.mkdir(parents=True)
        (packages / 'cache').write_text('prepared dependency cache')
        destination = self.base / 'work'
        verify.build_workspace(source, destination)
        self.assertFalse((destination / '.lake/build').exists())
        self.assertEqual(verify.proofloom.candidate_identity(source),
                         verify.proofloom.candidate_identity(destination))
        self.assertEqual((destination / '.lake/packages/mathlib/cache').read_text(), 'prepared dependency cache')

    def test_package_identity_binds_native_cache_and_rejects_ignored_source(self):
        checkout = self.base / 'checkout'
        package = checkout / '.lake/packages/mathlib'
        (package / '.lake/build/lib/lean').mkdir(parents=True)
        (package / '.lake/build/lib/lean/Init.olean').write_bytes(b'compiled Lean')
        plugin = package / '.lake/build/lib/plugin.so'
        plugin.write_bytes(b'original native plugin')
        (package / 'Source.lean').write_text('def source := 1\n')
        (package / '.gitignore').write_text('.lake/\nIgnored.lean\n')
        commands = (['init', '-q'], ['add', '.'], ['-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'])
        for command in commands:
            subprocess.run(['git', '-C', str(package), *command], check=True, capture_output=True)
        revision = verify.proofloom.git(package, 'rev-parse', 'HEAD')
        (checkout / 'lake-manifest.json').write_text(json.dumps({'packages': [
            {'type': 'git', 'name': 'mathlib', 'rev': revision}]}))
        before = verify.package_identity(checkout)
        plugin.write_bytes(b'modified native plugin')
        self.assertNotEqual(before, verify.package_identity(checkout))
        (package / 'Ignored.lean').write_text('axiom injected : False\n')
        with self.assertRaisesRegex(ValueError, 'ignored untracked'):
            verify.package_identity(checkout)

    def test_process_records_real_failure_and_kills_only_its_timed_out_group(self):
        failure = verify.run_process([sys.executable, '-c', 'print("failure"); raise SystemExit(7)'],
                                     self.base / 'failure.log', verify.budgets(5, 512, 1), enforce_memory=False)
        self.assertEqual(failure['outcome'], 'failed')
        self.assertEqual(failure['exit_code'], 7)
        self.assertIn('failure', (self.base / 'failure.log').read_text())
        expired = verify.run_process([sys.executable, '-c', 'import time; time.sleep(30)'],
                                     self.base / 'timeout.log', verify.budgets(0.15, 512, 1), enforce_memory=False)
        self.assertEqual(expired['outcome'], 'interrupted')
        self.assertTrue(expired['timed_out'])
        self.assertEqual(expired['exit_code'], -signal.SIGKILL)

    def test_failed_export_preserves_partial_artifact_without_publishing_success(self):
        runtime = self.runtime()
        work = self.base / 'work'
        work.mkdir()
        with patch.object(verify, 'sandbox', return_value=[sys.executable, '-c',
                                                         'print("partial export"); raise SystemExit(9)']), \
                patch.object(verify, 'memory_scope', side_effect=lambda command, budget: (command, None)):
            result = verify.stage(runtime, self.base, 'export', work, [], verify.budgets(5, 512, 1), export=True)
        self.assertEqual(result['outcome'], 'failed')
        self.assertFalse((self.base / 'export.export').exists())
        self.assertIn('partial export', (self.base / 'export.partial').read_text())
        self.assertEqual(json.loads((self.base / 'export.json').read_text())['exit_code'], 9)

    def test_doctor_cannot_claim_readiness_on_macos(self):
        with patch.object(verify.sys, 'platform', 'darwin'):
            with self.assertRaisesRegex(ValueError, 'Linux'):
                verify.doctor(self.config)


if __name__ == '__main__':
    unittest.main()
