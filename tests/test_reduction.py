import math
import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from nidaq_to_influx.config import load_config
from nidaq_to_influx.reduction import WindowReducer, sample_bounds, make_statistics_points


class ReductionTests(unittest.TestCase):
    def test_500_to_550_ms_across_reads(self):
        reducer = WindowReducer(["ai0"], {"ai0": {"start": 500, "stop": 550}}, 1000)
        for offset in range(0, 600, 100):
            reducer.add(list(range(offset, offset + 100)))
        result = reducer.result()["ai0"]
        self.assertEqual(set(result), {"mean", "min", "max", "std"})
        self.assertEqual(result["mean"], 524.5)
        self.assertEqual(result["min"], 500)
        self.assertEqual(result["max"], 549)
        self.assertAlmostEqual(result["std"], math.sqrt((50**2 - 1) / 12))

    def test_independent_windows_and_physical_channels(self):
        reducer = WindowReducer(["ai0", "ai16", "ai17"], {
            "ai0": {"start": 1, "stop": 3}, "ai17": {"start": 3, "stop": 4}}, 1000)
        reducer.add([[100, 2], [99, 99], [10, 11]])
        with self.assertRaisesRegex(ValueError, "incomplete"):
            reducer.result()
        reducer.add([[4, 100], [99, 99], [12, 13]])
        self.assertEqual(reducer.result(), {
            "ai0": {"mean": 3, "min": 2, "max": 4, "std": 1},
            "ai17": {"mean": 13, "min": 13, "max": 13, "std": 0}})

    def test_fractional_boundaries(self):
        self.assertEqual(sample_bounds(0.1, 2.1, 1000), (1, 3))
        self.assertEqual(sample_bounds(500, 550, 2000), (1000, 1100))
        self.assertEqual(sample_bounds(0, 0.07, 100000), (0, 7))

    def test_empty_window_and_invalid_reads(self):
        with self.assertRaises(ValueError):
            WindowReducer(["ai0"], {"ai0": {"start": 0.1, "stop": 0.2}}, 1000)
        for chunk in ([], [float("nan")], [float("inf")]):
            reducer = WindowReducer(["ai0"], {"ai0": {"start": 0, "stop": 1}}, 1000)
            with self.assertRaises(ValueError):
                reducer.add(chunk)

    def test_each_trigger_has_fresh_statistics(self):
        for voltage in (1, 20):
            reducer = WindowReducer(["ai0"], {"ai0": {"start": 0, "stop": 2}}, 1000)
            reducer.add([voltage, voltage])
            self.assertEqual(reducer.result()["ai0"]["mean"], voltage)
            with self.assertRaises(ValueError):
                reducer.add([10])

    def test_window_config_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            base = '[channels]\n'
            for window in ('{start = -1, stop = 2}', '{start = 2, stop = 2}',
                           '{start = 0, stop = inf}', '{start = 0.1, stop = 0.2}',
                           '{start = true, stop = 2}', '{start = 0, stop = 2, typo = 1}'):
                path.write_text(base + 'ai0 = ' + window.replace('{', '{label = "Monitor", ', 1))
                with self.assertRaises(ValueError):
                    load_config(path)
            path.write_text(base)
            with self.assertRaisesRegex(ValueError, "At least one"):
                load_config(path)
            path.write_text(base + 'ai0 = {label = "Monitor", start = 500, stop = 550}')
            self.assertEqual(load_config(path).channel_settings, {"ai0": {"label": "Monitor", "start": 500, "stop": 550}})

    def test_null_config_normalizes_and_requires_both_bounds(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text('[channels]\nai0 = {label="Monitor", start="null", stop="null"}')
            config = load_config(path)
            self.assertEqual(config.channel_settings["ai0"], {"label": "Monitor", "start": None, "stop": None})
            for bounds in ('start="null", stop=500', 'start=0, stop="null"',
                           'start="none", stop="none"'):
                path.write_text('[channels]\nai0 = {label="Monitor", ' + bounds + '}')
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_whole_cycle_waits_for_close_and_keeps_all_reads(self):
        reducer = WindowReducer(["ai0"], {"ai0": {"start": None, "stop": None}}, 1000)
        reducer.add([1, 2])
        reducer.add([3, 4, 5])
        self.assertFalse(reducer.complete)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            reducer.result()
        reducer.close()
        self.assertEqual(reducer.result()["ai0"], {"mean": 3, "min": 1, "max": 5, "std": math.sqrt(2)})
        with self.assertRaises(ValueError):
            reducer.add([6])

    def test_empty_whole_cycle_does_not_emit(self):
        reducer = WindowReducer(["ai0"], {"ai0": {"start": None, "stop": None}}, 1000)
        reducer.close()
        self.assertFalse(reducer.complete)
        with self.assertRaises(ValueError):
            reducer.result()

    def test_influx_contains_only_four_statistics(self):
        class Point:
            def __init__(self, measurement):
                self.fields = {}
                self.tags = {}

            def tag(self, name, value):
                self.tags[name] = value
                return self

            def field(self, name, value):
                self.fields[name] = value
                return self

            def time(self, timestamp, precision):
                self.timestamp = timestamp
                return self

        values = {"mean": 3, "min": 2, "max": 4, "std": 1}
        timestamp = datetime.datetime.now(datetime.timezone.utc)
        points = make_statistics_points(
            SimpleNamespace(measurement="daq", channel_settings={"ai17": {"label": "Monitor", "start": 0, "stop": 2}}),
            timestamp, {"ai17": values}, Point, "ns")
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].fields, values)
        self.assertEqual(points[0].tags, {"channel": "ai17", "label": "Monitor"})
        self.assertEqual(points[0].timestamp, timestamp)


if __name__ == "__main__":
    unittest.main()
