"""Strict terminal-assert observations; no patched code or user protocols run."""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
import difflib
import hashlib
import inspect
from pathlib import Path
import sys
import threading
import types
import unittest
import unittest.case
import unittest.util


SCALARS = (str, bytes, int, float, bool, type(None))


def code_fingerprint(code):
    constants = tuple(
        ('code', code_fingerprint(value)) if type(value) is types.CodeType
        else (type(value).__name__, repr(value)) for value in code.co_consts
    )
    fields = (code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount,
              code.co_nlocals, code.co_stacksize, code.co_flags, code.co_code,
              constants, code.co_names, code.co_varnames, code.co_freevars,
              code.co_cellvars, code.co_firstlineno, code.co_linetable,
              code.co_exceptiontable, code.co_name, code.co_qualname)
    return hashlib.sha256(repr(fields).encode()).hexdigest()


def sites_for(repo, tests):
    """Index existing, direct final assertions with no retained user locals."""
    modules = {}
    sites = {}
    for row in tests:
        parts = row['id'].split('::')
        if len(parts) != 3:
            continue
        path, cls_name, method_name = parts
        if path not in modules:
            source = (Path(repo) / path).read_text('utf-8')
            codes = {}
            pending = [compile(source, path, 'exec', dont_inherit=True)]
            while pending:
                code = pending.pop()
                codes[code.co_qualname] = code
                pending.extend(value for value in code.co_consts if type(value) is types.CodeType)
            modules[path] = ast.parse(source), codes
        tree, codes = modules[path]
        classes = [node for node in tree.body if type(node) is ast.ClassDef and node.name == cls_name]
        if len(classes) != 1:
            continue
        methods = [node for node in classes[0].body if type(node) is ast.FunctionDef and node.name == method_name]
        if len(methods) != 1:
            continue
        method = methods[0]
        if method.decorator_list or type(method.body[-1]) is not ast.Expr:
            continue
        call = method.body[-1].value
        if (type(call) is not ast.Call or type(call.func) is not ast.Attribute
                or type(call.func.value) is not ast.Name or call.func.value.id != 'self'
                or call.func.attr != 'assertEqual' or len(call.args) != 2 or call.keywords
                or type(call.args[1]) is not ast.Constant or type(call.args[1].value) not in SCALARS):
            continue
        code = codes[f'{cls_name}.{method_name}']
        if (code.co_varnames != ('self',) or code.co_freevars or code.co_cellvars
                or code.co_flags & (inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR)):
            continue
        sites[row['id']] = {
            'path': path, 'qualname': code.co_qualname, 'first_line': method.lineno,
            'number': row['number'],
            'code_sha256': code_fingerprint(code),
            'call_span': [call.lineno, call.end_lineno, call.col_offset, call.end_col_offset],
            'literal_span': [call.args[1].lineno, call.args[1].end_lineno,
                             call.args[1].col_offset, call.args[1].end_col_offset],
        }
    return sites


