import contextlib
from dataclasses import replace
import io
import os
import queue
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from nidaq_to_influx.cli import main
from nidaq_to_influx.config import load_config
from nidaq_to_influx.logger import run_logger


class Point:
    def __init__(self, measurement):
        self.measurement = measurement
        self.tags = {}
        self.fields = {}

    def tag(self, key, value):
        self.tags[key] = value
        return self

    def field(self, key, value):
        self.fields[key] = value
        return self

    def time(self, timestamp, precision):
        self.timestamp = timestamp
        return self


class LoggerTests(unittest.TestCase):
    def test_config_overrides_and_invalid_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.toml"
            path.write_text('[channels]\nai17 = {label = "Monitor", start = 500, stop = 550}\n'
                            '[trigger]\nterminal = "/Dev2/PFI3"\ncounter = "ctr2"\n')
            config = load_config(path)
            self.assertEqual(config.trigger_source, "/Dev2/PFI3")
            self.assertEqual(config.trigger_counter, "ctr2")
            self.assertEqual(load_config().trigger_source, "/Dev2/PFI0")
            for invalid in ('[daq]\nchunk_size = 0', '[daq]\nchannels = ["ai0", "ai0"]',
                            '[influx]\ntoken = "not-allowed"', '[daq]\nsample_rate = nan',
                            '[trigger]\nterminal = "/OtherDevice/PFI0"', '[trigger]\nterminal = "PFI16"',
                            '[trigger]\ncounter = "ctr4"', '[trigger]\nterminal = 0',
                            '[channels]\nai0 = {label = "", start = 0, stop = 1}',
                            '[channels]\nai0 = {label = "Monitor", start = 0}',
                            '[channels]\nai0 = {start = 0, stop = 1}',
                            '[labels]\nai0 = "Monitor"', '[windows]\nai0 = {start = 0, stop = 1}'):
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_cli_check_without_token_and_missing_token_error(self):
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--check-config"]), 0)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main([])
            self.assertEqual(error.exception.code, 2)

    def run_simulation(self, write_failure=False, read_failure=False, counter_failure=False,
                       single_channel=False, trigger=True, actual_rate=1000, queue_full=False):
        stop = threading.Event()
        analog, counter = MagicMock(), MagicMock()
        analog.__enter__.return_value = analog
        counter.__enter__.return_value = counter
        analog.timing.samp_clk_rate = actual_rate
        config = load_config()
        if single_channel:
            config = replace(config, channel_settings={"ai17": config.channel_settings["ai17"]})
        config = replace(config, trigger_min_interval_ms=0, channel_settings={ch: {**entry, "start": 1, "stop": 3} for ch, entry in config.channel_settings.items()}, chunk_size=4)
        order = []
        counter.start.side_effect = lambda: order.append("counter")
        analog.start.side_effect = lambda: order.append("analog")
        reads = 0

        def read_analog(**kwargs):
            nonlocal reads
            if read_failure:
                raise RuntimeError("DAQ disconnected")
            reads += 1
            # New edges at sample 1 and 7: previous cycle closes in the final read.
            # A stop signal during that read must not lose the completed cycle.
            if reads == 2:
                stop.set()
            values = [[100 * i + (reads - 1) * 4 + j for j in range(4)]
                      for i in range(len(config.channels))]
            return values[0] if single_channel else values

        analog.read.side_effect = read_analog
        if counter_failure:
            counter.read.side_effect = RuntimeError("Counter overflow")
        else:
            counter.read.side_effect = [[0, 1, 1, 1], [1, 1, 1, 2]] if trigger else [[0]*4, [0]*4]
        writer = MagicMock()
        writer.__enter__.return_value = writer
        if write_failure:
            writer.write.side_effect = RuntimeError("write failed")
        client = MagicMock()
        client.__enter__.return_value = client
        client.write_api.return_value = writer
        modules = {
            "nidaqmx": types.SimpleNamespace(Task=MagicMock(side_effect=[analog, counter])),
            "nidaqmx.constants": types.SimpleNamespace(
                AcquisitionType=types.SimpleNamespace(CONTINUOUS=1),
                TerminalConfiguration=types.SimpleNamespace(DEFAULT=0),
                Edge=types.SimpleNamespace(RISING="rising"),
                CountDirection=types.SimpleNamespace(COUNT_UP="up"),
                TaskMode=types.SimpleNamespace(TASK_COMMIT="commit"),
                TriggerType=types.SimpleNamespace(DIGITAL_EDGE="digital")),
            "influxdb_client": types.SimpleNamespace(
                InfluxDBClient=MagicMock(return_value=client), Point=Point,
                WritePrecision=types.SimpleNamespace(NS="ns")),
            "influxdb_client.client.write_api": types.SimpleNamespace(SYNCHRONOUS=1),
        }
        real_queue = queue.Queue()
        if queue_full:
            real_queue.put_nowait = MagicMock(side_effect=queue.Full)
        with patch.dict("sys.modules", modules), patch("nidaq_to_influx.logger.queue.Queue", return_value=real_queue):
            if write_failure or read_failure or counter_failure:
                with self.assertRaises(RuntimeError), patch("nidaq_to_influx.logger.log.error"):
                    run_logger(config, "test-token", stop)
            else:
                run_logger(config, "test-token", stop)
                if not trigger or queue_full:
                    writer.write.assert_not_called()
                else:
                    writer.write.assert_called_once()
                    points = writer.write.call_args.kwargs["record"]
                    self.assertEqual(len(points), 1 if single_channel else 15)
                    ai17 = next(p for p in points if p.tags["channel"] == "ai17")
                    offset = 100 * config.channels.index("ai17")
                    # At 2 kHz the 1–3 ms window has four samples, indices 3–6.
                    expected = ({"mean": offset + 2.5, "min": offset + 2., "max": offset + 3., "std": .5}
                                if actual_rate == 1000 else
                                {"mean": offset + 4.5, "min": offset + 3., "max": offset + 6., "std": 1.25**.5})
                    self.assertEqual(ai17.fields, expected)
                    self.assertEqual(ai17.tags["label"], config.channel_settings["ai17"]["label"])
                    self.assertNotIn("ai16", {p.tags["channel"] for p in points})
        self.assertEqual(order, ["counter", "analog"])
        self.assertEqual([c.args[0] for c in analog.ai_channels.add_ai_voltage_chan.call_args_list],
                         [f"Dev2/{ch}" for ch in config.channel_settings])
        analog.start.assert_called_once()
        counter.start.assert_called_once()
        analog.__exit__.assert_called_once()
        counter.__exit__.assert_called_once()
        channel = counter.ci_channels.add_ci_count_edges_chan.return_value
        self.assertEqual(channel.ci_count_edges_term, "/Dev2/PFI0")
        self.assertTrue(channel.ci_count_edges_dig_fltr_enable)
        self.assertAlmostEqual(channel.ci_count_edges_dig_fltr_min_pulse_width, 0.0001)
        timing = counter.timing.cfg_samp_clk_timing.call_args.kwargs
        self.assertEqual(timing["source"], "/Dev2/ai/SampleClock")
        self.assertEqual(timing["rate"], actual_rate)
        self.assertEqual(counter.triggers.arm_start_trigger.dig_edge_src, "/Dev2/ai/StartTrigger")
        writer.__exit__.assert_called_once()
        client.__exit__.assert_called_once()
        self.assertFalse(any(t.name == "influx-writer" for t in threading.enumerate()))

    def test_shutdown_drains_closed_cycle_and_maps_physical_channels(self):
        self.run_simulation()

    def test_single_channel_daq_reads(self):
        self.run_simulation(single_channel=True)

    def test_windows_use_actual_hardware_sample_rate(self):
        self.run_simulation(actual_rate=2000)

    def test_no_trigger_no_writes(self):
        self.run_simulation(trigger=False)

    def test_full_writer_queue_drops_statistics_without_blocking_reads(self):
        with self.assertLogs("nidaq_to_influx.logger", level="WARNING"):
            self.run_simulation(queue_full=True)

    def test_writer_failure_propagates_and_closes_resources(self):
        self.run_simulation(write_failure=True)

    def test_daq_failure_propagates_and_closes_resources(self):
        self.run_simulation(read_failure=True)

    def test_counter_failure_propagates_and_closes_resources(self):
        self.run_simulation(counter_failure=True)


if __name__ == "__main__":
    unittest.main()
