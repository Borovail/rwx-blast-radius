"""Choose tests and emit their worker assignments as competition JSONL."""

import argparse
import gzip
import json
from pathlib import Path

from impact import ImpactSelector
from scheduler import schedule_tests

APP = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path, nargs="?", default=Path("/data"))
    parser.add_argument("--base", type=Path, default=APP / "base")
    parser.add_argument("--coverage", type=Path, default=APP / "maps" / "coverage.json.gz")
    args = parser.parse_args()
    data_dir = args.data_dir
    tests = json.loads((data_dir / "tests.json").read_text("utf-8"))["tests"]
    with gzip.open(args.coverage, "rt", encoding="utf-8") as stream:
        coverage = json.load(stream)
    selector = ImpactSelector(tests, coverage, args.base)
    by_number = {test["number"]: test for test in tests}
    schedules = {}
    for patch in sorted((data_dir / "commits").glob("*.patch")):
        try:
            numbers = tuple(selector.select(patch.read_text("utf-8")))
        except UnicodeDecodeError:
            numbers = tuple(sorted(by_number))
        if numbers not in schedules:
            schedules[numbers] = schedule_tests(by_number[n] for n in numbers)
        print(json.dumps({"commit": patch.stem, "workers": schedules[numbers]}, separators=(",", ":")))


if __name__ == "__main__":
    main()
