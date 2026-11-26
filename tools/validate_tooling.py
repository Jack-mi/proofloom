"""Offline tooling checks; runs no proof source and writes no build outputs."""
import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SHELLS = ('build-original.sh', 'build-linux.sh', 'make-fork.sh',
          'tools/merge-phase.sh', 'tools/run-mutants.sh', 'tools/auto-repair.sh',
          'tools/min-round.sh', 'tools/progress.sh')


def main():
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                   cwd=ROOT, check=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    for path in sorted((ROOT / 'tools').glob('*.py')):
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for name in SHELLS:
        subprocess.run(['bash', '-n', str(ROOT / name)], check=True)
    for name in ('xidx.c', 'xan.c'):
        subprocess.run(['cc', '-fsyntax-only', str(ROOT / 'tools' / name)], check=True)
    print('Offline tooling tests and Python/Shell/C syntax checks passed.')


if __name__ == '__main__':
    main()
