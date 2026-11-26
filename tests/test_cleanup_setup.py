import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools' / 'cleanup_setup.py'
SPEC = importlib.util.spec_from_file_location('cleanup_setup', SCRIPT)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


class SetupCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.build = self.base / 'build'

    def pair(self, name, trace_time=None):
        setup = self.build / 'ir' / (name + '.setup.json')
        trace = self.build / 'lib' / 'lean' / (name + '.trace')
        setup.parent.mkdir(parents=True, exist_ok=True)
        setup.write_bytes(b'setup data')
        os.utime(setup, ns=(1_000_000_000, 1_000_000_000))
        if trace_time is not None:
            trace.parent.mkdir(parents=True, exist_ok=True)
            trace.write_bytes(b'completed module trace')
            os.utime(trace, ns=(trace_time, trace_time))
        return setup, trace

    def test_only_a_newer_completed_trace_removes_setup(self):
        completed, _ = self.pair('Project/Nested/Complete', 2_000_000_000)
        older, _ = self.pair('Project/Older', 999_999_999)
        same, _ = self.pair('Project/Same', 1_000_000_000)
        missing, _ = self.pair('Project/Missing')
        not_regular, trace_directory = self.pair('Project/NotRegular')
        trace_directory.parent.mkdir(parents=True, exist_ok=True)
        trace_directory.mkdir()
        unrelated = self.build / 'ir' / 'keep.json'
        unrelated.write_bytes(b'user data')
        self.assertEqual(cleanup.clean_setup(self.build), {'examined': 5, 'removed': 1, 'kept': 4})
        self.assertFalse(completed.exists())
        for path in (older, same, missing, not_regular, unrelated):
            self.assertTrue(path.is_file())

    def test_symlinked_setup_trace_and_directory_cannot_delete_external_data(self):
        external = self.base / 'outside'
        external.mkdir()
        outside_setup = external / 'Outside.setup.json'
        outside_setup.write_bytes(b'external setup')
        outside_trace = external / 'Outside.trace'
        outside_trace.write_bytes(b'external trace')
        os.utime(outside_trace, ns=(3_000_000_000, 3_000_000_000))
        setup, trace = self.pair('TraceLink')
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.symlink_to(outside_trace)
        linked_setup, _ = self.pair('SetupLink', 3_000_000_000)
        linked_setup.unlink()
        linked_setup.symlink_to(outside_setup)
        (self.build / 'ir' / 'directory-link').symlink_to(external, target_is_directory=True)
        self.assertEqual(cleanup.clean_setup(self.build)['removed'], 0)
        self.assertTrue(setup.exists())
        self.assertTrue(linked_setup.is_symlink())
        self.assertEqual(outside_setup.read_bytes(), b'external setup')
        self.assertEqual(outside_trace.read_bytes(), b'external trace')

    def test_trace_directory_and_ir_symlinks_are_not_followed(self):
        setup, _ = self.pair('Module')
        outside = self.base / 'outside'
        outside.mkdir()
        external_trace = outside / 'Module.trace'
        external_trace.write_bytes(b'trace')
        os.utime(external_trace, ns=(3_000_000_000, 3_000_000_000))
        (self.build / 'lib').mkdir()
        (self.build / 'lib' / 'lean').symlink_to(outside, target_is_directory=True)
        self.assertEqual(cleanup.clean_setup(self.build)['removed'], 0)
        self.assertTrue(setup.exists())
        other_build = self.base / 'other'
        other_build.mkdir()
        (other_build / 'ir').symlink_to(outside, target_is_directory=True)
        self.assertEqual(cleanup.clean_setup(other_build)['removed'], 0)
        self.assertTrue(external_trace.exists())

    def test_missing_build_directory_and_json_cli(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--build-dir', str(self.build), '--json'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'examined': 0, 'removed': 0, 'kept': 0})
        self.assertFalse(self.build.exists())

    def test_symlinked_build_directory_is_refused(self):
        outside = self.base / 'outside'
        outside.mkdir()
        self.build.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            cleanup.clean_setup(self.build)


if __name__ == '__main__':
    unittest.main()
