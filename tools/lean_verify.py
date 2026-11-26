"""Bounded Linux execution of pinned Lean verification; smoke results are not RH results."""
import argparse
import datetime
import json
import math
import os
from pathlib import Path
import platform
import resource
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import proofloom
import setup_lean

EXPORTER_COMMIT = '15f6055e299ad5b89345e533cc2192f4cc00f659'
FULL_MEMORY_MIB = 256 * 1024
CORE_TARGETS = ('Quot Quot.mk Quot.lift Quot.ind propext Quot.sound Classical.choice '
                'Nat.add Nat.sub Nat.mul Nat.pow Nat.gcd Nat.div Nat.mod Nat.beq Nat.ble '
                'Nat.land Nat.lor Nat.xor Nat.shiftLeft Nat.shiftRight String.ofList '
                'Char.ofNat List eagerReduce Nat String String.mk Char optParam autoParam '
                'semiOutParam outParam').split()
SYSTEM_PATHS = ('/usr', '/bin', '/lib', '/lib64', '/etc/ld.so.cache', '/etc/localtime',
                '/etc/alternatives/which')
THEOREMS = ('riemannHypothesis', 'riemannZeta_eq_zero_re', 'LFunction_ne_zero_of_half_lt_re',
            'LFunction_eq_zero_re', 'LFunction_eq_zero_re_of_isPrimitive')
AXIOMS = ('propext', 'Quot.sound', 'Classical.choice')


def load_runtime(path):
    value = proofloom.read_json(Path(path))
    proofloom.keys(value, ('version', 'build_prefix', 'judge_prefix', 'exporter',
                          'exporter_source', 'exporter_commit', 'bwrap', 'tool_digests',
                          'release_sha256', 'architecture', 'runtime_payload'))
    proofloom.require(value['version'] == 1, 'unsupported runtime version')
    proofloom.require(value['exporter_commit'] == EXPORTER_COMMIT, 'wrong exporter pin')
    proofloom.keys(value['release_sha256'], ('4.33.1', '4.36.0'))
    proofloom.require(value['architecture'] in ('aarch64', 'x86_64'), 'unsupported runtime architecture')
    machine = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'x86_64': 'x86_64', 'amd64': 'x86_64'}
    proofloom.require(machine.get(platform.machine().lower()) == value['architecture'], 'runtime architecture differs')
    for version, digest in value['release_sha256'].items():
        proofloom.require(digest == setup_lean.RELEASE_SHA256[(version, value['architecture'])],
                          'release archive digest differs from the official pin')
    for name in ('build_prefix', 'judge_prefix', 'exporter', 'exporter_source', 'bwrap'):
        proofloom.require(isinstance(value[name], str) and Path(value[name]).is_absolute(),
                          'runtime paths must be absolute: ' + name)
    build, judge = Path(value['build_prefix']), Path(value['judge_prefix'])
    proofloom.keys(value['runtime_payload'], ('build', 'judge'))
    for name, prefix in (('build', build), ('judge', judge)):
        proofloom.sha(value['runtime_payload'][name])
        proofloom.require(setup_lean.payload_digest(prefix) == value['runtime_payload'][name],
                          'runtime libraries or compiled core changed: ' + name)
    tools = {'build_lean': build / 'bin/lean', 'build_lake': build / 'bin/lake',
             'judge_lean': judge / 'bin/lean', 'judge_lake': judge / 'bin/lake',
             'leanchecker': judge / 'bin/leanchecker', 'exporter': Path(value['exporter']),
             'bwrap': Path(value['bwrap'])}
    for name, filename in (('leanchecker_paranoid', 'leanchecker-paranoid'), ('nanoda', 'nanoda_bin')):
        if name in value['tool_digests']:
            tools[name] = judge / 'bin' / filename
    proofloom.keys(value['tool_digests'], ('build_lean', 'build_lake', 'judge_lean', 'judge_lake',
                                         'leanchecker', 'exporter', 'bwrap'),
                   ('leanchecker_paranoid', 'nanoda'))
    for name, binary in tools.items():
        proofloom.sha(value['tool_digests'][name])
        proofloom.require(binary.is_file() and os.access(binary, os.X_OK), 'missing executable: ' + name)
        proofloom.require(proofloom.digest(binary) == value['tool_digests'][name],
                          'runtime executable changed: ' + name)
    proofloom.require(proofloom.git(Path(value['exporter_source']), 'rev-parse', 'HEAD') == EXPORTER_COMMIT,
                      'exporter source revision differs')
    value['_tools'] = tools
    return value


