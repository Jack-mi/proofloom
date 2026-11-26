"""Install a pinned Linux Lean validation runtime into a new directory.

Requires Python 3.9+, git, tar, zstd, make, a C compiler and bubblewrap. This
downloads official releases and builds the pinned exporter; it does not build
the Riemann Hypothesis submission or change an elan/global toolchain setting.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import Request, urlopen

import proofloom

VERSIONS = ('4.33.1', '4.36.0')
EXPORTER_COMMIT = '15f6055e299ad5b89345e533cc2192f4cc00f659'
EXPORTER_REPOSITORY = 'https://github.com/leanprover/lean4export.git'
# Official release asset digests, from the leanprover/lean4 GitHub release API.
RELEASE_SHA256 = {
    ('4.33.1', 'aarch64'): 'f7353a8b2a8741c84558523e450556f9a1c45e3cafcf54399ce68c6a24c55f07',
    ('4.33.1', 'x86_64'): '890afd185370f85666025b883914ab4f4b339136f8c96167b69cfb62aecaf235',
    ('4.36.0', 'aarch64'): '35471998152191ef809418ce5bf61f8fc5f209e5e3be05b48bdc4949fdb3e726',
    ('4.36.0', 'x86_64'): '2b7ab1ed8b92d2e6abfaf41344823b510064dbc9c0260cac73eb496ab2dea6eb',
}
CHUNK = 1024 * 1024
REPOSITORY = Path(__file__).resolve().parents[1]


class Invalid(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def digest(path):
    result = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            result.update(block)
    return result.hexdigest()


def architecture(system=None, machine=None):
    system = system or platform.system()
    machine = machine or platform.machine()
    require(system == 'Linux', 'setup requires Linux; use the isolated Linux VM described in docs/RUNTIME.md')
    names = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'amd64': 'x86_64', 'x86_64': 'x86_64'}
    require(machine.lower() in names, 'unsupported Linux architecture: ' + machine)
    return names[machine.lower()]


def release_name(version, arch):
    require((version, arch) in RELEASE_SHA256, 'unsupported pinned release or architecture')
    return 'lean-' + version + '-linux' + ('_aarch64' if arch == 'aarch64' else '')


def verify_release(path, version, arch):
    require(digest(path) == RELEASE_SHA256[(version, arch)], 'release checksum mismatch: ' + Path(path).name)


def download_release(directory, version, arch):
    name = release_name(version, arch) + '.tar.zst'
    target = directory / name
    if target.exists() or target.is_symlink():
        require(target.is_file() and not target.is_symlink(), 'cached release is not a regular file: ' + name)
        verify_release(target, version, arch)
        return target
    url = 'https://github.com/leanprover/lean4/releases/download/v' + version + '/' + name
    descriptor, temporary = tempfile.mkstemp(prefix='.' + name + '-', suffix='.part', dir=directory)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            with urlopen(Request(url, headers={'User-Agent': 'Proofloom-runtime'}), timeout=60) as response:
                shutil.copyfileobj(response, stream, CHUNK)
        verify_release(temporary, version, arch)
        # Keep completed cache entries immutable, including concurrent downloads.
        try:
            os.link(temporary, target)
        except FileExistsError:
            verify_release(target, version, arch)
        return target
    finally:
        Path(temporary).unlink()


def normalized(relative):
    parts = []
    for part in PurePosixPath(relative).parts:
        require(part not in ('/', ''), 'absolute archive path')
        if part == '.':
            continue
        if part == '..':
            require(parts, 'archive path escapes root')
            parts.pop()
        else:
            parts.append(part)
    return tuple(parts)


def validate_members(members, expected_root):
    """Validate the complete member/link topology before tar writes any files."""
    names, links = set(), set()
    for member in members:
        path = normalized(member.name)
        require(path and path[0] == expected_root, 'unexpected release root or path: ' + member.name)
        require(path not in names, 'duplicate archive path: ' + member.name)
        names.add(path)
        require(member.isfile() or member.isdir() or member.issym() or member.islnk(),
                'special archive entry: ' + member.name)
        if member.issym() or member.islnk():
            require(not PurePosixPath(member.linkname).is_absolute(), 'absolute archive link: ' + member.name)
            linked = normalized(str(PurePosixPath(member.name).parent / member.linkname)
                                if member.issym() else member.linkname)
            require(linked and linked[0] == expected_root, 'archive link escapes release: ' + member.name)
            links.add(path)
    # This also prevents a later member from writing through an earlier link.
    for path in names:
        require(not any(path[:length] in links for length in range(1, len(path))),
                'archive member traverses a link: ' + '/'.join(path))


def extract_release(archive, destination, expected_root):
    process = subprocess.Popen(['zstd', '-dc', str(archive)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as stream:
            validate_members(list(stream), expected_root)
        stderr = process.stderr.read().decode('utf-8', 'replace')
        require(process.wait() == 0, 'cannot inspect compressed release: ' + stderr.strip())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
    subprocess.run(['tar', '--zstd', '-xf', str(archive), '-C', str(destination),
                    '--no-same-owner', '--no-same-permissions'], check=True)
    return destination / expected_root


def runtime_tools(build, judge, exporter, bwrap):
    required = {'build_lean': build / 'bin/lean', 'build_lake': build / 'bin/lake',
                'judge_lean': judge / 'bin/lean', 'judge_lake': judge / 'bin/lake',
                'leanchecker': judge / 'bin/leanchecker', 'exporter': exporter, 'bwrap': bwrap}
    optional = {'leanchecker_paranoid': judge / 'bin/leanchecker-paranoid',
                'nanoda': judge / 'bin/nanoda_bin'}
    for name, path in required.items():
        require(path.is_file() and os.access(path, os.X_OK), 'missing executable: ' + name)
    required.update({name: path for name, path in optional.items() if path.is_file() and os.access(path, os.X_OK)})
    return required


def payload_digest(prefix):
    """Bind the complete dedicated toolchain, including compiled/native libraries."""
    return proofloom.tree_summary(Path(prefix))['sha256']


def emit_config(root, build, judge, exporter, source, bwrap, arch):
    require(arch in ('aarch64', 'x86_64'), 'unsupported runtime architecture')
    tools = runtime_tools(build, judge, exporter, bwrap)
    return {'version': 1, 'build_prefix': str(build), 'judge_prefix': str(judge),
            'exporter': str(exporter), 'exporter_source': str(source),
            'exporter_commit': EXPORTER_COMMIT, 'bwrap': str(bwrap),
            'architecture': arch,
            'tool_digests': {name: digest(path) for name, path in tools.items()},
            'runtime_payload': {'build': payload_digest(build), 'judge': payload_digest(judge)},
            'release_sha256': {version: RELEASE_SHA256[(version, arch)] for version in VERSIONS}}


def setup(root, downloads=None):
    arch = architecture()
    root_arg = Path(root)
    require(not root_arg.exists() and not root_arg.is_symlink(), 'runtime root already exists')
    root = root_arg.resolve()
    require(root.parent.is_dir(), 'runtime root parent must exist')
    require(REPOSITORY != root and REPOSITORY not in root.parents and root not in REPOSITORY.parents,
            'runtime root must be outside the repository')
    for name in ('git', 'tar', 'zstd', 'make', 'cc', 'bwrap'):
        require(shutil.which(name), 'missing prerequisite: ' + name + '; see docs/RUNTIME.md')
    bwrap = Path(shutil.which('bwrap')).resolve(strict=True)
    if downloads is not None:
        downloads = Path(downloads).resolve(strict=True)
        require(downloads.is_dir(), 'downloads must be an existing directory')
        require(root not in downloads.parents and downloads != root, 'downloads overlap runtime root')
    temporary = Path(tempfile.mkdtemp(prefix='.' + root.name + '-', dir=root.parent))
    published = False
    complete = False
    try:
        cache = downloads or temporary / 'downloads'
        cache.mkdir(exist_ok=True)
        prefixes = []
        for version in VERSIONS:
            release = download_release(cache, version, arch)
            prefix = extract_release(release, temporary, release_name(version, arch))
            prefixes.append(prefix)
        build, judge = prefixes
        source = temporary / 'lean4export'
        subprocess.run(['git', 'clone', '--quiet', EXPORTER_REPOSITORY, str(source)], check=True)
        subprocess.run(['git', '-C', str(source), 'checkout', '--quiet', '--detach', EXPORTER_COMMIT], check=True)
        commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        require(commit == EXPORTER_COMMIT, 'wrong exporter commit')
        # Direct Lake ignores elan selection; compiler/toolchain match the submission.
        build_home = temporary / 'bootstrap-home'
        build_home.mkdir()
        env = {**os.environ, 'PATH': str(build / 'bin') + os.pathsep + os.defpath,
               'HOME': str(build_home)}
        with (temporary / 'exporter-build.log').open('wb') as log:
            subprocess.run([str(build / 'bin/lake'), 'build', 'lean4export'], cwd=source,
                           env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        exporter = source / '.lake/build/bin/lean4export'
        runtime_tools(build, judge, exporter, bwrap)
        for prefix, version in zip(prefixes, VERSIONS):
            result = subprocess.check_output([str(prefix / 'bin/lean'), '--version'], text=True)
            require('version ' + version in result, 'unexpected installed Lean version')
        require(not root.exists() and not root.is_symlink(), 'runtime root appeared during setup')
        os.rename(temporary, root)
        published = True
        build, judge = [root / prefix.name for prefix in prefixes]
        source = root / 'lean4export'
        exporter = source / '.lake/build/bin/lean4export'
        # Loading Init and exporting a real constant detects stale search paths/RPATHs.
        env.update(PATH=str(build / 'bin') + os.pathsep + os.defpath,
                   HOME=str(root / 'bootstrap-home'))
        smoke = root / 'exporter-smoke.ndjson'
        with smoke.open('wb') as output, (root / 'exporter-smoke.log').open('wb') as log:
            subprocess.run([str(exporter), 'Init', '--', 'Nat.add'], cwd=root, env=env,
                           stdout=output, stderr=log, check=True)
        require(smoke.stat().st_size > 0, 'exporter produced an empty export after runtime relocation')
        config = emit_config(root, build, judge, exporter, source, bwrap, arch)
        with (root / 'runtime.json').open('x', encoding='utf-8') as stream:
            json.dump(config, stream, sort_keys=True, indent=2)
            stream.write('\n')
        complete = True
        return config
    finally:
        if not published and temporary.exists():
            shutil.rmtree(temporary)
        elif published and not complete:
            # This directory was created by this attempt, never supplied by the user.
            shutil.rmtree(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, help='new runtime directory outside this repository')
    parser.add_argument('--downloads', help='existing archive cache; checksums are always verified')
    args = parser.parse_args(argv)
    try:
        value = setup(args.root, args.downloads)
        print(json.dumps(value, sort_keys=True))
        return 0
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        print('setup: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