class TerminalMixin:
    def __init__(self, config):
        self.sites = config['terminal_sites']
        self.site_locations = {
            (row['path'], row['qualname'], row['first_line']): key
            for key, row in self.sites.items()
        }
        self.method_codes = {}
        self.terminal_calls = Counter()
        self.method_calls = Counter()
        self.terminal_values = defaultdict(set)
        self.terminal_opaque = defaultdict(set)
        self.terminal_items = {}
        self.terminal_hooks = {}
        self.terminal_main_pid = config.get('terminal_controller_pid', __import__('os').getpid())
        self.terminal_tool = 4
        self.standard_methods = {
            name: value for name, value in vars(unittest.TestCase).items()
            if type(value) in (types.FunctionType, classmethod, staticmethod)
        }
        self.standard_functions = [
            (module, name, value)
            for module in (unittest.case, unittest.util, difflib)
            for name, value in vars(module).items()
            if type(value) in (types.FunctionType, type)
        ]
        self.standard_equality = unittest.TestCase()._type_equality_funcs.copy()
        self.standard_classes = [
            (value, dict(vars(value)))
            for module in (unittest.case, unittest.util, difflib)
            for value in vars(module).values() if type(value) is type
        ]
        sys.monitoring.use_tool_id(self.terminal_tool, 'terminal-assert-facts')
        sys.monitoring.register_callback(self.terminal_tool, sys.monitoring.events.PY_START, self.terminal_start)
        sys.monitoring.set_local_events(
            self.terminal_tool, unittest.TestCase.assertEqual.__code__, sys.monitoring.events.PY_START
        )
        super().__init__(config)
        sys.addaudithook(self.terminal_audit)

    def after_fork(self):
        super().after_fork()
        self.terminal_calls = Counter()
        self.method_calls = Counter()
        self.terminal_values = defaultdict(set)
        self.terminal_opaque = defaultdict(set)

    def terminal_audit(self, event, args):
        if event != 'exec' or type(args[0]) is not types.CodeType:
            return
        pending = [args[0]]
        while pending:
            code = pending.pop()
            key = self.site_locations.get((self.relative(code.co_filename), code.co_qualname, code.co_firstlineno))
            if key is not None:
                if code_fingerprint(code) != self.sites[key]['code_sha256']:
                    self.terminal_opaque[key].add('noncanonical-code-or-rewriting')
                self.method_codes[id(code)] = code, key
                sys.monitoring.set_local_events(self.terminal_tool, code, sys.monitoring.events.PY_START)
            pending.extend(value for value in code.co_consts if type(value) is types.CodeType)

    def note_item(self, item, number):
        from _pytest.unittest import TestCaseFunction, UnitTestCase

        if number is None:
            return
        reasons = []
        if type(item) is not TestCaseFunction:
            reasons.append('nonstandard-pytest-item')
        expected_code = next(
            value for value in UnitTestCase._register_unittest_setup_class_fixture.__code__.co_consts
            if type(value) is types.CodeType and value.co_name == 'unittest_setup_class_fixture'
        )
        for name in item.fixturenames:
            if name == 'request':
                continue
            definitions = item._fixtureinfo.name2fixturedefs.get(name, ())
            if len(definitions) != 1 or definitions[0].func.__code__ is not expected_code:
                reasons.append('custom-or-unknown-fixture:' + name)
        self.terminal_items[number] = item, reasons
        hooks = self.hook_signature(item)
        if hooks is None:
            reasons.append('custom-or-unknown-outcome-hook')
        self.terminal_hooks[number] = hooks

    def hook_signature(self, item):
        """Only pinned pytest outcome hooks and this exact collector are allowed."""
        import pytest
        import _pytest
        from pluggy._hooks import HookCaller, HookImpl
        pytest_roots = tuple(str(Path(module.__file__).resolve().parent) + '/' for module in (pytest, _pytest))
        collector = self.config.get('terminal_collector_path')
        signatures = []
        for name in ('pytest_runtest_protocol', 'pytest_runtest_call',
                     'pytest_runtest_makereport', 'pytest_runtest_logreport',
                     'pytest_runtest_teardown', 'pytest_exception_interact',
                     'pytest_sessionfinish', 'pytest_terminal_summary'):
            hook = getattr(item.ihook, name)
            if type(hook) is not HookCaller:
                return None
            rows = []
            for implementation in hook.get_hookimpls():
                if type(implementation) is not HookImpl:
                    return None
                function = implementation.function
                if type(function) is types.MethodType:
                    function = function.__func__
                if type(function) is not types.FunctionType:
                    return None
                filename = str(Path(function.__code__.co_filename).resolve())
                if not filename.startswith(pytest_roots) and filename != collector:
                    return None
                rows.append((function.__module__, function.__qualname__, code_fingerprint(function.__code__)))
            signatures.append((name, tuple(rows)))
        return tuple(signatures)

    def instance_safe(self, instance, caller, number):
        """Reject outcome-reactive cleanup, custom formatting and retained locals."""
        if type(type(instance)) is not type or set(caller.f_locals) != {'self'}:
            return False
        cls = type(instance)
        if (inspect.getattr_static(cls, '__getattribute__') is not object.__getattribute__
                or inspect.getattr_static(cls, '__setattr__') is not object.__setattr__
                or inspect.getattr_static(cls, '__delattr__') is not object.__delattr__
                or inspect.getattr_static(cls, '__getattr__', None) is not None
                or inspect.getattr_static(cls, '__del__', None) is not None):
            return False
        for name, value in self.standard_methods.items():
            if inspect.getattr_static(cls, name) is not value:
                return False
        for module, name, value in self.standard_functions:
            if vars(module).get(name) is not value:
                return False
        for expected_class, bindings in self.standard_classes:
            current = vars(expected_class)
            if current.keys() != bindings.keys() or any(current[name] is not value for name, value in bindings.items()):
                return False
        state = object.__getattribute__(instance, '__dict__')
        if type(state) is not dict:
            return False
        permitted = {'_testMethodName', '_outcome', '_cleanups', '_subtest',
                     '_type_equality_funcs', '_testMethodDoc', state.get('_testMethodName')}
        if set(state) - permitted:
            return False
        if (type(state.get('_testMethodName')) is not str
                or state['_testMethodName'] != caller.f_code.co_name
                or type(state.get('_testMethodDoc')) not in (str, type(None))):
            return False
        bound = state.get(state['_testMethodName'])
        if type(bound) is not types.MethodType or bound.__func__.__code__ is not caller.f_code:
            return False
        cleanups = state.get('_cleanups')
        if type(cleanups) is not list or cleanups or state.get('_subtest') is not None:
            return False
        equality = state.get('_type_equality_funcs')
        if (type(equality) is not dict
                or any(type(key) is not type or type(value) is not str
                       for key, value in equality.items())
                or equality != self.standard_equality):
            return False
        outcome = state.get('_outcome')
        from unittest.case import _Outcome
        from _pytest.unittest import TestCaseFunction
        if type(outcome) is not _Outcome or type(outcome.result) is not TestCaseFunction:
            return False
        if self.terminal_items.get(number, (None, ['unknown-item'])) != (outcome.result, []):
            return False
        if self.hook_signature(outcome.result) != self.terminal_hooks.get(number):
            return False
        if outcome.result.instance is not instance:
            return False
        module_name = type.__getattribute__(cls, '__module__')
        class_name = type.__getattribute__(cls, '__qualname__')
        module = sys.modules.get(module_name)
        if type(module) is not types.ModuleType or caller.f_globals is not vars(module):
            return False
        if (vars(module).get(class_name) is not cls
                or self.relative(vars(module).get('__file__', '')) != self.relative(caller.f_code.co_filename)
                or caller.f_code.co_qualname != class_name + '.' + caller.f_code.co_name):
            return False
        original = vars(cls).get(caller.f_code.co_name)
        if (type(original) is not types.FunctionType or original is not bound.__func__
                or original.__globals__ is not caller.f_globals):
            return False
        if inspect.getattr_static(cls, 'failureException') is not AssertionError:
            return False
        class_cleanups = inspect.getattr_static(cls, '_class_cleanups', None)
        if type(class_cleanups) is not list or class_cleanups:
            return False
        for name in ('maxDiff', '_diffThreshold', 'longMessage'):
            if type(inspect.getattr_static(cls, name)) not in (int, bool, type(None)):
                return False
        return True

    def terminal_start(self, code, offset):
        frame = sys._getframe(1)
        entry = self.method_codes.get(id(code))
        if entry is not None and entry[0] is code:
            key = entry[1]
            number = self.active
            if number is None or threading.get_ident() != self.main_thread:
                self.terminal_opaque[key].add('startup-or-unattributed-method-call')
            else:
                self.method_calls[key, number] += 1
            return
        caller = frame.f_back
        if caller is None:
            return
        entry = self.method_codes.get(id(caller.f_code))
        if entry is None or entry[0] is not caller.f_code:
            return
        key = entry[1]
        span = tuple(caller.f_code.co_positions())[caller.f_lasti // 2]
        if list(span) != self.sites[key]['call_span']:
            return
        number = self.active
        if number is None or threading.get_ident() != self.main_thread:
            self.terminal_opaque[key].add('startup-or-unattributed-terminal-call')
            return
        self.terminal_calls[key, number] += 1
        values = frame.f_locals
        first, second = values.get('first'), values.get('second')
        if (type(first) not in SCALARS or type(second) not in SCALARS
                or values.get('msg') is not None):
            self.terminal_opaque[key].add('custom-comparison-or-message')
            return
        if not first == second:
            self.terminal_opaque[key].add('baseline-not-equal')
            return
        if not self.instance_safe(values['self'], caller, number):
            self.terminal_opaque[key].add('custom-instance-outcome-or-framework-bindings')
            return
        if type(first) in (str, bytes) and len(first) > 4096:
            self.terminal_opaque[key].add('large-scalar')
            return
        if type(first) is int and first.bit_length() > 8192:
            self.terminal_opaque[key].add('large-scalar')
            return
        self.terminal_values[key].add((type(first).__name__, repr(first), type(second).__name__, repr(second)))

    def finish(self):
        if self.done:
            return
        super().finish()
        import json
        path = self.output / f'{self.identity}.done.json'
        data = json.loads(path.read_bytes())
        data['terminal'] = {
            'main_pid': self.terminal_main_pid,
            'method_calls': [[key, number, count] for (key, number), count in sorted(self.method_calls.items())],
            'terminal_calls': [[key, number, count] for (key, number), count in sorted(self.terminal_calls.items())],
            'values': {key: sorted(value) for key, value in self.terminal_values.items()},
            'opaque': {key: sorted(value) for key, value in self.terminal_opaque.items()},
        }
        path.write_text(json.dumps(data, sort_keys=True, separators=(',', ':')))


def attach_terminal(artifact, observations, config):
    """Require every invocation to be a single controller-owned native check."""
    import json
    context = artifact['context']
    if context['complete'] is not True:
        raise ValueError('incomplete source-read protocol')
    controller = config['terminal_controller_pid']
    known = set(context['known_only'])
    calls, terminal = Counter(), Counter()
    values, opaque, processes = defaultdict(set), defaultdict(set), defaultdict(set)
    done = sorted(Path(observations).glob('*.done.json'))
    if len(done) != context['processes']:
        raise ValueError('missing process completion')
    for path in done:
        row = json.loads(path.read_bytes())
        facts = row['terminal']
        if facts['main_pid'] != controller:
            raise ValueError('child controller mismatch')
        for key, number, count in facts['method_calls']:
            if type(number) is not int or number not in context['passed'] or type(count) is not int or count <= 0:
                raise ValueError('invalid positive method call')
            calls[key, number] += count
            processes[key].add(row['pid'])
        for key, number, count in facts['terminal_calls']:
            if type(number) is not int or number not in context['passed'] or type(count) is not int or count <= 0:
                raise ValueError('invalid positive terminal call')
            terminal[key, number] += count
        for key, rows in facts['values'].items():
            values[key].update(tuple(value) for value in rows)
        for key, reasons in facts['opaque'].items():
            opaque[key].update(reasons)
    certified, rejected = {}, {}
    for key, site in config['terminal_sites'].items():
        owner = site['number']
        observed = {number: count for (name, number), count in calls.items() if name == key}
        observed_terminal = {number: count for (name, number), count in terminal.items() if name == key}
        reasons = set(opaque[key])
        if observed != {owner: 1} or observed_terminal != observed:
            reasons.add('missing-repeated-or-external-call')
        if processes[key] != {controller}:
            reasons.add('child-or-unattributed-invocation')
        if not values[key]:
            reasons.add('no-typed-comparison-witness')
        if site['path'] not in known:
            reasons.add('opaque-source-read')
        if reasons:
            rejected[key] = sorted(reasons)
        else:
            certified[key] = dict(site, callers=[owner], calls=1, main_process_only=True,
                                  opaque=[], values=sorted(values[key]))
    artifact['terminal'] = {
        'version': 1, 'policy': 'terminal-assert-equal-self-only-v1', 'complete': True,
        'controller_pid': controller, 'sites': certified, 'rejected': rejected,
        'collector_sha256': context['generator_sha256'],
        'support_sha256': config['terminal_support_sha256'],
    }
    return artifact
