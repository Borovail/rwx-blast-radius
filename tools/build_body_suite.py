#!/usr/bin/env python3
"""Collect factual Is/IsNot operands from an ordinary complete pinned-base suite.

Run on pinned Linux/Python 3.13 with locked dependencies. No candidate patches,
predictions or outcome labels are inputs. Any invalid suite/process attestation
prevents output; unsupported operand occurrences remain explicit null evidence.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from refine_coverage import BASE_COMMIT, fingerprint, read_json, write_json
from build_body_process import validate_processes
from m2_body import comparison_site, observed_outcome


RUNNER = '''
import json, os, pytest
class Plugin:
    def __init__(self):
        self.collected, self.phases, self.subtests, self.collection_errors = [], [], [], []
    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]
    def pytest_collectreport(self, report):
        if report.failed: self.collection_errors.append(report.nodeid)
    def pytest_runtest_logreport(self, report):
        target = self.subtests if hasattr(report, 'context') else self.phases
        target.append([report.nodeid, report.when, report.outcome])
p = Plugin()
result = pytest.main(['tests', '-q', '-p', 'no:cacheprovider'], plugins=[p])
with open(os.environ['BLAST_PYTEST_REPORT'], 'w') as stream:
    json.dump(dict(exit_code=int(result), collected=p.collected, phases=p.phases,
                   subtests=p.subtests, collection_errors=p.collection_errors), stream)
raise SystemExit(result)
'''


def test_identity(rows):
    """Reject duplicate numbers and IDs instead of silently collapsing them."""
    if (not isinstance(rows, list) or not rows
            or any(type(row['number']) is not int or row['number'] < 0
                   or not isinstance(row['id'], str) or not row['id'] for row in rows)):
        raise ValueError('invalid numbered manifest')
    pairs = {row['number']: row['id'] for row in rows}
    if len(pairs) != len(rows) or len(set(pairs.values())) != len(rows):
        raise ValueError('duplicate numbered manifest entry')
    return pairs


def validate_pytest(report, tests):
    expected = set(tests.values())
    collected = report['collected']
    if (report['exit_code'] != 0 or report['collection_errors']
            or len(collected) != len(set(collected)) or not expected <= set(collected)):
        raise ValueError('incomplete/failed suite collection')
    phases = {nodeid: [] for nodeid in collected}
    for nodeid, phase, outcome in report['phases']:
        if nodeid not in phases:
            raise ValueError('uncollected pytest report')
        phases[nodeid].append((phase, outcome))
    passed = Counter([('setup', 'passed'), ('call', 'passed'), ('teardown', 'passed')])
    skipped = [Counter([('setup', 'skipped'), ('teardown', 'passed')]),
               Counter([('setup', 'passed'), ('call', 'skipped'), ('teardown', 'passed')])]
    for nodeid, values in phases.items():
        allowed = [passed] if nodeid in expected else skipped
        if Counter(values) not in allowed:
            raise ValueError('incomplete/failed/skipped numbered test or non-skipped extra')
    for nodeid, phase, outcome in report['subtests']:
        if nodeid not in expected or phase != 'call' or outcome != 'passed':
            raise ValueError('unexpected/failed/skipped subtest')
    return len(report['subtests']), sorted(set(collected) - expected)


def validate_graph(run, metadata):
    """Require one rooted process tree, including every observed cleanup process."""
    for suffix in ('start', 'done', 'body'):
        for path in run.glob(f'*.{suffix}.json'):
            pid = int(path.name.split('.')[0])
            if pid <= 0 or path.name != f'{pid}.{suffix}.json':
                raise ValueError('invalid/duplicate process filename')
    starts = {int(p.name.split('.')[0]): read_json(p) for p in run.glob('*.start.json')}
    done = {int(p.name.split('.')[0]): read_json(p) for p in run.glob('*.done.json')}
    root = metadata['pid']
    parents = {}
    for pid, record in done.items():
        for child, kind in record['children'].items():
            child_pid = int(child)
            if (str(child_pid) != child or child_pid <= 0 or child_pid == root
                    or child_pid in parents or kind not in
                    ('fork', 'posix_spawn', 'posix_spawnp', 'fork_exec', 'Popen')):
                raise ValueError('invalid process parent graph')
            parents[child_pid] = pid
    if set(parents) != set(starts) - {root} or set(starts) != set(done):
        raise ValueError('missing/unaccounted process cleanup')
    for child, parent in parents.items():
        if starts[child]['ppid'] != parent:
            raise ValueError('child parent attestation mismatch')
        seen = set()
        while child != root:
            if child in seen or child not in parents:
                raise ValueError('disconnected/cyclic process graph')
            seen.add(child)
            child = parents[child]


def validate_files(files, repo, coverage):
    """Check coordinates and AST against actual pinned source, not peer reports."""
    for path, record in files.items():
        if record['sha256'] != coverage['paths'][path]['sha256']:
            raise ValueError('source hash mismatch')
        source = (repo / path).read_text(encoding='utf-8').replace('\r\n', '\n')
        sites = {comparison_site(node): node for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.Compare) and len(node.ops) == 1
                 and isinstance(node.ops[0], (ast.Is, ast.IsNot))}
        for site, value in record['sites'].items():
            node = sites.get(site)
            if (node is None or value['ast'] != ast.dump(node)
                    or value['operator'] != type(node.ops[0]).__name__):
                raise ValueError('comparison coordinates/AST/operator mismatch')
            observations = value['observations']
            if not isinstance(observations, list) or not observations:
                raise ValueError('unexecuted comparison is not evidence')
            if None in observations or len(observations) > 64:
                value['observations'] = [None]
                continue
            unique = set()
            for item in observations:
                if (not isinstance(item, list) or len(item) != 4 or type(item[3]) is not bool
                        or observed_outcome(item, value['operator']) is not item[3]):
                    raise ValueError('invalid raw operand observation')
                unique.add(json.dumps(item, sort_keys=True, separators=(',', ':')))
            value['observations'] = [json.loads(raw) for raw in sorted(unique)]
    return files


def validate(run, tests, stamp, coverage, coverage_digest, repo):
    metadata = read_json(run / 'run.json')
    if (metadata['version'] != 1 or metadata['base_commit'] != BASE_COMMIT
            or metadata['base_fingerprint'] != stamp
            or metadata['after_fingerprint'] != stamp
            or metadata['coverage_sha256'] != coverage_digest
            or metadata['tests'] != tests or metadata['platform'] != 'linux'
            or metadata['python_version'] != [3, 13]
            or metadata['repo'] != str(repo) + os.sep
            or metadata['timed_out'] or metadata['returncode'] != 0
            or metadata['cleanup_killed']):
        raise ValueError('suite lacks clean matching pinned-base attestation')
    subtests, extras = validate_pytest(read_json(run / 'pytest.json'), test_identity(tests))
    validate_graph(run, metadata)
    files, processes, _ = validate_processes(run, metadata, coverage)
    return validate_files(files, repo, coverage), processes, subtests, extras, metadata


def observe(args, tests, stamp, coverage, coverage_digest):
    run = args.observations / uuid.uuid4().hex
    run.mkdir(parents=True)
    trace = ROOT / 'tools' / 'trace_worker' / 'sitecustomize.py'
    with tempfile.TemporaryDirectory(prefix='blast-suite-hooks-') as hooks:
        Path(hooks, 'sitecustomize.py').write_text(
            'from pathlib import Path\n'
            f'exec(compile(Path({str(trace)!r}).read_bytes(), {str(trace)!r}, "exec"), '
            '{"__name__": "blast_suite_trace"})\n'
            'import build_body_worker as worker\n'
            'from suite_snapshot import snapshot\n'
            'worker.install(snapshotter=snapshot)\n')
        env = os.environ.copy()
        env.update(PYTHONPATH=os.pathsep.join([hooks, str(ROOT / 'tools'), str(ROOT),
                   str(args.repo), env.get('PYTHONPATH', '')]),
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1',
                   PYTHON_CPU_COUNT=str(args.workers), OPENBLAS_NUM_THREADS='1',
                   OMP_NUM_THREADS='1', SKIP_INTEGRATION='0',
                   BLAST_TRACE_DIR=str(run), BLAST_TRACE_REPO=str(args.repo),
                   BLAST_TRACE_TOKEN=run.name, BLAST_BODY_DIR=str(run),
                   BLAST_BODY_OPERATORS='Is,IsNot', BLAST_PYTEST_REPORT=str(run / 'pytest.json'))
        started = time.monotonic()
        timed_out, cleanup_killed = False, False
        with open(run / 'pytest.log', 'wb') as log:
            process = subprocess.Popen([sys.executable, '-c', RUNNER], cwd=args.repo,
                                       env=env, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                returncode = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                returncode = process.wait()
        # Keep the hook directory alive while resource trackers finish. Compare
        # the graph too: matching existing report sets may still omit a child.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                validate_graph(run, {'pid': process.pid})
                starts = {p.name.split('.')[0] for p in run.glob('*.start.json')}
                bodies = {p.name.split('.')[0] for p in run.glob('*.body.json')}
                if starts and starts == bodies:
                    break
            except (OSError, ValueError, KeyError, TypeError):
                pass
            time.sleep(0.05)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                cleanup_killed = True
            except ProcessLookupError:
                pass
    after = fingerprint(args.repo, coverage)
    write_json(run / 'run.json', dict(version=1, base_commit=BASE_COMMIT,
               base_fingerprint=stamp, after_fingerprint=after, coverage_sha256=coverage_digest,
               tests=tests, pid=process.pid, token=run.name, returncode=returncode,
               timed_out=timed_out, cleanup_killed=cleanup_killed,
               wall_seconds=round(time.monotonic() - started, 3), repo=str(args.repo) + os.sep,
               platform=sys.platform, python_version=list(sys.version_info[:2])))
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repo', 'manifest', 'coverage', 'observations', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--run', type=Path, help='validate a previously collected run')
    parser.add_argument('--timeout', type=float, default=1800)
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    if sys.platform != 'linux' or sys.version_info[:2] != (3, 13):
        parser.error('use pinned Linux Python 3.13')
    args.repo, args.observations = args.repo.resolve(), args.observations.resolve()
    if (args.output.resolve().is_relative_to(args.repo)
            or args.observations.is_relative_to(args.repo)
            or args.output.resolve() == args.coverage.resolve()):
        parser.error('output/observations must be outside pinned base and input coverage')
    coverage = read_json(args.coverage)
    tests = read_json(args.manifest)['tests']
    if (coverage['version'] != 1 or coverage['base_commit'] != BASE_COMMIT
            or test_identity(coverage['tests']) != test_identity(tests)):
        raise ValueError('unsupported base coverage/manifest')
    stamp = fingerprint(args.repo, coverage)
    digest = hashlib.sha256(args.coverage.read_bytes()).hexdigest()
    run = args.run.resolve() if args.run else observe(args, tests, stamp, coverage, digest)
    files, processes, subtests, extras, metadata = validate(
        run, tests, stamp, coverage, digest, args.repo)
    if fingerprint(args.repo, coverage) != stamp:
        raise ValueError('base changed during validation')
    write_json(args.output, dict(version=1, complete=True, base_commit=BASE_COMMIT,
               base_fingerprint=stamp, coverage_sha256=digest, tests=coverage['tests'], files=files,
               method='complete-suite-base-identity-operands-v1',
               observed_tests=sorted(test_identity(tests))))
    report = dict(run=str(run), processes=processes, subtests=subtests,
                  skipped_extra_ids=extras, wall_seconds=metadata['wall_seconds'])
    write_json(args.output.with_suffix(args.output.suffix + '.report.json'), report)
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
