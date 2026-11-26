"""Remove Lake setup files only after their corresponding module trace is newer."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys


def clean_setup(build_dir):
    root = Path(build_dir)
    if root.is_symlink():
        raise ValueError('build directory must not be a symlink')
    root = root.resolve()
    result = {'examined': 0, 'removed': 0, 'kept': 0}
    ir = root / 'ir'
    if not ir.is_dir() or ir.is_symlink():
        return result
    for directory, dirs, names, directory_fd in os.fwalk(ir, follow_symlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in names:
            if not name.endswith('.setup.json'):
                continue
            setup = Path(directory) / name
            result['examined'] += 1
            try:
                setup_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                relative = setup.relative_to(ir)
                trace = root / 'lib' / 'lean' / relative.with_name(name[:-11] + '.trace')
                paths = [trace, *trace.parents]
                if (not stat.S_ISREG(setup_stat.st_mode) or
                        any(path.is_symlink() for path in paths if path != root and root in path.parents)):
                    result['kept'] += 1
                    continue
                trace_stat = trace.lstat()
                if (not stat.S_ISREG(trace_stat.st_mode) or
                        trace_stat.st_mtime_ns <= setup_stat.st_mtime_ns or
                        os.stat(name, dir_fd=directory_fd, follow_symlinks=False) != setup_stat):
                    result['kept'] += 1
                    continue
                os.unlink(name, dir_fd=directory_fd)
                result['removed'] += 1
            except FileNotFoundError:
                result['kept'] += 1
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = clean_setup(args.build_dir)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result) if args.json else
          'Lake setup cleanup: {removed} removed, {kept} retained.'.format(**result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
