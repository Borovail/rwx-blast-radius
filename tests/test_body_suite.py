import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tools'))

from build_body_suite import test_identity as manifest_ids, validate, validate_files, validate_pytest
from m2_body import BASE_COMMIT, comparison_site, observed_outcome
from suite_snapshot import snapshot


class SnapshotTests(unittest.TestCase):
    def test_primitive_and_reflected_semantics(self):
        class Number(int):
            pass
        class Text(str):
            pass
        for left, right in [(None, None), (1, True), (1, 1.0), (1, Number(1)),
                            (Text('x'), 'x'), (object(), 1), (object(), None),
                            (float('nan'), float('nan')), (b'x', 'x')]:
            a, b = snapshot(left), snapshot(right)
            observation = [a[1], b[1], a[0] == b[0], left is right]
            self.assertEqual(observed_outcome(observation, 'Eq'), left == right)
            self.assertEqual(observed_outcome(observation, 'NotEq'), left != right)

    def test_scalar_rebound_to_object_is_unsupported(self):
        for primitive, value in [(int, 1), (float, 1), (str, 'x'), (bytes, b'x')]:
            rebound = type('Rebound', (primitive,), dict(__eq__=object.__eq__, __ne__=object.__ne__))
            self.assertEqual(snapshot(rebound(value))[1], ('unknown',))

    def test_cross_primitive_and_mixed_eq_ne_rebindings(self):
        class Cross(int):
            __eq__ = float.__eq__
        class Mixed(int):
            __ne__ = object.__ne__
        class Opaque:
            __eq__ = int.__eq__
            __ne__ = int.__ne__
        for value in (Cross(1), Mixed(1), Opaque()):
            self.assertEqual(snapshot(value)[1], ('unknown',))

    def test_does_not_invoke_user_descriptor_equality_hash_or_metaclass(self):
        def forbidden(*args):
            raise AssertionError('user behavior invoked')
        class Descriptor:
            __eq__ = __hash__ = __get__ = forbidden
        class Meta(type):
            __eq__ = __hash__ = __getattribute__ = forbidden
        class Hostile(metaclass=Meta):
            __eq__ = Descriptor()
        self.assertEqual(snapshot(Hostile())[1], ('unknown',))
        class Identity(metaclass=Meta):
            pass
        self.assertEqual(snapshot(Identity())[1], ('identity',))

    def test_subclass_converters_bypass_user_hooks(self):
        class Number(int):
            def __int__(self):
                raise AssertionError('conversion hook')
        self.assertEqual(snapshot(Number(2))[1], ('scalar_subclass', 'int', '2'))

    def test_metaclass_data_descriptors_cannot_intercept_mro_or_namespace(self):
        class Meta(type):
            @property
            def __mro__(self):
                raise AssertionError('MRO descriptor invoked')
            @property
            def __dict__(self):
                raise AssertionError('namespace descriptor invoked')
        class Identity(metaclass=Meta):
            pass
        class Number(int, metaclass=Meta):
            pass
        self.assertEqual(snapshot(Identity())[1], ('identity',))
        self.assertEqual(snapshot(Number(2))[1], ('scalar_subclass', 'int', '2'))

    def test_large_integer_is_bounded_before_decimal_conversion(self):
        self.assertEqual(snapshot(10 ** 5000)[1], ('unknown',))


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.repo, self.run = self.directory / 'repo', self.directory / 'run'
        self.repo.mkdir()
        self.run.mkdir()
        self.source = 'def f(x):\n return x is None\n'
        (self.repo / 'subject.py').write_text(self.source)
        self.sha = hashlib.sha256(self.source.encode()).hexdigest()
        self.tests = [{'number': 1, 'id': 'tests/test_x.py::test_x'}]
        self.id = self.tests[0]['id']
        self.coverage = {'paths': {'subject.py': {'sha256': self.sha, 'line_count': 2}}}
        node = next(node for node in ast.walk(ast.parse(self.source)) if isinstance(node, ast.Compare))
        self.site = comparison_site(node)
        self.value = {'ast': ast.dump(node), 'operator': 'Is',
                      'observations': [[['scalar', 'int', '1'], ['identity'], False, False]]}
        self.files = {'subject.py': {'sha256': self.sha, 'sites': {self.site: self.value}}}
        self.metadata = dict(version=1, base_commit=BASE_COMMIT, base_fingerprint='stamp',
                             after_fingerprint='stamp', coverage_sha256='digest', tests=self.tests,
                             platform='linux', python_version=[3, 13],
                             repo=str(self.repo) + os.sep, timed_out=False, returncode=0,
                             cleanup_killed=False, pid=101, token='token')
        self.report = dict(exit_code=0, collected=[self.id], collection_errors=[], subtests=[],
                           phases=[[self.id, phase, 'passed'] for phase in ('setup', 'call', 'teardown')])
        self.write('run.json', self.metadata)
        self.write('pytest.json', self.report)
        self.process(101, 1, {})

    def write(self, name, value):
        (self.run / name).write_text(json.dumps(value))

    def process(self, pid, ppid, children, files=None, unknown=None):
        common = dict(version=1, pid=pid, ppid=ppid, token='token', repo=str(self.repo) + os.sep)
        self.write(f'{pid}.start.json', common)
        self.write(f'{pid}.done.json', dict(common, complete=True, trace_installed=True,
                   errors=[], children=children, lines={'subject.py': [1, 2]}, readers=[]))
        self.write(f'{pid}.body.json', dict(common, complete=True, errors=[],
                   unknown=unknown or [], files=self.files if files is None else files))

    def run_validate(self):
        return validate(self.run, self.tests, 'stamp', self.coverage, 'digest', self.repo)

    def test_valid_complete_suite_and_child(self):
        self.process(101, 1, {'102': 'Popen'})
        self.process(102, 101, {})
        files, processes, subtests, extras, _ = self.run_validate()
        self.assertEqual(files, self.files)
        self.assertEqual((processes, subtests, extras), (2, 0, []))

    def test_exact_numbered_ids(self):
        self.assertEqual(manifest_ids([dict(self.tests[0], number=0)]), {0: self.id})
        for rows in ([self.tests[0], self.tests[0]],
                     [self.tests[0], dict(self.tests[0], number=2)],
                     [dict(self.tests[0], number=True)]):
            with self.assertRaises(ValueError):
                manifest_ids(rows)

    def test_missing_duplicate_or_bad_numbered_phase(self):
        for change in ('missing', 'duplicate', 'skipped', 'subfailed'):
            report = copy.deepcopy(self.report)
            if change == 'missing':
                report['phases'].pop()
            elif change == 'duplicate':
                report['phases'].append(report['phases'][0])
            elif change == 'skipped':
                report['phases'][0][2] = 'skipped'
            else:
                report['subtests'].append([self.id, 'call', 'failed'])
            with self.assertRaises(ValueError, msg=change):
                validate_pytest(report, {1: self.id})

    def test_all_subtest_statuses_checked(self):
        for phase, outcome in [('call', 'failed'), ('call', 'skipped'), ('setup', 'passed')]:
            report = dict(self.report, subtests=[[self.id, phase, outcome]])
            with self.assertRaises(ValueError):
                validate_pytest(report, {1: self.id})
        report = dict(self.report, subtests=[[self.id, 'call', 'passed']] * 3)
        self.assertEqual(validate_pytest(report, {1: self.id}), (3, []))

    def test_extra_collected_only_if_setup_skipped_and_teardown_passed(self):
        report = copy.deepcopy(self.report)
        report['collected'].append('extra')
        report['phases'] += [['extra', 'setup', 'skipped'], ['extra', 'teardown', 'passed']]
        self.assertEqual(validate_pytest(report, {1: self.id}), (0, ['extra']))
        report['phases'].append(['extra', 'call', 'passed'])
        with self.assertRaises(ValueError):
            validate_pytest(report, {1: self.id})
        report['phases'][-3:] = [['extra', 'setup', 'passed'], ['extra', 'call', 'skipped'],
                                 ['extra', 'teardown', 'passed']]
        self.assertEqual(validate_pytest(report, {1: self.id}), (0, ['extra']))

    def test_collection_missing_extra_or_duplicate_id(self):
        for update in [dict(collected=[]), dict(collected=[self.id, self.id]),
                       dict(collection_errors=['broken']), dict(exit_code=1)]:
            with self.assertRaises(ValueError):
                validate_pytest(dict(self.report, **update), {1: self.id})

    def test_wrong_fingerprint_cleanup_timeout_platform_or_manifest(self):
        for key, value in [('base_fingerprint', 'bad'), ('after_fingerprint', 'bad'),
                           ('coverage_sha256', 'bad'), ('tests', []), ('platform', 'win32'),
                           ('python_version', [3, 14]), ('timed_out', True),
                           ('cleanup_killed', True), ('returncode', 1), ('repo', '/other/')]:
            self.write('run.json', dict(self.metadata, **{key: value}))
            with self.assertRaises(ValueError, msg=key):
                self.run_validate()

    def test_missing_process_start_exit_or_body(self):
        for suffix in ('start', 'done', 'body'):
            self.process(101, 1, {'102': 'Popen'})
            self.process(102, 101, {})
            (self.run / f'102.{suffix}.json').unlink()
            with self.assertRaises(ValueError, msg=suffix):
                self.run_validate()

    def test_unaccounted_process(self):
        self.process(102, 101, {})
        with self.assertRaises(ValueError):
            self.run_validate()

    def test_pid_alias_cannot_hide_unaccounted_report(self):
        self.write('0101.body.json', {})
        with self.assertRaises(ValueError):
            self.run_validate()

    def test_wrong_parent_and_cyclic_graph(self):
        self.process(101, 1, {'102': 'Popen'})
        self.process(102, 999, {})
        with self.assertRaises(ValueError):
            self.run_validate()
        self.process(101, 1, {})
        self.process(102, 103, {'103': 'fork'})
        self.process(103, 102, {'102': 'fork'})
        with self.assertRaises(ValueError):
            self.run_validate()

    def test_bad_process_attestations(self):
        original = json.loads((self.run / '101.done.json').read_text())
        for update in [dict(token='bad'), dict(complete=False), dict(trace_installed=False),
                       dict(errors=['removed hook']), dict(repo='/other/'),
                       dict(lines={'generated.py': [1]}), dict(lines={'subject.py': [3]})]:
            self.write('101.done.json', dict(original, **update))
            with self.assertRaises(ValueError, msg=repr(update)):
                self.run_validate()

    def test_unknown_instrumentation_discards_whole_file(self):
        self.process(101, 1, {}, unknown=['subject.py'])
        self.assertEqual(self.run_validate()[0], {})

    def test_source_path_hash_ast_operator_and_empty_observations(self):
        mutations = [lambda x: x['subject.py'].update(sha256='bad'),
                     lambda x: x['subject.py']['sites'][self.site].update(ast='bad'),
                     lambda x: x['subject.py']['sites'][self.site].update(operator='Eq'),
                     lambda x: x['subject.py']['sites'][self.site].update(observations=[]),
                     lambda x: x['subject.py']['sites'].update(
                         {'1:0:1:1': x['subject.py']['sites'].pop(self.site)}),
                     lambda x: x.update({'unknown.py': x.pop('subject.py')})]
        for mutate in mutations:
            files = copy.deepcopy(self.files)
            mutate(files)
            self.process(101, 1, {}, files=files)
            with self.assertRaises(ValueError):
                self.run_validate()

    def test_null_and_union_overflow_cannot_supply_proof(self):
        self.process(101, 1, {'102': 'Popen'})
        files = copy.deepcopy(self.files)
        files['subject.py']['sites'][self.site]['observations'] = [None]
        self.process(102, 101, {}, files=files)
        result = self.run_validate()[0]
        self.assertEqual(result['subject.py']['sites'][self.site]['observations'], [None])

    def test_process_union_above_bound_collapses_to_null(self):
        for pid, first in ((101, 0), (102, 40)):
            files = copy.deepcopy(self.files)
            files['subject.py']['sites'][self.site]['observations'] = [
                [['scalar', 'int', str(n)], ['identity'], False, False]
                for n in range(first, first + 40)]
            self.process(pid, 1 if pid == 101 else 101,
                         {'102': 'Popen'} if pid == 101 else {}, files=files)
        result = self.run_validate()[0]
        self.assertEqual(result['subject.py']['sites'][self.site]['observations'], [None])

    def test_no_execution_creates_no_site(self):
        self.process(101, 1, {}, files={})
        self.assertEqual(self.run_validate()[0], {})

    def test_invalid_operand_structure_rejected(self):
        for item in ([['scalar', 'int', '1'], ['identity'], 1, False],
                     [['identity'], ['identity'], False, None],
                     [['scalar', 'str', 'x'], ['identity'], False, True], [1, 2, 3, 4]):
            files = copy.deepcopy(self.files)
            files['subject.py']['sites'][self.site]['observations'] = [item]
            with self.assertRaises(ValueError):
                validate_files(files, self.repo, self.coverage)


if __name__ == '__main__':
    unittest.main()
