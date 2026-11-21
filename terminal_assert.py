"""Optional source-read bypass for an exact witnessed final assertEqual."""

from __future__ import annotations

import ast
import io
import tokenize
import warnings

from source_reads import TestMethodReads, _dump, _methods
from trivia import observer_lines


def terminal_literal_change(old_source, new_source, baseline, site, path):
    old, methods = baseline
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        new = ast.parse(new_source)
        compile(new_source.encode('utf-8'), path, 'exec', dont_inherit=True)
    parts = site['qualname'].split('.')
    if len(parts) != 2:
        return None
    key = tuple(parts)
    before = methods.get(key)
    after = _methods(new).get(key)
    if before is None or after is None or before.lineno != site['first_line']:
        return None
    if any(before.lineno <= line <= before.end_lineno for line in observer_lines(old_source)):
        return None
    if type(before.body[-1]) is not ast.Expr or type(after.body[-1]) is not ast.Expr:
        return None
    left_call, right_call = before.body[-1].value, after.body[-1].value
    if type(left_call) is not ast.Call or type(right_call) is not ast.Call:
        return None
    if len(left_call.args) != 2 or len(right_call.args) != 2:
        return None
    left, right = left_call.args[1], right_call.args[1]
    if type(left) is not ast.Constant or type(right) is not ast.Constant:
        return None
    if (type(left.value) not in (str, bytes, int, float, bool, type(None))
            or type(left.value) is not type(right.value) or left.kind != right.kind
            or repr(left.value) == repr(right.value)):
        return None
    span = [left.lineno, left.end_lineno, left.col_offset, left.end_col_offset]
    if span != site['literal_span'] or left.lineno != left.end_lineno:
        return None
    if (right.lineno != right.end_lineno or right.lineno != left.lineno
            or right.col_offset != left.col_offset):
        return None
    old_lines, new_lines = old_source.encode().split(b'\n'), new_source.encode().split(b'\n')
    index = left.lineno - 1
    replacement = new_lines[index][right.col_offset:right.end_col_offset]
    original = old_lines[index][left.col_offset:left.end_col_offset]
    for fragment in (original, replacement):
        tokens = [token for token in tokenize.generate_tokens(io.StringIO(fragment.decode()).readline)
                  if token.type not in (tokenize.ENDMARKER, tokenize.NEWLINE)]
        if len(tokens) != 1 or tokens[0].type not in (tokenize.NUMBER, tokenize.STRING, tokenize.NAME):
            return None
    old_lines[index] = old_lines[index][:left.col_offset] + replacement + old_lines[index][left.end_col_offset:]
    if b'\n'.join(old_lines) != new_source.encode():
        return None
    if type(right.value) in (str, bytes) and len(right.value) > 4096:
        return None
    left_value, right_value = left.value, right.value
    try:
        left.value = right.value = None
        if _dump(old) != _dump(new):
            return None
    finally:
        left.value, right.value = left_value, right_value
    return right_value


class TerminalAssertions(TestMethodReads):
    def __init__(self, artifact, tests, coverage_sha256, positions_sha256):
        super().__init__(artifact, tests, coverage_sha256, positions_sha256)
        self.sites = {}
        self.groups = []
        if not self.ready:
            return
        self.ready = False
        try:
            proof = artifact['terminal']
            if (proof['version'] != 1 or proof['complete'] is not True
                    or proof['policy'] != 'terminal-assert-equal-self-only-v1'):
                return
            context = artifact['context']
            if (set(context['passed']) | self.skipped != self.numbers
                    or any(context[name] for name in ('failed', 'missing', 'unknown_tests'))):
                return
            for group in context.get('shared_pool_groups', ()):
                numbers = set(group['tests'])
                if any(type(number) is not int or number not in self.numbers for number in numbers):
                    return
                self.groups.append(numbers)
            for nodeid, site in proof['sites'].items():
                owner = self.numbers_by_id[nodeid]
                if (site['path'] not in self.paths or nodeid != site['path'] + '::' + site['qualname'].replace('.', '::')
                        or site['number'] != owner or site['callers'] != [owner]
                        or type(site['calls']) is not int or site['calls'] != 1
                        or site['main_process_only'] is not True or site['opaque']
                        or owner in self.skipped or not site['values']):
                    return
                self.sites.setdefault(site['path'], []).append(site)
            self.ready = True
        except (AttributeError, KeyError, TypeError, ValueError):
            self.sites = {}

    def select(self, path, old_source, new_source, record, *, position_observers,
               single_file=False, changed_line=None):
        if (not single_file or not self.ready or path not in self.sites
                or position_observers is None):
            return None
        try:
            sites = [site for site in self.sites[path]
                     if changed_line is None or site['literal_span'][0] == changed_line]
            if not sites:
                return None
            baseline = self._baseline(path, old_source)
            if baseline is None:
                return None
            matched = []
            for site in sites:
                value = terminal_literal_change(old_source, new_source, baseline, site, path)
                if value is not None:
                    matched.append(site)
            if len(matched) != 1:
                return None
            observers = set(position_observers)
            if any(type(number) is not int or number not in self.numbers for number in observers):
                return None
            row = self.paths[path]
            owner = matched[0]['number']
            if owner in set(row['readers']) | set(record['readers']) | observers:
                return None
            # Keep all file owners and every separately attributable source reader.
            selected = (set(row['owners']) | set(row['readers']) | set(record['readers'])
                        | set(matched[0]['callers']) | observers | self.skipped)
            previous = None
            while selected != previous:
                previous = selected.copy()
                for group in self.groups:
                    if selected & group:
                        selected.update(group)
            return selected
        except (SyntaxError, Warning, ValueError, TypeError, IndexError, KeyError, RecursionError,
                MemoryError, tokenize.TokenError):
            return None