def runtime_unchanged(runtime):
    for name, prefix in (('build', runtime['build_prefix']), ('judge', runtime['judge_prefix'])):
        proofloom.require(setup_lean.payload_digest(prefix) == runtime['runtime_payload'][name],
                          'runtime payload changed during verification: ' + name)
    for name, binary in runtime['_tools'].items():
        proofloom.require(proofloom.digest(binary) == runtime['tool_digests'][name],
                          'runtime executable changed during verification: ' + name)


def runtime_identity(runtime):
    return {key: value for key, value in runtime.items() if key != '_tools'}


def tool_mount(path):
    path = Path(path).resolve(strict=True)
    proofloom.require(path.is_dir() and path not in (Path('/'), Path('/home'), Path('/root'), Path.home()),
                      'tool mount must be a dedicated directory, not a host home/root')
    return str(path)


def available_memory_mib():
    if sys.platform != 'linux':
        return 0
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemTotal:'):
            return int(line.split()[1]) // 1024
    return 0


def budgets(timeout, memory_mib, jobs, full=False):
    proofloom.require(math.isfinite(timeout) and timeout > 0, 'timeout must be positive and finite')
    proofloom.require(memory_mib > 0 and jobs > 0, 'memory and jobs must be positive')
    if full:
        proofloom.require(memory_mib >= FULL_MEMORY_MIB and available_memory_mib() >= FULL_MEMORY_MIB,
                          'full verification needs at least 256 GiB; use smoke on this machine')
    return {'timeout_seconds': timeout, 'memory_mib': memory_mib, 'jobs': jobs}


def sandbox(runtime, workspace, command, judge=False, readonly=False, readonly_packages=False, threads=1):
    workspace = Path(workspace).resolve(strict=True)
    tools = runtime['_tools']
    argv = [str(tools['bwrap']), '--unshare-all', '--die-with-parent', '--clearenv']
    for path in SYSTEM_PATHS:
        if Path(path).exists():
            argv += ['--ro-bind', path, path]
    prefix = runtime['judge_prefix'] if judge else runtime['build_prefix']
    if judge:
        argv += ['--ro-bind', runtime['bwrap'], runtime['bwrap']]
    tool_paths = [tool_mount(prefix)]
    if not judge:
        tool_paths.append(tool_mount(Path(runtime['exporter']).parent))
    for path in sorted(set(tool_paths)):
        argv += ['--ro-bind', path, path]
    argv += ['--dir', '/home', '--dir', '/root', '--dir', '/run', '--dir', '/run/user', '--dir', '/var',
             '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--dir', '/tmp/home',
             '--ro-bind' if readonly else '--bind', str(workspace), str(workspace)]
    packages = workspace / '.lake/packages'
    if readonly_packages and packages.is_dir() and not readonly:
        argv += ['--ro-bind', str(packages), str(packages)]
    argv += ['--setenv', 'HOME', '/tmp/home', '--setenv', 'TMPDIR', '/tmp',
             '--setenv', 'PATH', prefix + '/bin:/usr/bin:/bin',
             '--setenv', 'LEAN_ABORT_ON_PANIC', '1', '--setenv', 'LEAN_NUM_THREADS', str(threads),
             '--setenv', 'COMPARATOR_BWRAP', runtime['bwrap'],
             '--chdir', str(workspace), '--']
    return argv + [str(part) for part in command]


