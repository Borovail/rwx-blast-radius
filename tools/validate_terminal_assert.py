#!/usr/bin/env python3
"""Independently validate a complete terminal-assert map against pinned assets."""

from pathlib import Path
import argparse
import ast
import gzip
import hashlib
import json
import sys


SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parent.parent if SCRIPT.parent.name == 'tools' else SCRIPT.parent
sys.path.insert(0, str(ROOT))
from terminal_assert import TerminalAssertions
from terminal_support import sites_for


def validate(map_path, coverage, manifest, repo, collector, support):
    tests = json.loads(Path(manifest).read_bytes())['tests']
    data = json.loads(gzip.decompress(Path(map_path).read_bytes()))
    runtime = TerminalAssertions.from_files(map_path, coverage, tests)
    if runtime is None or not runtime.ready:
        raise ValueError('runtime rejected map bindings or proof shape')
    proof = data['terminal']
    if proof['collector_sha256'] != hashlib.sha256(Path(collector).read_bytes()).hexdigest():
        raise ValueError('collector fingerprint mismatch')
    if proof['support_sha256'] != hashlib.sha256(Path(support).read_bytes()).hexdigest():
        raise ValueError('observer fingerprint mismatch')
    baseline_sites = sites_for(repo, tests)
    for path, row in data['paths'].items():
        source = (Path(repo) / path).read_text('utf-8')
        if hashlib.sha256(source.encode()).hexdigest() != row['sha256']:
            raise ValueError('source fingerprint mismatch: ' + path)
        if hashlib.sha256((Path(repo) / path).read_bytes()).hexdigest() != row['raw_sha256']:
            raise ValueError('raw source fingerprint mismatch: ' + path)
    names = {'str': str, 'bytes': bytes, 'int': int, 'float': float, 'bool': bool, 'NoneType': type(None)}
    for nodeid, site in proof['sites'].items():
        baseline = baseline_sites.get(nodeid)
        if baseline is None or any(site.get(key) != value for key, value in baseline.items()):
            raise ValueError('method/code/site mismatch: ' + nodeid)
        tree = ast.parse((Path(repo) / site['path']).read_text('utf-8'))
        cls_name, method_name = site['qualname'].split('.')
        cls = next(node for node in tree.body if type(node) is ast.ClassDef and node.name == cls_name)
        method = next(node for node in cls.body if type(node) is ast.FunctionDef and node.name == method_name)
        expected = method.body[-1].value.args[1].value
        for row in site['values']:
            if len(row) != 4 or any(type(value) is not str for value in row):
                raise ValueError('invalid typed operand row')
            first_type, first_repr, second_type, second_repr = row
            first, second = ast.literal_eval(first_repr), ast.literal_eval(second_repr)
            if (first_type not in names or second_type not in names
                    or type(first) is not names[first_type] or type(second) is not names[second_type]
                    or type(second) is not type(expected) or repr(second) != repr(expected)
                    or not first == second):
                raise ValueError('typed operand mismatch: ' + nodeid)
    return {'complete': True, 'certified_sites': len(proof['sites']),
            'rejected_sites': len(proof['rejected']), 'passed': len(data['context']['passed']),
            'subtests': data['context']['subtests'], 'processes': data['context']['processes'],
            'source_hashes_verified': len(data['paths']),
            'map_sha256': hashlib.sha256(Path(map_path).read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('map', 'coverage', 'manifest', 'repo'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--collector', type=Path, default=ROOT / 'tools/build_terminal_assert.py')
    parser.add_argument('--support', type=Path, default=ROOT / 'terminal_support.py')
    args = parser.parse_args()
    print(json.dumps(validate(args.map, args.coverage, args.manifest, args.repo,
                              args.collector, args.support), sort_keys=True))


if __name__ == '__main__':
    main()
