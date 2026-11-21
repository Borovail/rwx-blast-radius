import ast
import copy
import hashlib
import unittest

from terminal_assert import TerminalAssertions
from source_reads import BASE_COMMIT, METHOD


PATH = 'tests/test_value.py'
SOURCE = '''import unittest
class TestValue(unittest.TestCase):
    def test_value(self):
        self.assertEqual(1, 1)
'''
TESTS = [
    {'number': 0, 'id': PATH + '::TestValue::test_value'},
    {'number': 1, 'id': PATH + '::TestValue::test_owner'},
    {'number': 2, 'id': 'tests/test_reader.py::TestReader::test_read'},
    {'number': 3, 'id': 'tests/test_positions.py::TestPositions::test_code'},
    {'number': 4, 'id': 'tests/test_unrelated.py::TestUnrelated::test_other'},
]


def artifact(source=SOURCE):
    method = ast.parse(source).body[1].body[0]
    call = method.body[-1].value
    literal = call.args[1]
    site = {
        'path': PATH, 'qualname': 'TestValue.test_value', 'first_line': method.lineno,
        'number': 0, 'call_span': [call.lineno, call.end_lineno, call.col_offset, call.end_col_offset],
        'literal_span': [literal.lineno, literal.end_lineno, literal.col_offset, literal.end_col_offset],
        'callers': [0], 'calls': 1, 'main_process_only': True, 'opaque': [],
        'values': [['int', '1', 'int', '1']],
    }
    return {
        'version': 1, 'base_commit': BASE_COMMIT, 'tests': TESTS,
        'context': {
            'complete': True, 'method': METHOD, 'known_only': [PATH], 'unknown_startup': [],
            'coverage_sha256': 'c', 'positions_sha256': 'p', 'unfinished': [],
            'process_errors': [], 'unobserved_children': [], 'skipped': [],
            'passed': list(range(5)), 'failed': [], 'missing': [], 'unknown_tests': [],
        },
        'paths': {PATH: {
            'sha256': hashlib.sha256(source.encode()).hexdigest(), 'owners': [0, 1],
            'known_startup_count': 1, 'unknown_startup': [], 'readers': [2],
        }},
        'terminal': {'version': 1, 'complete': True, 'policy': 'terminal-assert-equal-self-only-v1',
                     'sites': {TESTS[0]['id']: site}},
    }


class TerminalAssertionTests(unittest.TestCase):
    def select(self, changed, data=None, source=SOURCE, **options):
        runtime = TerminalAssertions(data or artifact(source), TESTS, 'c', 'p')
        kwargs = dict(single_file=True, position_observers={3})
        kwargs.update(options)
        return runtime.select(PATH, source, changed, {'readers': [2]}, **kwargs)

    def test_literal_failure_keeps_all_owners_readers_and_positions(self):
        self.assertEqual(self.select(SOURCE.replace('(1, 1)', '(1, 2)')), {0, 1, 2, 3})

    def test_hunk_line_must_match_the_certified_token(self):
        changed = SOURCE.replace('(1, 1)', '(1, 2)')
        self.assertIsNone(self.select(changed, changed_line=3))
        self.assertEqual(self.select(changed, changed_line=4), {0, 1, 2, 3})

    def test_prior_effects_are_allowed_and_unchanged(self):
        source = SOURCE.replace('        self.assertEqual', '        print("completed")\n        self.assertEqual')
        self.assertEqual(self.select(source.replace('(1, 1)', '(1, 2)'), source=source), {0, 1, 2, 3})

    def test_actual_argument_change_declines(self):
        self.assertIsNone(self.select(SOURCE.replace('(1, 1)', '(2, 1)')))

    def test_unrelated_bytes_or_directive_decline(self):
        for extra in ('# extra\n', '# type: ignore\n', '\n'):
            with self.subTest(extra=extra):
                self.assertIsNone(self.select(SOURCE.replace('(1, 1)', '(1, 2)') + extra))

    def test_added_assert_or_statement_declines(self):
        self.assertIsNone(self.select(SOURCE + '        assert True\n'))

    def test_expression_expectation_declines(self):
        self.assertIsNone(self.select(SOURCE.replace('(1, 1)', '(1, 1 + 1)')))

    def test_missing_or_external_method_call_attestation_declines(self):
        for field, value in (('callers', [0, 4]), ('callers', []), ('calls', 2),
                             ('main_process_only', False), ('values', []), ('opaque', ['unknown'])):
            data = artifact()
            data['terminal']['sites'][TESTS[0]['id']][field] = value
            with self.subTest(field=field, value=value):
                self.assertIsNone(self.select(SOURCE.replace('(1, 1)', '(1, 2)'), data))

    def test_shared_or_unknown_process_declines(self):
        for field in ('unfinished', 'process_errors', 'unobserved_children', 'failed', 'missing'):
            data = artifact()
            data['context'][field] = [4]
            with self.subTest(field=field):
                self.assertIsNone(self.select(SOURCE.replace('(1, 1)', '(1, 2)'), data))

    def test_unknown_startup_and_stale_hash_decline(self):
        data = artifact()
        data['paths'][PATH]['unknown_startup'] = [['other-reader']]
        self.assertIsNone(self.select(SOURCE.replace('(1, 1)', '(1, 2)'), data))
        data = artifact()
        data['paths'][PATH]['sha256'] = 'wrong'
        self.assertIsNone(self.select(SOURCE.replace('(1, 1)', '(1, 2)'), data))

    def test_multiple_file_or_missing_position_observers_declines(self):
        self.assertIsNone(self.select(SOURCE.replace('(1, 1)', '(1, 2)'), single_file=False))
        self.assertIsNone(self.select(SOURCE.replace('(1, 1)', '(1, 2)'), position_observers=None))

    def test_child_group_widening_is_transitive(self):
        data = artifact()
        data['context']['shared_pool_groups'] = [{'tests': [0, 3]}, {'tests': [3, 4]}]
        self.assertEqual(self.select(SOURCE.replace('(1, 1)', '(1, 2)'), data), set(range(5)))

    def test_adjacent_literal_token_rewrite_declines(self):
        source = SOURCE.replace('(1, 1)', '("ab", "a" "b")')
        self.assertIsNone(self.select(source.replace('"a" "b"', '"cd"'), source=source))

    def test_compile_warning_declines(self):
        source = SOURCE.replace('(1, 1)', '("a", "a")')
        changed = source.replace('("a", "a")', '("a", "\\z")')
        self.assertIsNone(self.select(changed, source=source))

    def test_owner_observing_changed_code_declines(self):
        source = SOURCE.replace('        self.assertEqual', '        print(self.test_value.__func__.__code__)\n        self.assertEqual')
        self.assertIsNone(self.select(source.replace('(1, 1)', '(1, 2)'), source=source))
        data = artifact()
        data['paths'][PATH]['readers'].append(0)
        self.assertIsNone(self.select(SOURCE.replace('(1, 1)', '(1, 2)'), data))


if __name__ == '__main__':
    unittest.main()
