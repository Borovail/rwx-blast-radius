import unittest

from scheduler import schedule_tests


def rows(*seconds):
    return [dict(number=index + 1, seconds=value) for index, value in enumerate(seconds)]


class SchedulerTests(unittest.TestCase):
    def test_empty_selection(self):
        self.assertEqual(schedule_tests([]), [])

    def test_single_test(self):
        self.assertEqual(schedule_tests(rows(50)), [[1]])

    def test_overhead_favors_single_machine_for_short_tests(self):
        self.assertEqual(schedule_tests(rows(0.5, 0.4, 0.3)), [[1, 2, 3]])

    def test_equal_cost_favors_fewer_machines(self):
        self.assertEqual(schedule_tests(rows(2, 2)), [[1, 2]])

    def test_long_jobs_use_multiple_machines(self):
        self.assertEqual(schedule_tests(rows(20, 20, 20)), [[1], [2], [3]])

    def test_lpt_balances_unequal_durations(self):
        self.assertEqual(schedule_tests(rows(4, 3, 2, 1)), [[1, 4], [2, 3]])

    def test_input_order_does_not_change_tie_breaks(self):
        tests = rows(10, 10, 10, 10)
        self.assertEqual(schedule_tests(reversed(tests)), [[1], [2], [3], [4]])

    def test_rounding_is_per_test(self):
        # Each duration rounds to 2000 ms, tying one machine with two.
        self.assertEqual(schedule_tests(rows(2.0004, 2.0004)), [[1, 2]])
        self.assertEqual(schedule_tests(rows(2.0006, 2.0006)), [[1], [2]])

    def test_round_matches_official_scorer_at_half_millisecond(self):
        # The scorer charges 2063 ms for the second job, scheduling it first.
        self.assertEqual(schedule_tests(rows(2.0624, 2.0625, 0)), [[2], [1, 3]])

    def test_eight_machine_limit_and_no_lost_tests(self):
        result = schedule_tests(rows(*([100] * 16)))
        self.assertEqual(len(result), 8)
        self.assertEqual(sorted(number for machine in result for number in machine), list(range(1, 17)))


if __name__ == "__main__":
    unittest.main()
