import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PIN = '8b83d5e7b319ebc7b3be6e59ce3411ba2b09d116'


class BuildScriptTests(unittest.TestCase):
    def test_real_entry_paths_propagate_build_and_sandbox_results(self):
        for script in ('build-original.sh', 'build-linux.sh'):
            for status, refusal in ((37, False), (0, False), (0, True)):
                with self.subTest(script=script, status=status, refusal=refusal), tempfile.TemporaryDirectory() as tmp:
                    base = Path(tmp)
                    work = base / "work ' $()"
                    (work / 'original/.git').mkdir(parents=True)
                    (work / 'reduced/src').mkdir(parents=True)
                    bin_dir = base / 'bin'
                    bin_dir.mkdir()
                    calls = base / 'calls'
                    stubs = {
                        'git': 'case "$*" in *rev-parse*) echo ' + PIN + ';; *mirror*) exit 29;; esac\n',
                        'lake': 'echo "$*" >> "$STUB_CALLS"\ncase "$*" in build*) echo fixture; exit "$BUILD_STATUS";; esac\n',
                        'bwrap': 'case "$*" in *true) exit "$PROBE_STATUS";; esac\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n',
                        'nproc': 'echo 2\n', 'free': 'echo "Mem: 2"\n',
                        'elan': 'exit 0\n', 'zstd': 'exit 0\n',
                        'curl': 'exit 91\n', 'sudo': 'exit 92\n',
                        'find': 'exit 1\n',
                    }
                    for name, body in stubs.items():
                        path = bin_dir / name
                        path.write_text('#!/bin/sh\n' + body)
                        path.chmod(0o755)
                    env = {**os.environ, 'HOME': str(base / 'home'),
                           'PATH': str(bin_dir) + ':' + os.environ['PATH'],
                           'BUILD_STATUS': str(status), 'PROBE_STATUS': '1' if refusal else '0',
                           'STUB_CALLS': str(calls), 'ALLOW_NO_SANDBOX': '0'}
                    result = subprocess.run(['bash', str(ROOT / script), str(work)], env=env,
                                            capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 1 if refusal else status,
                                     result.stdout + result.stderr)
                    invocations = calls.read_text().splitlines()
                    self.assertIn('--version', invocations)
                    self.assertIn('exe cache get', invocations)
                    self.assertEqual('build Main' in invocations, not refusal)


class CheckForkTests(unittest.TestCase):
    def test_local_package_imports_and_unreached_challenge(self):
        cases = [('import Kept\nimport Mathlib.Data.Nat.Basic\n', 0),
                 ('import Missing.Local\n', 1), ('import MathlibTypo.Local\n', 1)]
        for text, status in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                fork, original = base / 'fork', base / 'original'
                fork.mkdir()
                original.mkdir()
                for name, content in {'Main': text, 'Kept': 'def kept := 1\n',
                                      'Challenge': 'import Mathlib\n'}.items():
                    (fork / (name + '.lean')).write_text(content)
                    (original / (name + '.lean')).write_text(content)
                result = subprocess.run(['python3', str(ROOT / 'tools/check_fork.py'), str(fork), str(original)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, status, result.stderr)


if __name__ == '__main__':
    unittest.main()