def manager_environment():
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    for name in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def memory_scope(command, budget):
    proofloom.require(sys.platform == 'linux', 'memory scopes need Linux')
    proofloom.require(shutil.which('systemd-run') and shutil.which('systemctl'),
                      'systemd user resource manager is required')
    unit = 'proofloom-' + uuid.uuid4().hex + '.scope'
    return [shutil.which('systemd-run'), '--user', '--scope', '--quiet', '--unit=' + unit,
            '-p', 'MemoryMax=' + str(budget['memory_mib'] * 1024 * 1024), '-p', 'MemorySwapMax=0',
            '-p', 'TasksMax=512', '-p', 'CPUQuota=' + str(budget['jobs'] * 100) + '%', '--',
            *command], unit


def stop_scope(unit):
    if not unit:
        return
    subprocess.run(['systemctl', '--user', 'kill', '--kill-whom=all', '--signal=SIGKILL', unit],
                   env=manager_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=10, check=False)
    subprocess.run(['systemctl', '--user', 'stop', unit], env=manager_environment(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)


def scope_properties(unit):
    if not unit:
        return {}
    result = subprocess.run(['systemctl', '--user', 'show', unit,
                             '--property=MemoryMax,MemorySwapMax,CPUQuotaPerSecUSec,TasksMax,MemoryPeak'],
                            env=manager_environment(), capture_output=True, text=True, timeout=10)
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def run_process(command, log, budget, stdout_path=None, enforce_memory=True):
    """Record real exit state; timeout kills this process group, never a process-name match."""
    started = time.monotonic()
    timeout = False
    interrupted = False
    original_command = command
    unit = None
    enforced = {}
    if enforce_memory:
        command, unit = memory_scope(command, budget)

    def limits():
        seconds = max(1, math.ceil(budget['timeout_seconds']) + 5)
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
        if hasattr(os, 'sched_getaffinity'):
            available = sorted(os.sched_getaffinity(0))
            os.sched_setaffinity(0, available[:budget['jobs']])

    with open(log, 'xb') as stderr:
        output = open(stdout_path, 'xb') if stdout_path else None
        try:
            process = subprocess.Popen(command, stdout=output or stderr, stderr=stderr,
                                       start_new_session=True, preexec_fn=limits,
                                       env={**manager_environment(), 'LEAN_NUM_THREADS': str(budget['jobs'])})
            try:
                if unit:
                    # Wait for the transient scope to exist, then read back the actual memory ceiling.
                    for _ in range(50):
                        enforced = scope_properties(unit)
                        if enforced.get('MemoryMax') == str(budget['memory_mib'] * 1024 * 1024):
                            break
                        if process.poll() is not None:
                            break
                        time.sleep(0.01)
                    if process.poll() is None:
                        proofloom.require(enforced.get('MemoryMax') == str(budget['memory_mib'] * 1024 * 1024)
                                          and enforced.get('MemorySwapMax') == '0',
                                          'resource manager did not apply the requested memory/swap limit')
                code = process.wait(timeout=budget['timeout_seconds'])
            except subprocess.TimeoutExpired:
                timeout = True
                stop_scope(unit)
                os.killpg(process.pid, signal.SIGKILL)
                code = process.wait()
            except KeyboardInterrupt:
                interrupted = True
                stop_scope(unit)
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    code = process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    code = process.wait()
            except BaseException:
                stop_scope(unit)
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
        finally:
            if output:
                output.close()
    status = 'interrupted' if timeout or interrupted or code < 0 else ('passed' if code == 0 else 'failed')
    return {'outcome': status, 'exit_code': code, 'timed_out': timeout,
            'failure_reason': ('timeout' if timeout else 'signal' if code < 0 else
                               'nonzero_exit' if code else ''),
            'elapsed_seconds': round(time.monotonic() - started, 6),
            'command': command, 'payload_command': original_command,
            'memory_enforcement': 'systemd MemoryMax; swap disabled' if unit else 'fixture-only',
            'resource_unit': unit, 'resource_properties': enforced, 'log': proofloom.file_record(log)}


def stage(runtime, directory, name, workspace, command, budget, judge=False, export=False,
          readonly=False, readonly_packages=False):
    log = directory / (name + '.log')
    artifact = directory / (name + '.export') if export else None
    partial = directory / (name + '.partial') if export else None
    result = run_process(sandbox(runtime, workspace, command, judge, readonly, readonly_packages,
                                 threads=budget['jobs']),
                         log, budget, partial)
    if export and result['outcome'] == 'passed':
        proofloom.require(partial.stat().st_size > 0, 'exporter produced an empty artifact')
        partial.rename(artifact)
        result['artifact'] = proofloom.file_record(artifact)
    elif partial and partial.exists():
        result['partial_artifact'] = proofloom.file_record(partial)
    proofloom.write_json(directory / (name + '.json'), result)
    return result


def doctor(runtime_path):
    proofloom.require(sys.platform == 'linux', 'verification runs in Linux; configure the Linux backend first')
    runtime = load_runtime(runtime_path)
    versions = {}
    for key, version in (('build_lean', '4.33.1'), ('build_lake', '4.33.1'),
                         ('judge_lean', '4.36.0'), ('judge_lake', '4.36.0')):
        text = subprocess.check_output([str(runtime['_tools'][key]), '--version'],
                                       text=True, stderr=subprocess.STDOUT, timeout=10).strip()
        proofloom.require(re.search(r'(?<![0-9.])' + re.escape(version) + r'(?![0-9.])', text),
                          'wrong runtime version: ' + key)
        versions[key] = text
    with tempfile.TemporaryDirectory(prefix='proofloom-sandbox-probe-') as temporary:
        parent = Path(temporary)
        work = parent / 'work'
        work.mkdir()
        sentinel = parent / 'host-file'
        sentinel.write_text('must not be visible')
        probe = ('sleep 0.2; test ! -e ' + shlex.quote(str(sentinel)) +
                 ' && test -w . && test "$(wc -l </proc/net/route)" -le 1'
                 ' && which bwrap >/dev/null && which git >/dev/null && which env >/dev/null')
        result = run_process(sandbox(runtime, work, ['/bin/sh', '-c', probe]), parent / 'probe.log',
                             budgets(10, 512, 1))
        proofloom.require(result['outcome'] == 'passed', 'narrow no-network sandbox probe failed')
    return {'outcome': 'ready', 'runtime': runtime_identity(runtime), 'versions': versions,
            'memory_mib': available_memory_mib(), 'full_memory_ready': available_memory_mib() >= FULL_MEMORY_MIB,
            'sandbox': 'empty root; explicit read-only tools; isolated network; one writable workspace',
            'memory_enforcement': 'systemd user cgroup MemoryMax; swap disabled',
            'resource_systemd_required': True}


def new_output(output, inputs=()):
    output = Path(output).resolve()
    proofloom.require(not output.exists() and output.parent.is_dir(), 'output must be new with an existing parent')
    proofloom.require(not proofloom.contained(output, proofloom.ROOT), 'run output must be outside repository')
    for path in inputs:
        path = Path(path).resolve()
        proofloom.require(not proofloom.contained(output, path) and not proofloom.contained(path, output),
                          'run output overlaps inputs')
    output.mkdir()
    return output


def write_fixture(directory, body):
    directory.mkdir()
    (directory / 'src').mkdir()
    (directory / 'lakefile.toml').write_text('name = "proofloom_smoke"\nsrcDir = "src"\n'
                                            '[[lean_lib]]\nname = "Smoke"\nroots = ["Main", "Challenge"]\n')
    (directory / 'lean-toolchain').write_text('leanprover/lean4:v4.33.1\n')
    (directory / 'src/Main.lean').write_text(body)
    (directory / 'src/Challenge.lean').write_text('import Init\ntheorem comm (a b : Nat) : a + b = b + a := by sorry\n')


def smoke(runtime_path, output, timeout=180, memory_mib=4096, jobs=1):
    inspection = doctor(runtime_path)
    runtime = load_runtime(runtime_path)
    budget = budgets(timeout, memory_mib, jobs)
    output = new_output(output)
    report = {'version': 1, 'profile': 'smoke', 'smoke_profile': True, 'proof_accepted': False,
              'runtime': inspection, 'budget': budget, 'stages': {}, 'negative_cases': {}}
    build_lake = runtime['_tools']['build_lake']
    judge_lake = runtime['_tools']['judge_lake']
    targets = CORE_TARGETS + ['comm']
    challenge = output / 'challenge'
    write_fixture(challenge, 'import Init\ntheorem comm (a b : Nat) : a + b = b + a := Nat.add_comm a b\n')
    config = {'challenge_module': 'Challenge', 'solution_module': 'Main', 'theorem_names': ['comm'],
              'permitted_axioms': list(AXIOMS), 'enable_nanoda': False, 'external_kernels': {}}
    if 'leanchecker_paranoid' in runtime['_tools']:
        config['external_kernels']['Lean paranoid'] = [str(runtime['_tools']['leanchecker_paranoid']),
                                                      '--silent', '--from-export']
    proofloom.write_json(output / 'comparator.json', config)

    def execute(name, workspace, command, **kwargs):
        value = stage(runtime, output, name, workspace, command, budget, **kwargs)
        report['stages'][name] = value
        return value

    # Challenge is exported before any solution is built and is never mounted writable into a solution.
    challenge_build = execute('challenge-build', challenge, [build_lake, 'build', 'Challenge'])
    if challenge_build['outcome'] == 'passed':
        execute('challenge-export', challenge, [build_lake, 'env', runtime['exporter'], 'Challenge', '--', *targets],
                export=True, readonly=True)
    challenge_export = report['stages'].get('challenge-export', {})
    if challenge_export.get('outcome') != 'passed':
        report['outcome'] = 'failed'
        proofloom.write_json(output / 'result.json', report)
        return report

    bodies = {'valid': 'import Init\ntheorem comm (a b : Nat) : a + b = b + a := Nat.add_comm a b\n',
              'wrong_statement': 'import Init\ntheorem comm (a b : Nat) : a + b = a + b := rfl\n',
              'sorry': 'import Init\ntheorem comm (a b : Nat) : a + b = b + a := by sorry\n',
              'extra_axiom': 'import Init\naxiom invented : ∀ a b : Nat, a + b = b + a\n'
                             'theorem comm (a b : Nat) : a + b = b + a := invented a b\n'}
    for name, body in bodies.items():
        workspace = output / ('solution-' + name)
        write_fixture(workspace, body)
        built = execute(name + '-build', workspace, [build_lake, 'build', 'Main'])
        if built['outcome'] != 'passed':
            continue
        exported = execute(name + '-export', workspace,
                           [build_lake, 'env', runtime['exporter'], 'Main', '--', *targets],
                           export=True, readonly=True)
        if exported['outcome'] != 'passed':
            continue
        judging = output / ('judge-' + name)
        judging.mkdir()
        shutil.copyfile(output / 'comparator.json', judging / 'comparator.json')
        shutil.copyfile(challenge_export['artifact']['path'], judging / 'challenge.export')
        shutil.copyfile(exported['artifact']['path'], judging / 'solution.export')
        checked = execute(name + '-compare', judging,
                          [judge_lake, 'comparator', '--config', 'comparator.json',
                           '--challenge-from-export', 'challenge.export', '--solution-from-export', 'solution.export'],
                          judge=True, readonly=True)
        if name != 'valid':
            report['negative_cases'][name] = {'rejected': checked['outcome'] == 'failed' and
                                              report['stages'].get('valid-compare', {}).get('outcome') == 'passed',
                                              'outcome': checked['outcome'], 'exit_code': checked['exit_code']}
    valid = report['stages'].get('valid-compare', {}).get('outcome') == 'passed'
    if valid:
        judging = output / 'judge-corrupt_export'
        judging.mkdir()
        shutil.copyfile(output / 'comparator.json', judging / 'comparator.json')
        shutil.copyfile(challenge_export['artifact']['path'], judging / 'challenge.export')
        (judging / 'solution.export').write_bytes(b'{"invalid_export": true}\n')
        corrupt = execute('corrupt_export-compare', judging,
                          [judge_lake, 'comparator', '--config', 'comparator.json',
                           '--challenge-from-export', 'challenge.export', '--solution-from-export', 'solution.export'],
                          judge=True, readonly=True)
        report['negative_cases']['corrupt_export'] = {'rejected': corrupt['outcome'] == 'failed',
                                                      'outcome': corrupt['outcome'], 'exit_code': corrupt['exit_code']}
    report['outcome'] = 'smoke_passed' if valid and len(report['negative_cases']) == 4 and all(
        item['rejected'] for item in report['negative_cases'].values()) else 'failed'
    runtime_unchanged(runtime)
    proofloom.write_json(output / 'result.json', report)
    return report


def package_identity(checkout):
    manifest = proofloom.read_json(checkout / 'lake-manifest.json')
    packages = manifest.get('packages', [])
    proofloom.require(isinstance(packages, list), 'invalid package manifest')
    result = {}
    for item in packages:
        proofloom.require(item.get('type') == 'git' and isinstance(item.get('name'), str),
                          'unsupported package source')
        name, revision = item['name'], item.get('rev')
        proofloom.require(name and '/' not in name and name not in ('.', '..'), 'invalid package name')
        package = checkout / '.lake/packages' / name
        proofloom.require(proofloom.git(package, 'rev-parse', 'HEAD') == revision, 'wrong package pin: ' + name)
        proofloom.require(not proofloom.git(package, 'status', '--porcelain'), 'dirty package: ' + name)
        ignored = proofloom.git(package, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard').split('\0')
        proofloom.require(not any(path.endswith('.lean') and not path.startswith('.lake/') for path in ignored),
                          'ignored untracked package source: ' + name)
        cached = package / '.lake/build'
        proofloom.require(cached.is_dir(), 'package cache must be prepared before validation: ' + name)
        configuration = {str(path.relative_to(package)): proofloom.digest(path)
                         for path in (package / '.lake').glob('lakefile.*') if path.is_file()}
        result[name] = {'commit': revision, 'compiled_cache': proofloom.tree_summary(cached),
                        'compiled_configuration': configuration}
    return result


def build_workspace(source, destination):
    """Copy bound inputs and prepared packages, never an unbound root build cache."""
    destination.mkdir()
    shutil.copytree(source / 'src', destination / 'src')
    for name in proofloom.CONFIGS:
        shutil.copyfile(source / name, destination / name)
    packages = source / '.lake/packages'
    if packages.is_dir():
        (destination / '.lake').mkdir()
        shutil.copytree(packages, destination / '.lake/packages', symlinks=False)


def validate(runtime_path, experiment, challenge, output, timeout=86400, memory_mib=FULL_MEMORY_MIB,
             jobs=1, through='all'):
    budgets(timeout, memory_mib, jobs, full=True)
    inspection = doctor(runtime_path)
    runtime = load_runtime(runtime_path)
    experiment, challenge = Path(experiment).resolve(strict=True), Path(challenge).resolve(strict=True)
    manifest = proofloom.load_manifest(experiment)
    proofloom.require(not proofloom.stale_reasons(experiment, manifest), 'experiment is stale')
    proofloom.require(proofloom.git(challenge, 'rev-parse', 'HEAD') == proofloom.PIN and
                      not proofloom.git(challenge, 'status', '--porcelain'), 'Challenge needs a clean pinned checkout')
    candidate = experiment / 'candidate'
    for name in proofloom.CONFIGS:
        proofloom.require(proofloom.digest(challenge / name) == manifest['identity']['candidate']['configs'][name],
                          'trusted Challenge configuration differs: ' + name)
    trusted_packages, candidate_packages = package_identity(challenge), package_identity(candidate)
    proofloom.require(trusted_packages == candidate_packages, 'candidate package/cache differs from trusted Challenge')
    output = new_output(output, (experiment, challenge))
    budget = budgets(timeout, memory_mib, jobs, full=True)
    report = {'version': 1, 'profile': 'full', 'smoke_profile': False, 'proof_accepted': False,
              'candidate_id': manifest['candidate_id'], 'runtime': inspection, 'budget': budget,
              'packages': trusted_packages, 'stages': {}}
    clean, work = output / 'challenge', output / 'candidate'
    build_workspace(challenge, clean)
    build_workspace(candidate, work)
    proofloom.require(proofloom.candidate_identity(work) == manifest['identity']['candidate'],
                      'working candidate differs before verification')
    targets = (challenge / 'export-targets.txt').read_text().split()
    config = proofloom.read_json(challenge / 'comparator.json')
    proofloom.require(tuple(config.get('theorem_names', [])) == THEOREMS and
                      tuple(config.get('permitted_axioms', [])) == AXIOMS, 'target/axiom profile differs')
    lake = runtime['_tools']['build_lake']

    def execute(name, workspace, command, **kwargs):
        result = stage(runtime, output, name, workspace, command, budget, **kwargs)
        report['stages'][name] = result
        return result

    steps = [('challenge-build', clean, [lake, 'build', 'Challenge'], False),
             ('challenge-export', clean, [lake, 'env', runtime['exporter'], 'Challenge', '--', *targets], True),
             ('build', work, [lake, 'build', 'Main'], False),
             ('export', work, [lake, 'env', runtime['exporter'], 'Main', '--', *targets], True)]
    maximum = {'build': 2, 'export': 3, 'compare': 4, 'all': 4}[through]
    for index, (name, workspace, command, export) in enumerate(steps):
        if index > maximum:
            break
        result = execute(name, workspace, command, export=export, readonly=export, readonly_packages=True)
        if result['outcome'] != 'passed':
            report['outcome'] = result['outcome']
            proofloom.write_json(output / 'result.json', report)
            return report
        proofloom.require(proofloom.candidate_identity(work) == manifest['identity']['candidate'],
                          'working candidate source/config changed during verification')
    proofloom.require(package_identity(clean) == trusted_packages and package_identity(work) == candidate_packages,
                      'compiled package cache changed during validation')
    if maximum == 4:
        judging = output / 'judge'
        judging.mkdir()
        for source, destination in ((challenge / 'comparator.json', 'comparator.json'),
                                    (output / 'challenge-export.export', 'challenge.export'),
                                    (output / 'export.export', 'solution.export')):
            shutil.copyfile(source, judging / destination)
        compared = execute('comparator', judging,
                           [runtime['_tools']['judge_lake'], 'comparator', '--config', 'comparator.json',
                            '--challenge-from-export', 'challenge.export', '--solution-from-export', 'solution.export'],
                           judge=True, readonly=True)
        report['outcome'] = compared['outcome']
    else:
        report['outcome'] = 'partial_validation'
    report['candidate_unchanged'] = proofloom.candidate_identity(candidate) == manifest['identity']['candidate']
    proofloom.require(report['candidate_unchanged'], 'original candidate changed')
    runtime_unchanged(runtime)
    proofloom.write_json(output / 'result.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('doctor')
    check.add_argument('--runtime', required=True)
    for name in ('smoke', 'validate'):
        command = commands.add_parser(name)
        command.add_argument('--runtime', required=True)
        command.add_argument('--output', required=True)
        command.add_argument('--timeout', type=float, default=180 if name == 'smoke' else 86400)
        command.add_argument('--memory-mib', type=int, default=4096 if name == 'smoke' else FULL_MEMORY_MIB)
        command.add_argument('--jobs', type=int, default=1)
        if name == 'validate':
            command.add_argument('--experiment', required=True)
            command.add_argument('--challenge', required=True)
            command.add_argument('--stage', choices=('build', 'export', 'compare', 'all'), default='all')
    args = parser.parse_args(argv)
    try:
        if args.command == 'doctor':
            result = doctor(args.runtime)
        elif args.command == 'smoke':
            result = smoke(args.runtime, args.output, args.timeout, args.memory_mib, args.jobs)
        else:
            result = validate(args.runtime, args.experiment, args.challenge, args.output,
                              args.timeout, args.memory_mib, args.jobs, args.stage)
        print(proofloom.canonical(result).decode())
        return 0 if result['outcome'] in ('ready', 'smoke_passed', 'passed', 'partial_validation') else 1
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('error: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
