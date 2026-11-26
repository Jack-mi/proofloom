import copy
import errno
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import proofloom as rh


def git(source, *args):
    return subprocess.check_output(['git', '-C', str(source), *args], stderr=subprocess.PIPE).decode().strip()


def fixture(base):
    source = base / "mirror space ' $(touch nope)"
    archive = base / 'archive'
    source.mkdir()
    archive.mkdir()
    texts = {'Main': 'import Kept\ndef main := live\n',
             'Statements': 'def statement := True\n', 'Challenge': 'def challenge := True\n',
             'Kept': 'import Dropped\n-- α😀\ndef gone := 0\ndef live := 1\ndef twin := 2\n',
             'Dropped': 'import Frontier\nimport Mathlib.Data.Nat.Basic\n',
             'Frontier': 'def frontier := 1\n', 'Reverted': 'def untouched := 7\n'}
    for name, text in texts.items():
        path = source / 'src' / (name + '.lean')
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding='utf-8')
    for name in rh.CONFIGS:
        (source / name).write_bytes((name + '\n').encode())
    git(source, 'init', '-q')
    git(source, 'add', '.')
    git(source, 'add', '-f', *rh.CONFIGS)
    git(source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
    kept = set(texts) - {'Dropped'}
    text = texts['Kept']
    cuts = {name: [] for name in texts}
    cuts['Kept'] = [[text.index('def gone'), text.index('def live')]]
    cuts['Reverted'] = [[-9, 99999]]
    swaps = {'Kept': [[text.index('def twin'), len(text), 'def twin := live\n']],
             'Reverted': [[0, 90000, 'bad']]}
    write_archive(archive, cuts, swaps, kept)
    return source, archive, git(source, 'rev-parse', 'HEAD')


def write_archive(archive, cuts, swaps, kept):
    for name, data in [('cuts.json.gz', cuts), ('swaps.json.gz', swaps)]:
        with gzip.open(archive / name, 'wt', encoding='utf-8') as stream:
            json.dump(data, stream)
    with gzip.open(archive / 'kept-files.txt.gz', 'wt') as stream:
        stream.write('\n'.join(sorted(kept)) + '\n')
    (archive / 'files-kept-as-they-were.txt').write_text('Reverted\n')
    (archive / 'kept-because-build-needed.txt').write_text('Some.declaration\n')
    with gzip.open(archive / 'merged-theorems.tsv.gz', 'wt') as stream:
        stream.write('Some.theorem\tOther.theorem\n')


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source, self.archive, self.pin = fixture(self.base)
        self.output = self.base / "result ' $()"

    def replay(self, output=None):
        return rh.replay(self.source, output or self.output, self.archive, self.pin)

    def receipt(self, stage='build', outcome='passed'):
        log = self.base / 'log'
        tool = self.base / 'executable'
        log.write_text('actual fixture log\n')
        tool.write_bytes(b'fixture executable')
        artifacts = {}
        for name in rh.receipt_artifacts(stage):
            path = self.base / name
            path.write_bytes(b'fixture ' + name.encode())
            artifacts[name] = str(path)
        if 'comparison_config' in artifacts:
            artifacts['comparison_config'] = str(self.output / 'candidate/comparator.json')
        value = {'version': 1, 'candidate_id': rh.load_manifest(self.output)['candidate_id'],
                 'stage': stage, 'outcome': outcome, 'exit_code': 0 if outcome == 'passed' else None,
                 'tool': {'name': 'fixture', 'version': '1', 'executable': str(tool)},
                 'command': ['fixture', 'space $()'], 'log': str(log), 'artifacts': artifacts}
        if stage == 'checker':
            value['checker'] = 'nanoda'
        return value

    def record(self, value):
        path = self.base / 'receipt.json'
        path.write_text(json.dumps(value))
        return rh.record(self.output, path)

    def test_deterministic_replay_frontier_unicode_reverts_and_protection(self):
        before = rh.tree_summary(self.source / 'src')
        archive = rh.inventory(self.archive)
        first = self.replay()
        second = self.replay(self.base / 'second')
        self.assertEqual(first['candidate_id'], second['candidate_id'])
        self.assertEqual(before, rh.tree_summary(self.source / 'src'))
        self.assertEqual(archive, rh.inventory(self.archive))
        kept = (self.output / 'candidate/src/Kept.lean').read_text()
        self.assertIn('import Frontier\nimport Mathlib.Data.Nat.Basic', kept)
        self.assertIn('-- α😀\n', kept)
        self.assertNotIn('def gone', kept)
        self.assertIn('def twin := live\n', kept)
        for name in (*rh.PROTECTED, 'Reverted'):
            self.assertEqual((self.source / 'src' / (name + '.lean')).read_bytes(),
                             (self.output / 'candidate/src' / (name + '.lean')).read_bytes())
        for name in rh.CONFIGS:
            self.assertEqual((self.source / name).read_bytes(), (self.output / 'candidate' / name).read_bytes())
        report = rh.report(self.output)
        self.assertFalse(report['proof_accepted'])
        self.assertEqual(report['stages'], rh.default_stages())
        self.assertFalse((self.base / 'nope').exists())

    def test_wrong_pin_and_dirty_checkout(self):
        with self.assertRaisesRegex(rh.Invalid, 'pin'):
            rh.replay(self.source, self.output, self.archive)
        for path in [self.source / 'src/Kept.lean', self.source / 'untracked.toml']:
            with self.subTest(path=path):
                old = path.read_bytes() if path.exists() else None
                path.write_text('changed')
                with self.assertRaisesRegex(rh.Invalid, 'dirty'):
                    self.replay()
                if old is None:
                    path.unlink()
                else:
                    path.write_bytes(old)
        self.assertFalse(self.output.exists())

    def test_ignored_build_cache_allowed_but_ignored_source_refused(self):
        (self.source / '.gitignore').write_text('.lake/\nsrc/Ignored*\n')
        git(self.source, 'add', '-f', '.gitignore')
        git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'ignore')
        self.pin = git(self.source, 'rev-parse', 'HEAD')
        (self.source / '.lake').mkdir()
        (self.source / '.lake/cache').write_text('cache')
        self.replay()
        ignored = self.source / 'src/Ignored space.lean'
        ignored.write_text('untracked source')
        with self.assertRaisesRegex(rh.Invalid, 'ignored untracked'):
            self.replay(self.base / 'new')
        self.assertEqual(rh.report(self.output)['outcome'], 'stale')

    def test_missing_source_kept_and_configuration(self):
        for path in [self.source / 'src/Dropped.lean', self.source / 'src/Kept.lean',
                     *[self.source / n for n in rh.CONFIGS]]:
            with self.subTest(path=path):
                data = path.read_bytes()
                path.unlink()
                git(self.source, 'add', '-u')
                git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'missing')
                pin = git(self.source, 'rev-parse', 'HEAD')
                with self.assertRaises((OSError, rh.Invalid)):
                    rh.replay(self.source, self.output, self.archive, pin)
                path.write_bytes(data)
        self.assertFalse(self.output.exists())

    def test_corrupt_archive_shapes_and_relationships(self):
        originals = {n: (self.archive / n).read_bytes() for n in rh.INPUTS}
        cases = [('cuts.json.gz', '[]'), ('cuts.json.gz', '{"Main":[],"Main":[]}'),
                 ('swaps.json.gz', '{"Dropped":[]}'), ('swaps.json.gz', '{"Kept":[[1,2,3]]}'),
                 ('kept-files.txt.gz', 'Main\nMain\n'), ('kept-files.txt.gz', '../escape\n'),
                 ('kept-files.txt.gz', 'Main\nStatements\n'),
                 ('files-kept-as-they-were.txt', 'Dropped\n'),
                 ('files-kept-as-they-were.txt', 'Reverted\nReverted\n'),
                 ('merged-theorems.tsv.gz', 'single-column\n')]
        for name, text in cases:
            with self.subTest(name=name, text=text):
                if name.endswith('.gz'):
                    with gzip.open(self.archive / name, 'wt') as stream:
                        stream.write(text)
                else:
                    (self.archive / name).write_text(text)
                with self.assertRaises((ValueError, OSError)):
                    self.replay()
                (self.archive / name).write_bytes(originals[name])
        self.assertFalse(self.output.exists())

    def test_bad_bounds_overlaps_protected(self):
        original = (self.archive / 'cuts.json.gz').read_bytes()
        for name, spans in [('Kept', [[-1, 3]]), ('Kept', [[0, 9999]]), ('Kept', [[0, 0]]),
                            ('Kept', [[True, 3]]), ('Kept', [[0, 4], [2, 8]]),
                            ('Kept', [[55, 70]]), ('Main', [[0, 3]])]:
            with self.subTest(name=name, spans=spans):
                cuts = rh.read_json(self.archive / 'cuts.json.gz', True)
                cuts[name] = spans
                with gzip.open(self.archive / 'cuts.json.gz', 'wt') as stream:
                    json.dump(cuts, stream)
                with self.assertRaises(rh.Invalid):
                    self.replay()
                (self.archive / 'cuts.json.gz').write_bytes(original)
        self.assertFalse(self.output.exists())

    def test_destinations_and_symlink_escape_leave_user_data(self):
        self.output.mkdir()
        sentinel = self.output / 'sentinel'
        sentinel.write_text('keep')
        destinations = [self.output, self.source / 'result', self.source.parent,
                        ROOT / 'result', ROOT.parent]
        link = self.base / 'source-link'
        link.symlink_to(self.source, target_is_directory=True)
        destinations.append(link / 'result')
        dangling = self.base / 'dangling'
        dangling.symlink_to(self.base / 'absent')
        destinations.append(dangling)
        for dest in destinations:
            with self.subTest(dest=dest), self.assertRaises(rh.Invalid):
                self.replay(dest)
        self.assertEqual(sentinel.read_text(), 'keep')
        self.assertTrue(dangling.is_symlink())
        external = self.base / 'outside.lean'
        external.write_text('outside')
        path = self.source / 'src/Kept.lean'
        path.unlink()
        path.symlink_to(external)
        git(self.source, 'add', '.')
        git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'symlink')
        with self.assertRaisesRegex(rh.Invalid, 'escapes'):
            rh.replay(self.source, self.base / 'new', self.archive, git(self.source, 'rev-parse', 'HEAD'))

    def test_prune_failure_removes_only_owned_temporary_directory(self):
        real_run = subprocess.run
        def run(argv, **kwargs):
            if len(argv) > 1 and argv[1] == str(ROOT / 'tools/prune.py'):
                return subprocess.CompletedProcess(argv, 17, '', 'failure')
            return real_run(argv, **kwargs)
        with patch.object(rh.subprocess, 'run', side_effect=run):
            with self.assertRaises(rh.Invalid):
                self.replay()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.base.glob('.proofloom-replay-*')))
        self.assertTrue(self.source.exists())

    def test_receipts_preserve_attempts_and_never_accept_proof(self):
        self.replay()
        for outcome in ('failed', 'interrupted', 'unsupported', 'passed'):
            value = self.receipt(outcome=outcome)
            self.record(value)
            report = rh.report(self.output)
            self.assertEqual(report['stages']['build'], outcome)
            self.assertFalse(report['proof_accepted'])
        self.assertEqual(len(list((self.output / 'evidence').glob('*.json'))), 4)
        for stage in ('export', 'comparator', 'checker'):
            self.record(self.receipt(stage))
        report = rh.report(self.output)
        self.assertEqual(report['stages']['comparator'], 'passed')
        self.assertEqual(report['stages']['checkers']['nanoda'], 'passed')
        self.assertEqual(report['stages']['checkers']['Tenet'], 'not_run')

    def test_receipt_rejections(self):
        self.replay()
        valid = self.receipt('export')
        cases = [('version', 2), ('candidate_id', '0' * 64), ('exit_code', None), ('exit_code', 7),
                 ('exit_code', True), ('outcome', 'not_run'), ('command', 'echo passed'),
                 ('artifacts', {}), ('artifacts', {'unknown': 'x'}), ('log', '/missing'),
                 ('tool', {'name': 'x', 'version': '1', 'executable': '/missing'}),
                 ('peak_rss_bytes', -1), ('elapsed_seconds', float('nan'))]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                bad = copy.deepcopy(valid)
                bad[name] = value
                with self.assertRaises((ValueError, OSError)):
                    self.record(bad)
        bad = copy.deepcopy(valid)
        bad['artifacts']['solution_export'] = '/missing'
        with self.assertRaises(OSError):
            self.record(bad)
        self.assertEqual(list((self.output / 'evidence').glob('*')), [])

    def test_artifact_changed_during_recording_is_not_published(self):
        self.replay()
        receipt = self.receipt('export')
        exported = Path(receipt['artifacts']['solution_export'])
        original_file_record = rh.file_record
        changed = False

        def replace_after_binding(path):
            nonlocal changed
            result = original_file_record(path)
            if Path(path).resolve() == exported.resolve() and not changed:
                changed = True
                exported.write_bytes(b'replaced after the original export was hashed')
            return result

        with patch.object(rh, 'file_record', side_effect=replace_after_binding):
            with self.assertRaisesRegex(rh.Invalid, 'evidence changed while recording'):
                self.record(receipt)
        self.assertEqual(list((self.output / 'evidence').iterdir()), [])

    def test_interrupted_record_write_does_not_block_report_or_retry(self):
        self.replay()
        self.record(self.receipt(outcome='failed'))
        evidence = self.output / 'evidence'
        existing = {p.name: p.read_bytes() for p in evidence.iterdir()}
        receipt = self.receipt('export')
        original_fdopen = os.fdopen

        class InterruptedWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def write(self, payload):
                self.stream.write(payload[:23])
                self.stream.flush()
                raise OSError(errno.ENOSPC, 'injected interrupted record write')

        with patch.object(rh.os, 'fdopen', side_effect=lambda *a, **kw: InterruptedWriter(original_fdopen(*a, **kw))):
            with self.assertRaises(OSError) as error:
                self.record(receipt)
        self.assertEqual(error.exception.errno, errno.ENOSPC)
        self.assertEqual({p.name: p.read_bytes() for p in evidence.iterdir()}, existing)
        self.assertEqual(rh.report(self.output)['stages']['build'], 'failed')
        self.record(receipt)
        self.assertEqual(rh.report(self.output)['stages']['export'], 'passed')
        self.assertEqual(len(list(evidence.glob('*.json'))), 2)
        self.assertFalse(list(evidence.glob('*.tmp')))

    def test_atomic_json_publish_does_not_replace_existing_data(self):
        destination = self.base / 'immutable.json'
        destination.write_bytes(b'original record bytes')
        with self.assertRaises(FileExistsError):
            rh.write_json(destination, {'replacement': True})
        self.assertEqual(destination.read_bytes(), b'original record bytes')
        self.assertFalse(list(self.base.glob('.immutable.json-*.tmp')))

    def test_export_and_comparison_config_mismatch(self):
        self.replay()
        self.record(self.receipt('export'))
        different = self.base / 'different.export'
        different.write_text('different')
        for stage in ('export', 'comparator', 'checker'):
            with self.subTest(stage=stage):
                receipt = self.receipt(stage)
                receipt['artifacts']['solution_export'] = str(different)
                with self.assertRaisesRegex(rh.Invalid, 'export mismatch'):
                    self.record(receipt)
        receipt = self.receipt('comparator')
        receipt['artifacts']['comparison_config'] = str(self.base / 'log')
        with self.assertRaisesRegex(rh.Invalid, 'configuration mismatch'):
            self.record(receipt)

    def test_partial_exports_do_not_lock_successful_retries(self):
        self.replay()
        partial = self.base / 'partial.export'
        partial.write_bytes(b'partial export')
        attempts = []
        for stage in ('export', 'comparator', 'checker'):
            for outcome in ('failed', 'interrupted', 'unsupported'):
                with self.subTest(stage=stage, outcome=outcome):
                    receipt = self.receipt(stage, outcome)
                    receipt['exit_code'] = 7 if outcome == 'failed' else None
                    receipt['artifacts']['solution_export'] = str(partial)
                    attempts.append(self.record(receipt)['record_id'])
        historical = {p.name: p.read_bytes() for p in (self.output / 'evidence').glob('*.json')}
        for stage in ('export', 'comparator', 'checker'):
            self.record(self.receipt(stage))
        report = rh.report(self.output)
        self.assertEqual(report['outcome'], 'replayed_unverified')
        self.assertEqual(report['stages']['export'], 'passed')
        self.assertEqual(report['stages']['comparator'], 'passed')
        self.assertEqual(report['stages']['checkers']['nanoda'], 'passed')
        self.assertEqual(len(report['evidence']), 12)
        self.assertTrue(all(r['current'] for r in report['evidence']))
        self.assertEqual(partial.read_bytes(), b'partial export')
        for name, data in historical.items():
            self.assertEqual((self.output / 'evidence' / name).read_bytes(), data)
        self.assertEqual({r['record_id'] for r in report['evidence'] if r['receipt']['outcome'] != 'passed'},
                         set(attempts))

    def test_stale_successful_export_does_not_lock_current_evidence(self):
        self.replay()
        receipt = self.receipt('export')
        self.record(receipt)
        Path(receipt['artifacts']['solution_export']).write_bytes(b'changed')
        full = self.base / 'full.export'
        full.write_bytes(b'new full export')
        receipt['artifacts']['solution_export'] = str(full)
        self.record(receipt)
        report = rh.report(self.output)
        self.assertEqual(report['outcome'], 'stale')
        self.assertEqual(report['stages']['export'], 'passed')
        self.assertEqual([r['current'] for r in sorted(report['evidence'], key=lambda r: r['recorded_at'])],
                         [False, True])

    def test_explanatory_declarations_are_not_module_paths(self):
        declarations = ['Reformulated.AutPerF.BdRPlus.«section»', 'Namespace.«a.b»']
        (self.archive / 'kept-because-build-needed.txt').write_text('\n'.join(declarations) + '\n')
        self.assertEqual(rh.inventory(self.archive)['counts']['build_needed_entries'], 2)
        self.replay()
        with self.assertRaises(rh.Invalid):
            rh.module_name(declarations[1])

    def test_mutations_make_report_stale_and_block_recording(self):
        self.replay()
        receipt = self.receipt()
        self.record(receipt)
        paths = [self.output / 'candidate/src/Kept.lean',
                 *[self.output / 'candidate' / n for n in rh.CONFIGS],
                 self.source / 'src/Kept.lean', self.archive / 'kept-because-build-needed.txt']
        for path in paths:
            with self.subTest(path=path):
                old = path.read_bytes()
                path.write_bytes(old + b'\n')
                self.assertEqual(rh.report(self.output)['outcome'], 'stale')
                with self.assertRaisesRegex(rh.Invalid, 'stale'):
                    self.record(receipt)
                path.write_bytes(old)
        original_tools = rh.tool_identity()
        for name in original_tools:
            changed = {**original_tools, name: '0' * 64}
            with patch.object(rh, 'tool_identity', return_value=changed):
                self.assertEqual(rh.report(self.output)['outcome'], 'stale')
                with self.assertRaises(rh.Invalid):
                    self.record(receipt)
        for name in ('log', 'executable'):
            path = self.base / name
            old = path.read_bytes()
            path.write_bytes(b'changed')
            self.assertEqual(rh.report(self.output)['outcome'], 'stale')
            path.write_bytes(old)
        receipt = self.receipt('export')
        self.record(receipt)
        Path(receipt['artifacts']['solution_export']).write_bytes(b'changed')
        self.assertEqual(rh.report(self.output)['outcome'], 'stale')

    def test_manifest_corruption_is_error_not_stale(self):
        self.replay()
        path = self.output / 'manifest.json'
        manifest = rh.read_json(path)
        for key, value in [('stages', {'build': 'passed'}), ('version', 99), ('proof_accepted', True)]:
            with self.subTest(key=key):
                bad = {**manifest, key: value}
                path.write_text(json.dumps(bad))
                with self.assertRaises(rh.Invalid):
                    rh.report(self.output)
        path.write_text('{')
        with self.assertRaises(ValueError):
            rh.report(self.output)

    def test_artifact_hash_reads_bounded_chunks(self):
        class Bounded(io.BytesIO):
            def read(self, size=-1):
                self.assert_size(size)
                return super().read(size)
        data = Bounded(b'x' * (rh.CHUNK_SIZE * 3 + 19))
        data.assert_size = lambda size: self.assertTrue(0 < size <= rh.CHUNK_SIZE)
        with patch('builtins.open', return_value=data):
            self.assertEqual(rh.digest('large.export'), 'fb4ab257bfa81f1324e3a8cb5febf70104dbb625717085cfb70a128854920971')

    def test_cli_error_json_and_stale_exit_zero(self):
        self.replay()
        (self.output / 'candidate/src/Kept.lean').write_text('changed')
        result = subprocess.run([sys.executable, str(ROOT / 'tools/proofloom.py'), 'report',
                                 '--experiment', str(self.output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['outcome'], 'stale')
        result = subprocess.run([sys.executable, str(ROOT / 'tools/proofloom.py'), 'inventory',
                                 '--archive-dir', str(self.base / 'missing')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)


class RealInventoryTests(unittest.TestCase):
    def test_default_archive_from_another_working_directory(self):
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/proofloom.py'), 'inventory'],
                                    cwd=cwd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        counts = json.loads(result.stdout)['counts']
        self.assertEqual({name: counts[name] for name in (
            'kept_modules', 'cut_map_modules', 'raw_cuts', 'swap_map_modules', 'raw_swaps',
            'merged_theorem_rows', 'reverted_modules', 'masked_cuts', 'masked_cut_modules',
            'masked_swaps', 'masked_swap_modules')}, {
                'kept_modules': 172315, 'cut_map_modules': 175188, 'raw_cuts': 90693,
                'swap_map_modules': 41668, 'raw_swaps': 43069, 'merged_theorem_rows': 43266,
                'reverted_modules': 900, 'masked_cuts': 194, 'masked_cut_modules': 15,
                'masked_swaps': 7, 'masked_swap_modules': 7})


if __name__ == '__main__':
    unittest.main()
