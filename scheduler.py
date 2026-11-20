"""Deterministic scheduling of selected tests using longest processing time first."""

from collections.abc import Iterable, Mapping
from math import floor


def schedule_tests(tests: Iterable[Mapping[str, int | float]]) -> list[list[int]]:
    """Minimize LPT makespan plus 2,000 ms per machine, using up to eight.

    Each input row contains a unique ``number`` and a duration in ``seconds``.
    Durations are rounded independently to integer milliseconds before scheduling.
    Test-number and machine-index tie breaks make the result independent of input
    order. Equal objective values favor fewer machines.
    """
    jobs = sorted(
        ((row["number"], floor(row["seconds"] * 1000 + 0.5)) for row in tests),
        key=lambda job: (-job[1], job[0]),
    )
    if not jobs:
        return []

    best_schedule: list[list[int]] = []
    best_cost: int | None = None
    for count in range(1, min(len(jobs), 8) + 1):
        machines: list[list[int]] = [[] for _ in range(count)]
        loads = [0] * count
        for number, milliseconds in jobs:
            machine = loads.index(min(loads))
            machines[machine].append(number)
            loads[machine] += milliseconds
        cost = max(loads) + 2000 * count
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_schedule = machines
    return best_schedule
