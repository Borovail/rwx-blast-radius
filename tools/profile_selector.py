"""Measure the entry point's Linux process CPU, including waited-for children."""

import resource
import subprocess
import sys


if __name__ == "__main__":
    subprocess.run([sys.executable, "/app/selector.py"], check=True)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    # The parent shell captures only stdout from this runner as JSONL.
    print(f"Selector CPU seconds: {usage.ru_utime + usage.ru_stime:.6f}", file=sys.stderr)
