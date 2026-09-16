"""Continuous AI and trigger-edge acquisition with a statistics-only writer."""

from dataclasses import replace
import datetime
import logging
import queue
import threading

from .cycles import CycleReducer
from .reduction import make_statistics_points

log = logging.getLogger(__name__)


def run_logger(config, token, stop_event=None):
    # Lazy imports let configuration and CLI help work without the NI driver.
    import nidaqmx
    from nidaqmx.constants import AcquisitionType, CountDirection, Edge, TaskMode, TerminalConfiguration, TriggerType
    from influxdb_client import InfluxDBClient, Point, WritePrecision
    from influxdb_client.client.write_api import SYNCHRONOUS

    stop_event = stop_event if stop_event is not None else threading.Event()
    producer_done = threading.Event()
    pending = queue.Queue(maxsize=config.queue_size)
    failures = []
    counts = {"written": 0, "dropped": 0}
    cycles = None

    with InfluxDBClient(
        url=config.url, token=token, org=config.org, timeout=config.timeout_ms
    ) as client:
        with client.write_api(write_options=SYNCHRONOUS) as writer:
            def write_loop():
                try:
                    while not producer_done.is_set() or not pending.empty():
                        try:
                            timestamp, statistics = pending.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        try:
                            points = make_statistics_points(config, timestamp, statistics, Point, WritePrecision.NS)
                            writer.write(bucket=config.bucket, org=config.org, record=points)
                            counts["written"] += 1
                        finally:
                            pending.task_done()
                except Exception as exc:
                    failures.append(exc)
                    stop_event.set()

            worker = threading.Thread(target=write_loop, name="influx-writer")
            worker.start()
            try:
                with nidaqmx.Task() as analog, nidaqmx.Task() as counter:
                    for channel in config.channels:
                        analog.ai_channels.add_ai_voltage_chan(
                            f"{config.device}/{channel}",
                            terminal_config=TerminalConfiguration.DEFAULT,
                        )
                    analog.timing.cfg_samp_clk_timing(
                        rate=config.sample_rate,
                        sample_mode=AcquisitionType.CONTINUOUS,
                        samps_per_chan=config.chunk_size * config.buffer_chunks,
                    )
                    analog.control(TaskMode.TASK_COMMIT)
                    # Use the driver's actual (possibly coerced) rate for window indices.
                    actual_rate = analog.timing.samp_clk_rate
                    cycles = CycleReducer(replace(config, sample_rate=actual_rate))
                    channel = counter.ci_channels.add_ci_count_edges_chan(
                        f"{config.device}/{config.trigger_counter}",
                        edge=Edge.RISING, initial_count=0, count_direction=CountDirection.COUNT_UP,
                    )
                    channel.ci_count_edges_term = config.trigger_source
                    if config.trigger_filter_min_pulse_width_us > 0:
                        channel.ci_count_edges_dig_fltr_min_pulse_width = config.trigger_filter_min_pulse_width_us / 1_000_000
                        channel.ci_count_edges_dig_fltr_enable = True
                    else:
                        channel.ci_count_edges_dig_fltr_enable = False
                    counter.timing.cfg_samp_clk_timing(
                        rate=actual_rate,
                        source=f"/{config.device}/ai/SampleClock",
                        active_edge=Edge.RISING,
                        sample_mode=AcquisitionType.CONTINUOUS,
                        samps_per_chan=config.chunk_size * config.buffer_chunks,
                    )
                    # Arm the counter on AI startup, before the first shared sample clock.
                    # This avoids counting PFI edges during software task setup.
                    arm = counter.triggers.arm_start_trigger
                    arm.trig_type = TriggerType.DIGITAL_EDGE
                    arm.dig_edge_src = f"/{config.device}/ai/StartTrigger"
                    arm.dig_edge_edge = Edge.RISING
                    counter.start()
                    # Host estimate of sample-zero UTC; relative timing is hardware clocked.
                    origin = datetime.datetime.now(datetime.timezone.utc)
                    analog.start()
                    log.info("Sampling %s channels on %s at %s Hz; rising edges on %s via %s",
                             len(config.channels), config.device, actual_rate,
                             config.trigger_source, config.trigger_counter)
                    log.info("Waiting for two trigger edges to publish the first cycle; press Ctrl+C to stop")
                    log.info("Trigger lockout: %s ms; hardware minimum pulse width: %s us",
                             config.trigger_min_interval_ms, config.trigger_filter_min_pulse_width_us)
                    while not stop_event.is_set():
                        chunk = analog.read(
                            number_of_samples_per_channel=config.chunk_size,
                            timeout=config.read_timeout,
                        )
                        edge_counts = counter.read(
                            number_of_samples_per_channel=config.chunk_size,
                            timeout=config.read_timeout,
                        )
                        for start_sample, statistics in cycles.add(chunk, edge_counts):
                            timestamp = origin + datetime.timedelta(seconds=start_sample / actual_rate)
                            try:
                                pending.put_nowait((timestamp, statistics))
                            except queue.Full:
                                counts["dropped"] += 1
                                log.warning("Writer queue full: dropped a completed cycle")
                        # No raw arrays enter the writer queue or survive this read iteration.
                        del chunk, edge_counts
            except Exception as exc:
                # This block only handles acquisition/reduction, never HTTP client errors.
                log.error("Acquisition failed (%s): %s", type(exc).__name__, exc)
                raise
            finally:
                if cycles is not None:
                    cycles.discard_open_cycle()
                producer_done.set()
                worker.join() # Drain completed cycles before closing the client.
                log.info("Stopped: %s cycles written, %s dropped, %s skipped, %s pending",
                         counts["written"], counts["dropped"],
                         cycles.cycles_skipped if cycles is not None else 0, pending.qsize())
                if cycles is not None:
                    log.info("Trigger diagnostics: %s ignored edges, %s extra edges merged between scans",
                             cycles.edges_ignored, cycles.edges_merged)
            if failures:
                raise RuntimeError("InfluxDB write failed; acquisition stopped") from failures[0]
