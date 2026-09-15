from dataclasses import replace
import unittest

from nidaq_to_influx.config import load_config
from nidaq_to_influx.cycles import CycleReducer


def config_for_window(start=0, stop=2):
    return replace(load_config(), channel_settings={"ai0": {"label": "Monitor", "start": start, "stop": stop}})


class CycleTests(unittest.TestCase):
    def test_whole_cycles_of_different_lengths_across_read_partitions(self):
        values = [99, 2, 4, 6, 10, 20, 5, 100]
        counts = [0, 1, 1, 1, 2, 2, 3, 4]
        for size in (1, 2, 3, 8):
            cycles = CycleReducer(config_for_window(None, None))
            result = []
            for start in range(0, len(values), size):
                result.extend(cycles.add(values[start:start+size], counts[start:start+size]))
            self.assertEqual([start for start, _ in result], [1, 4, 6])
            self.assertEqual([stats["ai0"]["mean"] for _, stats in result], [4, 15, 5])
            self.assertEqual(result[2][1]["ai0"]["std"], 0)
            cycles.discard_open_cycle()
            self.assertEqual(cycles.cycles_completed, 3)

    def test_mixed_whole_cycle_and_fixed_windows(self):
        config = replace(load_config(), channel_settings={
            "ai0": {"label": "Whole", "start": None, "stop": None},
            "ai1": {"label": "Fixed", "start": 1, "stop": 3}})
        cycles = CycleReducer(config)
        self.assertEqual(cycles.add([[1, 2, 3], [10, 20, 30]], [1, 1, 1]), [])
        # Continue the whole-cycle channel after the fixed window has completed.
        result = cycles.add([[4, 5, 100], [999, 999, 999]], [1, 1, 2])
        self.assertEqual(result[0][1]["ai0"]["mean"], 3)
        self.assertEqual(result[0][1]["ai1"], {"mean": 25, "min": 20, "max": 30, "std": 5})
        with self.assertLogs("nidaq_to_influx.cycles", level="WARNING"):
            # Skip the next cycle if its fixed window is incomplete, even with whole-cycle data.
            self.assertEqual(cycles.add([[1], [1]], [3]), [])

    def test_next_edge_closes_cycle_and_belongs_to_new_cycle(self):
        cycles = CycleReducer(config_for_window())
        self.assertEqual(cycles.add([99, 2, 4, 99], [0, 1, 1, 1]), [])
        # Windows finished, but still wait for closing edge.
        self.assertEqual(cycles.add([99, 99], [1, 1]), [])
        result = cycles.add([10, 20, 999], [2, 2, 3])
        self.assertEqual([start for start, _ in result], [1, 6])
        self.assertEqual(result[0][1]["ai0"], {"mean": 3, "min": 2, "max": 4, "std": 1})
        self.assertEqual(result[1][1]["ai0"]["mean"], 15)
        self.assertEqual(cycles.cycles_completed, 2)

    def test_boundaries_at_read_edges_and_float_counts(self):
        cycles = CycleReducer(config_for_window())
        self.assertEqual(cycles.add([2, 4], [1.0, 1.0]), [])
        result = cycles.add([10, 20], [2.0, 2.0])
        self.assertEqual(result[0][0], 0)
        self.assertEqual(result[0][1]["ai0"]["mean"], 3)

    def test_500_to_550_ms_window_waits_for_next_trigger(self):
        cycles = CycleReducer(config_for_window(500, 550))
        for start in range(0, 700, 100):
            self.assertEqual(cycles.add(list(range(start, start + 100)), [1]*100), [])
        result = cycles.add([700], [2])
        self.assertEqual(result[0][1]["ai0"]["mean"], 524.5)
        self.assertEqual(result[0][1]["ai0"]["max"], 549)

    def test_short_cycle_is_skipped_and_next_recovers(self):
        cycles = CycleReducer(config_for_window(1, 3))
        with self.assertLogs("nidaq_to_influx.cycles", level="WARNING"):
            self.assertEqual(cycles.add([2, 4, 10, 20], [1, 1, 2, 2]), [])
        result = cycles.add([30, 40], [2, 3])
        self.assertEqual(result[0][1]["ai0"]["mean"], 25)
        self.assertEqual(cycles.cycles_skipped, 1)

    def test_open_cycle_discarded_even_when_windows_complete(self):
        cycles = CycleReducer(config_for_window())
        self.assertEqual(cycles.add([2, 4], [1, 1]), [])
        cycles.discard_open_cycle()
        self.assertIsNone(cycles.reducer)
        self.assertEqual(cycles.cycles_completed, 0)

    def test_counter_rollover_is_one_edge(self):
        cycles = CycleReducer(config_for_window())
        cycles.last_count = 2**32 - 2
        cycles.add([2, 4], [2**32 - 1]*2)
        result = cycles.add([10], [0])
        self.assertEqual(result[0][1]["ai0"]["mean"], 3)

    def test_multiple_edges_between_scans_and_invalid_counts_fail(self):
        for counts in ([0, 2], [0, -1], [0, 1.5], [0, float("nan")], [0, 2**32]):
            cycles = CycleReducer(config_for_window())
            with self.assertRaises(ValueError):
                cycles.add([2, 4], counts)
        cycles = CycleReducer(config_for_window())
        with self.assertRaisesRegex(ValueError, "matching"):
            cycles.add([2], [0, 1])

    def test_results_independent_of_read_chunking(self):
        # Several variable-length cycles, including a short one, across all read partitions.
        values = list(range(30))
        counts = [0]*3 + [1]*7 + [2]*4 + [3]*9 + [4]*2 + [5]*5
        baseline = None
        for size in (1, 2, 3, 5, 7, 30):
            cycles = CycleReducer(config_for_window(1, 4))
            result = []
            with self.assertLogs("nidaq_to_influx.cycles", level="WARNING"):
                for start in range(0, len(values), size):
                    result.extend(cycles.add(values[start:start+size], counts[start:start+size]))
            if baseline is None:
                baseline = result
            self.assertEqual(result, baseline)
            self.assertEqual(cycles.cycles_skipped, 1)


if __name__ == "__main__":
    unittest.main()
