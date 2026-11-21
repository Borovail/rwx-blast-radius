"""Join base operand facts from isolated tests and the shared pytest process.

The output preserves the previously shipped isolated witnesses. New numbered
tests qualify at a comparison only when all their observations also occurred
in the ordinary complete suite. The selector receives the raw suite facts.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from m2_body import BASE_COMMIT, SUITE_POLICY
from tools.refine_coverage import read_json, write_json


def observed_facts(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 64:
        return set()
    facts = set()
    for item in items:
        if (not isinstance(item, list) or len(item) != 4
                or type(item[2]) is not bool or type(item[3]) is not bool):
            return set()
        for atom in item[:2]:
            if (not isinstance(atom, list) or not atom
                    or atom[0] not in ("identity", "scalar", "scalar_subclass")):
                return set()
        facts.add(json.dumps(item, separators=(",", ":"), ensure_ascii=True))
    return facts


def join_witnesses(original, isolated, suite, repeat):
    if (any(item.get("method") != "complete-suite-base-identity-operands-v1" for item in (suite, repeat))
            or isolated.get("method") != "isolated-base-identity-operands-certified-types-v1"):
        raise ValueError("unexpected collection method")
    for artifact in (original, isolated, suite, repeat):
        if (type(artifact.get("version")) is not int or artifact["version"] != 1
                or artifact.get("complete") is not True or artifact.get("base_commit") != BASE_COMMIT):
            raise ValueError("incomplete or incompatible baseline observations")
    for key in ("tests", "base_fingerprint", "coverage_sha256"):
        if not original.get(key) or any(item.get(key) != original[key] for item in (isolated, suite, repeat)):
            raise ValueError(f"baseline witnesses disagree on {key}")
    manifest = {row["number"] for row in original["tests"]}
    if (len(manifest) != len(original["tests"]) or any(type(n) is not int for n in manifest)
            or any(set(item["observed_tests"]) != manifest for item in (suite, repeat))):
        raise ValueError("suite did not attest every numbered test")
    observed = set(isolated["observed_tests"])
    if not observed <= manifest or any(type(n) is not int for n in observed):
        raise ValueError("invalid isolated test number")
    if "suite_policy" in original:
        raise ValueError("use the original isolated witness map as the merge base")
    output = copy.deepcopy(original)
    for path, suite_file in suite["files"].items():
        isolated_file = isolated["files"].get(path)
        repeat_file = repeat["files"].get(path)
        if not isolated_file or not repeat_file:
            continue
        if not isolated_file["sha256"] == suite_file["sha256"] == repeat_file["sha256"]:
            raise ValueError(f"comparison source mismatch in {path}")
        for position, suite_site in suite_file["sites"].items():
            isolated_site = isolated_file["sites"].get(position)
            if not isolated_site or suite_site["operator"] not in ("Is", "IsNot"):
                continue
            if isolated_site["ast"] != suite_site["ast"]:
                raise ValueError(f"comparison AST mismatch in {path} at {position}")
            if repeat_file["sites"].get(position) != suite_site:
                continue
            normal = observed_facts(suite_site["observations"])
            if not normal:
                continue
            numbered = []
            for number, items in isolated_site["tests"].items():
                test = int(number)
                if str(test) != number or test not in observed:
                    raise ValueError("isolated site contains an unattested test")
                facts = observed_facts(items)
                if facts and facts <= normal:
                    numbered.append(test)
            if not numbered:
                continue
            out_file = output["files"].setdefault(path, {"sha256": suite_file["sha256"], "sites": {}})
            out_site = out_file["sites"].setdefault(position, {"ast": suite_site["ast"], "tests": {}})
            if out_file["sha256"] != suite_file["sha256"] or out_site["ast"] != suite_site["ast"]:
                raise ValueError("existing witness source or AST mismatch")
            out_site["suite"] = {"numbers": sorted(numbered), "observations": suite_site["observations"]}
    output["suite_policy"] = SUITE_POLICY
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--isolated", required=True, type=Path)
    parser.add_argument("--suite", required=True, type=Path)
    parser.add_argument("--repeat", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = join_witnesses(read_json(args.original), read_json(args.isolated),
                            read_json(args.suite), read_json(args.repeat))
    write_json(args.output, result)


if __name__ == "__main__":
    main()
