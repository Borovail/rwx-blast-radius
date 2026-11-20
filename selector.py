"""Choose tests and emit their worker assignments as competition JSONL."""

import json
import sys
from pathlib import Path

from scheduler import schedule_tests

DATA = Path("/data")


def main() -> None:
    data_dir = Path(sys.argv[1]) if len(sys.argv) == 2 else DATA
    tests = json.loads((data_dir / "tests.json").read_text("utf-8"))["tests"]
    workers = schedule_tests(tests)
    for patch in sorted((data_dir / "commits").glob("*.patch")):
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(f"selected {len(tests)} tests on every commit", file=sys.stderr)


if __name__ == "__main__":
    main()
