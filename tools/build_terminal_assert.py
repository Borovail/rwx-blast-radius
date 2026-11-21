#!/usr/bin/env python3
"""Measure exact pytest import reads separately from all other test-source reads.

Inputs are the pinned checkout, manifest and checked coverage map only. Every
Python descendant must supply a complete process protocol. No patches or patch
outcomes are inputs. Unknown startup reads always override known import reads.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import marshal
import os
from pathlib import Path
import sys
import tempfile
import threading
import types

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIRECTORY.parent if SCRIPT_DIRECTORY.name == 'tools' else SCRIPT_DIRECTORY.parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from tools.build_dispatch import Observer, read_json, write_json, refresh_dependencies

BASE_COMMIT = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
METHOD = 'exact-pytest-rewrite-test-source-reads-v1'


def positions_digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


class TestReadObserver(Observer):
    def __init__(self, config):
        import pytest
        from _pytest.assertion.rewrite import AssertionRewritingHook, _rewrite_test
        self.valid = set(config['test_paths'])
        self.known_startup = Counter()
        self.unknown_startup = defaultdict(set)
        self.test_readers = defaultdict(set)
        self.main_thread = threading.get_ident()
        self.codes = (Path.open.__code__, Path.read_bytes.__code__, _rewrite_test.__code__,
                      AssertionRewritingHook.exec_module.__code__)
        self.hook_type = AssertionRewritingHook
        super().__init__(config)
        sys.addaudithook(self.source_audit)

    def after_fork(self):
        super().after_fork()
        self.known_startup = Counter()
        self.unknown_startup = defaultdict(set)
        self.test_readers = defaultdict(set)
        self.main_thread = threading.get_ident()

    def relative(self, value):
        try:
            path = os.path.abspath(os.fsdecode(value))
            root = str(self.repo) + os.sep
            return path[len(root):].replace(os.sep, '/') if path.startswith(root) else None
        except (TypeError, ValueError, OSError):
            return None

    def known_rewrite(self, frames, path):
        if len(frames) != 4 or tuple(frame.f_code for frame in frames) != self.codes:
            return False
        opening, reading, rewriting, executing = (frame.f_locals for frame in frames)
        target = reading.get('self')
        module = executing.get('module')
        hook = executing.get('self')
        return (opening.get('self') is target and rewriting.get('fn') is target
                and executing.get('fn') is target and self.relative(target) == path
                and opening.get('mode') == 'rb' and type(hook) is self.hook_type
                and rewriting.get('config') is hook.config
                and type(module) is types.ModuleType and module.__spec__ is not None
                and self.relative(module.__spec__.origin) == path)

    def source_audit(self, event, args):
        if event != 'open' or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        mode, flags = args[1:3]
        if ((isinstance(mode, str) and 'r' not in mode and '+' not in mode)
                or (mode is None and flags & os.O_WRONLY and not flags & os.O_RDWR)):
            return
        caller = sys._getframe(1)
        if caller.f_code.co_filename == '<frozen importlib._bootstrap_external>':
            return
        path = self.relative(args[0])
        if path not in self.valid:
            return
        frames = []
        while caller is not None and len(frames) < 4:
            frames.append(caller)
            caller = caller.f_back
        signature = tuple(frame.f_code.co_qualname for frame in frames)
        if threading.get_ident() != self.main_thread:
            self.unknown_startup[path].add(('unattributed-thread', *signature))
        elif self.active is not None:
            self.test_readers[path].add(self.active)
        elif self.known_rewrite(frames, path):
            self.known_startup[path] += 1
        else:
            self.unknown_startup[path].add(signature)

    def finish(self):
        if self.done:
            return
        super().finish()
        path = self.output / f'{self.identity}.done.json'
        data = read_json(path)
        data.update(method=METHOD, known_startup=dict(self.known_startup),
                    unknown_startup={key: sorted(value) for key, value in self.unknown_startup.items()},
                    test_readers={key: sorted(value) for key, value in self.test_readers.items()})
        write_json(path, data)


from terminal_support import TerminalMixin, sites_for, attach_terminal


class TerminalObserver(TerminalMixin, TestReadObserver):
    pass


def install(config):
    return TerminalObserver(config)


def child_install():
    if os.environ.get('BR_DISPATCH_CONFIG'):
        install(read_json(os.environ['BR_DISPATCH_CONFIG']))


def merge_sources(artifact, directories):
    expected = {row['number'] for row in artifact['tests']}
    widened = {}
    for group in artifact['context'].get('shared_pool_groups', ()):
        for number in group['tests']:
            widened.setdefault(number, set()).update(group['tests'])
    for number in list(widened):
        pending, component = [number], set()
        while pending:
            current = pending.pop()
            if current not in component:
                component.add(current)
                pending.extend(widened.get(current, ()))
        for current in component:
            widened[current] = component
    known, unknown, readers = Counter(), defaultdict(set), defaultdict(set)
    for directory in directories:
        for start in sorted(Path(directory).glob('*.start.json')):
            begin = read_json(start)
            done = start.with_name(start.name.replace('.start.json', '.done.json'))
            data = read_json(done)
            if data.get('method') != METHOD or data.get('process_errors'):
                raise ValueError('incomplete or unsupported source-read process protocol')
            for path, count in data['known_startup'].items():
                if type(count) is not int or count <= 0:
                    raise ValueError('invalid known import count')
                known[path] += count
            for path, signatures in data['unknown_startup'].items():
                unknown[path].update(tuple(signature) for signature in signatures)
            for path, numbers in data['test_readers'].items():
                if any(type(n) is not int or n not in expected for n in numbers):
                    raise ValueError('unknown source reader number')
                readers[path].update(numbers)
                if begin['test'] is not None:
                    for number in numbers:
                        readers[path].update(widened.get(number, ()))
    for path, row in artifact['paths'].items():
        row.update(known_startup_count=known[path],
                   unknown_startup=[list(signature) for signature in sorted(unknown[path])],
                   readers=sorted(readers[path]))
    eligible = sorted(path for path in artifact['paths'] if known[path] and not unknown[path])
    opaque = sorted(path for path in artifact['paths'] if unknown[path])
    assert not set(eligible) & set(opaque)
    artifact['context'].update(known_only=eligible, unknown_startup=opaque,
                               classification='unknown-overrides-known; groups are disjoint')
    return artifact


def collect(args):
    repo, out, observations = args.repo.resolve(), args.output.resolve(), args.observations.resolve()
    args.coverage, args.positions = args.coverage.resolve(), args.positions.resolve()
    metadata, coverage, manifest = read_json(args.checkout_metadata), read_json(args.coverage), read_json(args.manifest)
    tests = manifest['tests'] if isinstance(manifest, dict) else manifest
    tests = [{'number': row['number'], 'id': row['id']} for row in tests]
    if (len({row['number'] for row in tests}) != len(tests)
            or len({row['id'] for row in tests}) != len(tests)):
        raise ValueError('manifest identities must be unique')
    if (metadata['base_commit'] != BASE_COMMIT or coverage['base_commit'] != BASE_COMMIT
            or tests != coverage['tests'] or coverage['context']['complete'] is not True):
        raise ValueError('baseline, manifest or coverage mismatch')
    if observations.exists() and any(observations.iterdir()):
        raise ValueError('observation directory must be fresh')
    observations.mkdir(parents=True, exist_ok=True)
    for name, digest in metadata['files'].items():
        if hashlib.sha256((repo/name).read_bytes()).hexdigest() != digest:
            raise ValueError('baseline source fingerprint mismatch: '+name)
    owners = defaultdict(list)
    for test in tests:
        owners[test['id'].split('::')[0]].append(test['number'])
    paths = {name: {'raw_sha256': digest, 'sha256': coverage['paths'][name]['sha256'],
                    'owners': sorted(owners[name])}
             for name, digest in metadata['files'].items() if name.startswith('tests/') and name.endswith('.py')}
    for name, row in paths.items():
        normalized = (repo/name).read_text('utf-8').replace('\r\n','\n')
        if hashlib.sha256(normalized.encode()).hexdigest() != coverage['paths'][name]['sha256']:
            raise ValueError('coverage source fingerprint mismatch: '+name)
    config = {'repo': str(repo), 'observations': str(observations), 'modules': {},
              'test_paths': sorted(paths), 'process_hooks': True,
              'terminal_sites': sites_for(repo, tests),
              'terminal_controller_pid': os.getpid(),
              'terminal_collector_path': str(Path(__file__).resolve()),
              'terminal_support_sha256': hashlib.sha256(
                  (PROJECT_ROOT/'terminal_support.py').read_bytes()).hexdigest()}
    config_path = observations/'config.json'
    write_json(config_path, config)
    os.environ.update(BR_DISPATCH_CONFIG=str(config_path), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                      PYTHONDONTWRITEBYTECODE='1', SKIP_INTEGRATION='0')
    os.chdir(repo)
    sys.path.insert(0,str(repo))
    observer = install(config)
    import pytest
    from _pytest.subtests import SubtestReport
    by_id = {row['id']: row['number'] for row in tests}
    outcomes, extra, collected, collection_errors = {}, {}, [], []
    subtests = Counter()

    class Plugin:
        def pytest_collection_modifyitems(self, items):
            collected.extend(item.nodeid for item in items)
            for item in items:
                observer.note_item(item, by_id.get(item.nodeid))

        def pytest_collectreport(self, report):
            if report.failed:
                collection_errors.append(report.nodeid)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            observer.active = by_id.get(item.nodeid)
            if observer.active is not None:
                os.environ['BR_DISPATCH_TEST'] = str(observer.active)
            try:
                return (yield)
            finally:
                observer.active = None
                os.environ.pop('BR_DISPATCH_TEST', None)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_fixture_setup(self, fixturedef, request):
            if fixturedef.scope == 'function':
                return (yield)
            previous = observer.active
            observer.active = None
            os.environ.pop('BR_DISPATCH_TEST', None)
            try:
                return (yield)
            finally:
                observer.active = previous
                if previous is not None:
                    os.environ['BR_DISPATCH_TEST'] = str(previous)

        def pytest_runtest_logreport(self, report):
            target = outcomes if report.nodeid in by_id else extra
            key = by_id.get(report.nodeid, report.nodeid)
            phases = target.setdefault(key,{})
            if phases.get(report.when) != 'failed':
                phases[report.when] = report.outcome
            if isinstance(report, SubtestReport):
                subtests[report.outcome] += 1

    with tempfile.TemporaryDirectory(prefix='test-source-child-') as temporary:
        module_name = Path(__file__).stem
        if not module_name.isidentifier():
            raise ValueError('collector filename must be an importable module name')
        (Path(temporary)/'sitecustomize.py').write_text('from '+module_name+' import child_install\nchild_install()\n','utf-8')
        os.environ['PYTHONPATH'] = os.pathsep.join([temporary, str(SCRIPT_DIRECTORY),
            str(PROJECT_ROOT), str(repo), os.environ.get('PYTHONPATH','')])
        exit_code = int(pytest.main(['-q','-p','no:cacheprovider','tests'],plugins=[Plugin()]))
    for module_name, attribute in (('multiprocessing.forkserver','_forkserver'),
                                   ('multiprocessing.resource_tracker','_resource_tracker')):
        module = sys.modules.get(module_name)
        service = getattr(module,attribute,None) if module is not None else None
        if service is not None:
            service._stop()
    observer.finish()
    passed, skipped, failed, missing = [], [], [], []
    for test in tests:
        phases = outcomes.get(test['number'],{})
        if 'failed' in phases.values(): failed.append(test['number'])
        elif 'skipped' in phases.values(): skipped.append(test['number'])
        elif phases.get('call') == 'passed' and phases.get('teardown') == 'passed': passed.append(test['number'])
        else: missing.append(test['number'])
    ignored_skipped = sorted(key for key,phases in extra.items() if 'skipped' in phases.values() and 'failed' not in phases.values())
    unknown_tests = sorted(set(extra)-set(ignored_skipped))
    starts = sorted(observations.glob('*.start.json'))
    unfinished, process_errors, children, pids = [], [], {}, set()
    for start in starts:
        pids.add(str(read_json(start)['pid']))
        done = start.with_name(start.name.replace('.start.json','.done.json'))
        if not done.exists():
            unfinished.append(start.name)
            continue
        data = read_json(done)
        children.update(data.get('children',{}))
        process_errors.extend(data.get('process_errors',()))
    unobserved = sorted(set(children)-pids)
    complete = not (exit_code or failed or missing or unknown_tests or collection_errors
                    or unfinished or process_errors or unobserved or subtests['failed'])
    context = {'complete': complete, 'method': METHOD, 'passed': passed, 'skipped': skipped,
               'failed': failed, 'missing': missing, 'unknown_tests': unknown_tests,
               'ignored_skipped_tests': ignored_skipped, 'collection_errors': collection_errors,
               'pytest_exit_code': exit_code, 'subtests': dict(subtests), 'processes': len(starts),
               'unfinished': unfinished, 'process_errors': process_errors, 'unobserved_children': unobserved,
               'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'coverage_sha256': hashlib.sha256(args.coverage.read_bytes()).hexdigest(),
               'positions_sha256': positions_digest(read_json(args.positions)),
               'python': sys.version, 'platform': sys.platform, 'pytest': pytest.__version__,
               'code_fingerprints': [hashlib.sha256(marshal.dumps(code)).hexdigest() for code in observer.codes]}
    # Reuse the already checked process-owner component construction, without
    # changing any line/attribute observations.
    temporary_artifact = {'version':1,'base_commit':BASE_COMMIT,'tests':tests,
        'paths': coverage['paths'], 'attributes':{}, 'context': dict(context)}
    refresh_dependencies(temporary_artifact,coverage,[observations])
    context['shared_pool_groups'] = temporary_artifact['context']['shared_pool_groups']
    artifact = {'version':1,'base_commit':BASE_COMMIT,'tests':tests,'paths':paths,'context':context}
    merge_sources(artifact,[observations])
    attach_terminal(artifact, observations, config)
    write_json(out,artifact)
    print(json.dumps({key: len(value) if key in ('passed','known_only') else value
                      for key,value in context.items() if key != 'code_fingerprints'},sort_keys=True))
    return 0 if complete else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repo','manifest','checkout-metadata','coverage','positions','observations','output'):
        parser.add_argument('--'+name, required=True,type=Path)
    return collect(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
