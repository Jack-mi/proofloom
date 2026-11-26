"""Text-only archived reconstruction and operator-recorded external evidence."""
import argparse
import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

PIN = '8b83d5e7b319ebc7b3be6e59ce3411ba2b09d116'
ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / 'data/min'
CONFIGS = ('lakefile.toml', 'lake-manifest.json', 'lean-toolchain',
           'comparator.json', 'export-targets.txt', '.gitignore')
INPUTS = ('cuts.json.gz', 'swaps.json.gz', 'kept-files.txt.gz',
          'files-kept-as-they-were.txt', 'kept-because-build-needed.txt',
          'merged-theorems.tsv.gz')
PROTECTED = ('Main', 'Statements', 'Challenge')
CHECKERS = ('leanchecker-paranoid', 'nanoda', 'con-leche', 'con-ron',
            'Tenet', 'tenet-hs', 'tenet-jvm')
CHUNK_SIZE = 1024 * 1024
LABEL = 'archived snapshot; revert masks; prune.py; no package-aware import trim'


class Invalid(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def identity(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(CHUNK_SIZE), b''):
            h.update(block)
    return h.hexdigest()


def file_record(path):
    path = Path(path).resolve(strict=True)
    require(path.is_file(), 'not a regular file: ' + str(path))
    return {'path': str(path), 'sha256': digest(path), 'size': path.stat().st_size}


def read_json(path, compressed=False):
    opener = gzip.open if compressed else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        return json.load(stream, object_pairs_hook=unique_object,
                         parse_constant=lambda x: fail('invalid JSON number: ' + x))


def fail(message):
    raise Invalid(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def module_name(name):
    require(isinstance(name, str) and bool(name), 'invalid module name')
    require(all(re.fullmatch(r"(?:[^\W\d][\w']*|«[^«»/\\\x00.]+»)", part, re.UNICODE)
                for part in name.split('.')), 'invalid module name: ' + name)
    return name


def text_lines(path):
    opener = gzip.open if str(path).endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        return [line.strip() for line in stream if line.strip()]


def module_list(path):
    lines = text_lines(path)
    for name in lines:
        module_name(name)
    require(len(lines) == len(set(lines)), 'duplicate entries: ' + str(path))
    return set(lines)


def load_archive(archive_dir=ARCHIVE):
    directory = Path(archive_dir).resolve(strict=True)
    before = {name: file_record(directory / name) for name in INPUTS}
    cuts = read_json(directory / INPUTS[0], True)
    swaps = read_json(directory / INPUTS[1], True)
    for mapping, width in ((cuts, 2), (swaps, 3)):
        require(isinstance(mapping, dict), 'transform map must be an object')
        for name, spans in mapping.items():
            module_name(name)
            require(isinstance(spans, list), 'spans must be lists: ' + name)
            for span in spans:
                require(isinstance(span, list) and len(span) == width,
                        'invalid span shape: ' + name)
                if width == 3:
                    require(isinstance(span[2], str), 'swap replacement must be text')
    kept = module_list(directory / INPUTS[2])
    reverted = module_list(directory / INPUTS[3])
    needed = text_lines(directory / INPUTS[4])
    merged = text_lines(directory / INPUTS[5])
    require(all(len(row.split('\t')) == 2 and all(row.split('\t')) for row in merged),
            'merged theorem TSV requires two nonempty columns')
    require(set(PROTECTED) <= kept, 'missing required kept roots')
    require(kept <= cuts.keys(), 'kept modules missing from cut map')
    require(swaps.keys() <= cuts.keys() and swaps.keys() <= kept,
            'swap modules must be kept and in cut map')
    require(reverted <= kept, 'reverted modules must be kept')
    counts = {
        'kept_modules': len(kept), 'cut_map_modules': len(cuts),
        'raw_cuts': sum(map(len, cuts.values())), 'swap_map_modules': len(swaps),
        'raw_swaps': sum(map(len, swaps.values())), 'reverted_modules': len(reverted),
        'masked_cuts': sum(len(cuts[n]) for n in reverted),
        'masked_cut_modules': sum(bool(cuts[n]) for n in reverted),
        'masked_swaps': sum(len(swaps.get(n, [])) for n in reverted),
        'masked_swap_modules': sum(bool(swaps.get(n)) for n in reverted),
        'build_needed_entries': len(needed), 'merged_theorem_rows': len(merged)}
    masked_cuts = {n: ([] if n in reverted else spans) for n, spans in cuts.items()}
    masked_swaps = {n: spans for n, spans in swaps.items() if n not in reverted}
    require(all(not masked_cuts[n] and not masked_swaps.get(n) for n in PROTECTED),
            'protected roots have active transformations')
    files = {n: {'sha256': before[n]['sha256'], 'size': before[n]['size'],
                 'role': 'transform' if n in INPUTS[:4] else 'explanatory'} for n in INPUTS}
    require(before == {n: file_record(directory / n) for n in INPUTS},
            'archive changed during inventory')
    summary = {'label': LABEL, 'digest': identity({n: files[n]['sha256'] for n in INPUTS}),
               'files': files, 'counts': counts}
    return summary, masked_cuts, masked_swaps, kept


def inventory(archive_dir=ARCHIVE):
    return load_archive(archive_dir)[0]


def contained(path, root):
    return path == root or root in path.parents


def safe_file(root, relative):
    path = root / relative
    require(contained(path.resolve(strict=True), root), 'source input escapes tree: ' + str(path))
    require(path.is_file(), 'missing source/config file: ' + str(path))
    return path


def tree_summary(root):
    root = root.resolve(strict=True)
    require(root.is_dir(), 'missing source tree: ' + str(root))
    paths = sorted(p.relative_to(root).as_posix() for p in root.rglob('*')
                   if p.is_file() or p.is_symlink())
    h = hashlib.sha256()
    counts = {'files': 0, 'lean_files': 0, 'lines': 0, 'bytes': 0}
    for rel in paths:
        path = safe_file(root, rel)
        h.update(rel.encode('utf-8') + b'\0')
        h.update(bytes.fromhex(digest(path)))
        counts['files'] += 1
        if rel.endswith('.lean'):
            counts['lean_files'] += 1
            with open(path, 'rb') as stream:
                for block in iter(lambda: stream.read(CHUNK_SIZE), b''):
                    counts['bytes'] += len(block)
                    counts['lines'] += block.count(b'\n')
    return {'sha256': h.hexdigest(), **counts}


def git(source, *args):
    return subprocess.check_output(['git', '-C', str(source), *args],
                                   stderr=subprocess.PIPE).decode().strip()


def source_identity(source, modules, expected_commit=PIN):
    require(git(source, 'rev-parse', '--show-toplevel') == str(source),
            'source must be a checkout root')
    commit = git(source, 'rev-parse', 'HEAD')
    require(commit == expected_commit, 'wrong source pin: ' + commit)
    require(not git(source, 'status', '--porcelain', '--untracked-files=all'),
            'source checkout is dirty')
    ignored = git(source, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard',
                  '--', 'src', *CONFIGS).split('\0')
    require(not any(name.endswith('.lean') or name in CONFIGS for name in ignored),
            'source has ignored untracked source/configuration')
    for name in modules:
        safe_file(source, 'src/' + name.replace('.', '/') + '.lean')
    configs = {n: digest(safe_file(source, n)) for n in CONFIGS}
    return {'commit': commit, 'git_tree': git(source, 'rev-parse', 'HEAD^{tree}'),
            'src': tree_summary(source / 'src'), 'configs': configs}


def tool_identity():
    return {n: digest(ROOT / 'tools' / n) for n in ('proofloom.py', 'prune.py')}


def candidate_identity(candidate):
    require(contained((candidate / 'src').resolve(strict=True), candidate.resolve(strict=True)),
            'candidate source escapes tree')
    return {'src': tree_summary(candidate / 'src'),
            'configs': {n: digest(safe_file(candidate, n)) for n in CONFIGS}}


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    payload = canonical(value) + b'\n'
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp',
                                             dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link publishes complete bytes without replacing an immutable record.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def default_stages():
    return {'build': 'not_run', 'export': 'not_run', 'comparator': 'not_run',
            'checkers': {name: 'not_run' for name in CHECKERS}}


def replay(source, output, archive_dir=ARCHIVE, expected_commit=None):
    if expected_commit is None:
        expected_commit = PIN
    source = Path(source).resolve(strict=True)
    require(not Path(output).exists() and not Path(output).is_symlink(), 'output already exists')
    output = Path(output).resolve()
    archive_dir = Path(archive_dir).resolve(strict=True)
    require(not output.exists() and not output.is_symlink(), 'output already exists')
    for tree in (source, ROOT):
        require(not contained(output, tree) and not contained(tree, output),
                'output overlaps source or repository')
    snapshot, cuts, swaps, kept = load_archive(archive_dir)
    original = source_identity(source, cuts.keys(), expected_commit)
    tools = tool_identity()
    for name in cuts.keys() | swaps.keys():
        spans = [(*s, '') for s in cuts.get(name, [])] + [tuple(s) for s in swaps.get(name, [])]
        if not spans:
            continue
        text = safe_file(source, 'src/' + name.replace('.', '/') + '.lean').read_text(encoding='utf-8')
        for start, end, replacement in spans:
            require(type(start) is int and type(end) is int and 0 <= start < end <= len(text),
                    'invalid character bounds: ' + name)
        spans.sort()
        require(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])),
                'overlapping transformations: ' + name)
    require(output.parent.is_dir(), 'output parent must exist')
    temporary = Path(tempfile.mkdtemp(prefix='.proofloom-replay-', dir=output.parent))
    try:
        candidate = temporary / 'candidate'
        candidate.mkdir()
        write_json(temporary / 'cuts.json', cuts)
        write_json(temporary / 'swaps.json', swaps)
        (temporary / 'kept.txt').write_text('\n'.join(sorted(kept)) + '\n', encoding='utf-8')
        result = subprocess.run([sys.executable, str(ROOT / 'tools/prune.py'),
                                 str(source / 'src'), str(temporary / 'kept.txt'), str(candidate),
                                 str(temporary / 'cuts.json'), str(temporary / 'swaps.json')],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        require(result.returncode == 0, 'prune failed: ' + result.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr, end='')
        for name in CONFIGS:
            shutil.copyfile(safe_file(source, name), candidate / name)
        for name in PROTECTED:
            require(digest(candidate / 'src' / (name + '.lean')) == digest(source / 'src' / (name + '.lean')),
                    'protected source changed: ' + name)
        candidate_data = candidate_identity(candidate)
        require(candidate_data['src']['lean_files'] == len(kept), 'incomplete reconstructed candidate')
        components = {'snapshot': snapshot, 'source': original, 'tools': tools,
                      'candidate': candidate_data}
        manifest = {'version': 1, 'candidate_id': identity(components), 'identity': components,
                    'provenance': {'source': str(source), 'archive': str(archive_dir),
                                   'output': str(output), 'created_at': timestamp()},
                    'stages': default_stages(), 'proof_accepted': False}
        require(original == source_identity(source, cuts.keys(), expected_commit),
                'source changed during replay')
        require(snapshot == inventory(archive_dir), 'archive changed during replay')
        require(tools == tool_identity(), 'replay tools changed during replay')
        for name in ('cuts.json', 'swaps.json', 'kept.txt'):
            (temporary / name).unlink()
        (temporary / 'evidence').mkdir()
        write_json(temporary / 'manifest.json', manifest)
        require(not output.exists() and not output.is_symlink(), 'output appeared during replay')
        # rename publishes the validated directory on the same filesystem.
        os.rename(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {'outcome': 'replayed_unverified', 'candidate_id': manifest['candidate_id'],
            'proof_accepted': False, 'candidate': candidate_data}


def keys(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= value.keys()
            and value.keys() <= set(required) | set(optional), 'invalid object fields')


def sha(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'invalid digest')


def validate_tree(value):
    keys(value, ('sha256', 'files', 'lean_files', 'lines', 'bytes'))
    sha(value['sha256'])
    require(all(type(value[n]) is int and value[n] >= 0 for n in ('files', 'lean_files', 'lines', 'bytes')),
            'invalid tree counts')


def validate_configs(value):
    keys(value, CONFIGS)
    for hash_value in value.values():
        sha(hash_value)


def load_manifest(experiment):
    manifest = read_json(experiment / 'manifest.json')
    keys(manifest, ('version', 'candidate_id', 'identity', 'provenance', 'stages', 'proof_accepted'))
    require(type(manifest['version']) is int and manifest['version'] == 1, 'unknown manifest version')
    require(manifest['proof_accepted'] is False and manifest['stages'] == default_stages(),
            'invalid proof-stage defaults')
    keys(manifest['provenance'], ('source', 'archive', 'output', 'created_at'))
    require(all(isinstance(v, str) and v for v in manifest['provenance'].values()), 'invalid provenance')
    components = manifest['identity']
    keys(components, ('snapshot', 'source', 'tools', 'candidate'))
    snapshot = components['snapshot']
    keys(snapshot, ('label', 'digest', 'files', 'counts'))
    require(snapshot['label'] == LABEL, 'unknown snapshot definition')
    sha(snapshot['digest'])
    keys(snapshot['files'], INPUTS)
    for name, value in snapshot['files'].items():
        keys(value, ('sha256', 'size', 'role'))
        sha(value['sha256'])
        require(type(value['size']) is int and value['size'] >= 0 and
                value['role'] == ('transform' if name in INPUTS[:4] else 'explanatory'), 'invalid archive entry')
    require(snapshot['digest'] == identity({n: snapshot['files'][n]['sha256'] for n in INPUTS}),
            'corrupt snapshot digest')
    keys(snapshot['counts'], ('kept_modules', 'cut_map_modules', 'raw_cuts', 'swap_map_modules',
                             'raw_swaps', 'reverted_modules', 'masked_cuts', 'masked_cut_modules',
                             'masked_swaps', 'masked_swap_modules', 'build_needed_entries', 'merged_theorem_rows'))
    require(all(type(v) is int and v >= 0 for v in snapshot['counts'].values()), 'invalid archive counts')
    source = components['source']
    keys(source, ('commit', 'git_tree', 'src', 'configs'))
    require(all(isinstance(source[n], str) and re.fullmatch('[0-9a-f]{40}', source[n])
                for n in ('commit', 'git_tree')), 'invalid git identity')
    validate_tree(source['src'])
    validate_configs(source['configs'])
    keys(components['tools'], ('proofloom.py', 'prune.py'))
    for value in components['tools'].values():
        sha(value)
    keys(components['candidate'], ('src', 'configs'))
    validate_tree(components['candidate']['src'])
    validate_configs(components['candidate']['configs'])
    require(components['candidate']['configs'] == source['configs'], 'config identity mismatch')
    require(manifest['candidate_id'] == identity(components), 'corrupt candidate identity')
    return manifest


def stale_reasons(experiment, manifest):
    reasons = []
    components = manifest['identity']
    checks = {
        'candidate': lambda: candidate_identity(experiment / 'candidate'),
        'snapshot': lambda: inventory(manifest['provenance']['archive']),
        'tools': tool_identity,
        'source': lambda: source_identity(Path(manifest['provenance']['source']).resolve(strict=True),
                                         load_archive(manifest['provenance']['archive'])[1].keys(),
                                         components['source']['commit'])}
    for name, compute in checks.items():
        try:
            if compute() != components[name]:
                reasons.append(name + ' identity changed')
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            reasons.append(name + ': ' + str(error))
    return reasons


def receipt_artifacts(stage):
    return {'build': set(), 'export': {'solution_export'},
            'comparator': {'challenge_export', 'solution_export', 'comparison_config'},
            'checker': {'solution_export'}}[stage]


def validate_receipt(value):
    keys(value, ('version', 'candidate_id', 'stage', 'outcome', 'exit_code', 'tool', 'command', 'log', 'artifacts'),
         ('checker', 'elapsed_seconds', 'peak_rss_bytes'))
    require(type(value['version']) is int and value['version'] == 1, 'unknown receipt version')
    sha(value['candidate_id'])
    require(value['stage'] in ('build', 'export', 'comparator', 'checker'), 'unknown stage')
    if value['stage'] == 'checker':
        require(value.get('checker') in CHECKERS, 'unknown independent checker')
    else:
        require('checker' not in value, 'checker name only allowed for checker stage')
    require(value['outcome'] in ('passed', 'failed', 'interrupted', 'unsupported'), 'unknown outcome')
    require(value['exit_code'] is None or type(value['exit_code']) is int, 'invalid exit code')
    require(value['outcome'] != 'passed' or value['exit_code'] == 0, 'passed requires actual exit code zero')
    keys(value['tool'], ('name', 'version', 'executable'))
    require(all(isinstance(v, str) and v for v in value['tool'].values()), 'invalid tool provenance')
    require(isinstance(value['command'], list) and value['command'] and
            all(isinstance(v, str) and v for v in value['command']), 'command must be a nonempty argv array')
    require(isinstance(value['log'], str) and value['log'], 'missing log path')
    require(isinstance(value['artifacts'], dict) and value['artifacts'].keys() <= receipt_artifacts(value['stage']),
            'unknown artifact names')
    if value['outcome'] == 'passed':
        require(value['artifacts'].keys() == receipt_artifacts(value['stage']), 'passed stage lacks artifacts')
    require(all(isinstance(v, str) and v for v in value['artifacts'].values()), 'invalid artifact path')
    if 'elapsed_seconds' in value:
        require(type(value['elapsed_seconds']) in (int, float) and 0 <= value['elapsed_seconds'] < float('inf'),
                'invalid elapsed time')
    if 'peak_rss_bytes' in value:
        require(type(value['peak_rss_bytes']) is int and value['peak_rss_bytes'] >= 0, 'invalid peak RSS')


def bound_files(receipt):
    return {'tool': file_record(receipt['tool']['executable']), 'log': file_record(receipt['log']),
            'artifacts': {n: file_record(p) for n, p in receipt['artifacts'].items()}}


def successful_exports(records):
    return {r['files']['artifacts']['solution_export']['sha256'] for r in records
            if r['current'] and r['receipt']['outcome'] == 'passed'
            and 'solution_export' in r['files']['artifacts']}


def evidence_records(experiment, manifest):
    require((experiment / 'evidence').is_dir(), 'missing evidence directory')
    records = []
    for path in sorted((experiment / 'evidence').glob('*.json')):
        value = read_json(path)
        keys(value, ('version', 'record_id', 'recorded_at', 'receipt', 'files'))
        require(type(value['version']) is int and value['version'] == 1 and
                value['record_id'] == path.stem and isinstance(value['recorded_at'], str), 'invalid evidence record')
        validate_receipt(value['receipt'])
        require(value['receipt']['candidate_id'] == manifest['candidate_id'], 'evidence candidate mismatch')
        keys(value['files'], ('tool', 'log', 'artifacts'))
        keys(value['files']['artifacts'], value['receipt']['artifacts'].keys())
        for item in [value['files']['tool'], value['files']['log'], *value['files']['artifacts'].values()]:
            keys(item, ('path', 'sha256', 'size'))
            sha(item['sha256'])
            require(isinstance(item['path'], str) and type(item['size']) is int and item['size'] >= 0,
                    'invalid bound file')
        reasons = []
        try:
            if bound_files(value['receipt']) != value['files']:
                reasons.append('external artifacts or executable changed')
        except (OSError, ValueError) as error:
            reasons.append(str(error))
        records.append({**value, 'current': not reasons, 'stale_reasons': reasons})
    require(len(successful_exports(records)) <= 1, 'evidence solution exports disagree')
    return records


def report(experiment):
    experiment = Path(experiment).resolve(strict=True)
    manifest = load_manifest(experiment)
    reasons = stale_reasons(experiment, manifest)
    records = evidence_records(experiment, manifest)
    stages = default_stages()
    for record in sorted(records, key=lambda r: (r['recorded_at'], r['record_id'])):
        receipt = record['receipt']
        status = receipt['outcome'] if record['current'] and not reasons else 'stale'
        if receipt['stage'] == 'checker':
            stages['checkers'][receipt['checker']] = status
        else:
            stages[receipt['stage']] = status
    reasons.extend('evidence ' + r['record_id'] + ': ' + reason for r in records for reason in r['stale_reasons'])
    return {'outcome': 'stale' if reasons else 'replayed_unverified', 'proof_accepted': False,
            'candidate_id': manifest['candidate_id'], 'stages': stages, 'stale_reasons': reasons,
            'evidence_kind': 'operator-recorded external evidence', 'evidence': records}


def record(experiment, receipt_path):
    experiment = Path(experiment).resolve(strict=True)
    manifest = load_manifest(experiment)
    require(not stale_reasons(experiment, manifest), 'experiment is stale; cannot record evidence')
    receipt = read_json(receipt_path)
    validate_receipt(receipt)
    require(receipt['candidate_id'] == manifest['candidate_id'], 'receipt candidate mismatch')
    receipt['tool']['executable'] = str(Path(receipt['tool']['executable']).resolve(strict=True))
    receipt['log'] = str(Path(receipt['log']).resolve(strict=True))
    receipt['artifacts'] = {n: str(Path(p).resolve(strict=True)) for n, p in receipt['artifacts'].items()}
    files = bound_files(receipt)
    if 'comparison_config' in files['artifacts']:
        require(files['artifacts']['comparison_config']['sha256'] ==
                manifest['identity']['candidate']['configs']['comparator.json'], 'comparison configuration mismatch')
    prior = evidence_records(experiment, manifest)
    exports = successful_exports(prior)
    if receipt['outcome'] == 'passed' and 'solution_export' in files['artifacts']:
        exports.add(files['artifacts']['solution_export']['sha256'])
    require(len(exports) <= 1, 'solution export mismatch')
    require(files == bound_files(receipt), 'evidence changed while recording')
    require(not stale_reasons(experiment, manifest), 'experiment changed while recording')
    record_id = uuid.uuid4().hex
    value = {'version': 1, 'record_id': record_id, 'recorded_at': timestamp(),
             'receipt': receipt, 'files': files}
    write_json(experiment / 'evidence' / (record_id + '.json'), value)
    return {'record_id': record_id, 'candidate_id': manifest['candidate_id'], 'proof_accepted': False,
            'evidence_kind': 'operator-recorded external evidence', 'outcome': receipt['outcome']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    inv = commands.add_parser('inventory')
    inv.add_argument('--archive-dir', default=str(ARCHIVE))
    rep = commands.add_parser('replay')
    rep.add_argument('--source', required=True)
    rep.add_argument('--output', required=True)
    rep.add_argument('--archive-dir', default=str(ARCHIVE))
    for name in ('report', 'record'):
        command = commands.add_parser(name)
        command.add_argument('--experiment', required=True)
        if name == 'record':
            command.add_argument('--receipt', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'inventory':
            value = inventory(args.archive_dir)
        elif args.command == 'replay':
            value = replay(args.source, args.output, args.archive_dir)
        elif args.command == 'report':
            value = report(args.experiment)
        else:
            value = record(args.experiment, args.receipt)
        print(canonical(value).decode('utf-8'))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print('error: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
