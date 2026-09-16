"""Identify experimental cycles from sample-clocked, cumulative edge counts."""

import logging
from numbers import Real
from decimal import Decimal, ROUND_CEILING

from .reduction import WindowReducer

log = logging.getLogger(__name__)


class CycleReducer:
    """Consume aligned AI/counter reads; emit only when the next edge arrives.

    Counter sample i and analog scan i share the same clock. A count increment
    makes scan i the first scan of a new cycle. Memory usage is independent of
    cycle length: only running statistics survive each read.
    """

    def __init__(self, config):
        self.config = config
        self.windows = config.channel_settings
        self.samples_seen = 0
        self.last_count = 0
        self.cycle_start = None
        self.reducer = None
        self.cycles_completed = 0
        self.cycles_skipped = 0
        self.min_interval_samples = int((Decimal(str(config.trigger_min_interval_ms))
                                        * Decimal(str(config.sample_rate)) / 1000)
                                       .to_integral_value(rounding=ROUND_CEILING))
        self.edges_ignored = 0
        self.edges_merged = 0

    def _feed(self, chunk, start, stop):
        if start == stop or self.reducer is None or self.reducer.complete:
            return
        segment = [samples[start:stop] for samples in chunk]
        self.reducer.add(segment[0] if len(segment) == 1 else segment)

    def add(self, chunk, edge_counts):
        """Return (start_sample_index, statistics) for each newly closed cycle."""
        if len(self.config.channels) == 1:
            chunk = [chunk]
        size = len(edge_counts)
        if not size or len(chunk) != len(self.config.channels) or any(len(row) != size for row in chunk):
            raise ValueError("Analog and counter reads must have matching, nonzero sample counts")
        completed = []
        segment_start = 0
        for index, count in enumerate(edge_counts):
            # Task.read can represent counter samples as integral-valued doubles.
            if not isinstance(count, Real) or not 0 <= count < 2**32 or int(count) != count:
                raise ValueError("Expected unsigned 32-bit hardware edge counts")
            count = int(count)
            delta = (count - self.last_count) % (2**32)
            if delta == 0:
                continue
            # Half-range comparison distinguishes a small forward jump (including
            # normal 32-bit wrap) from a reset/backward count. Ambiguous jumps fail.
            if delta >= 2**31:
                absolute_index = self.samples_seen + index
                raise ValueError(
                    "Trigger counter reset or backward/ambiguous jump: "
                    f"previous={self.last_count}, current={count}, delta={delta}, "
                    f"sample_index={absolute_index}, chunk_offset={index}, "
                    f"scan_interval_ms={1000 / self.config.sample_rate:.6g}, "
                    f"source={self.config.trigger_source}, counter={self.config.trigger_counter}. "
                    "Check trigger signal quality and counter routing; "
                    "buffered reads are independent of InfluxDB write latency."
                )
            self.last_count = count # Always track observed edges, including ignored ones.
            absolute_index = self.samples_seen + index
            if self.cycle_start is not None and absolute_index - self.cycle_start < self.min_interval_samples:
                self.edges_ignored += delta
                log.debug("Ignored %s trigger edges at sample %s during lockout", delta, absolute_index)
                continue
            if delta > 1:
                self.edges_merged += delta - 1
                log.warning("Merged %s trigger edges into one boundary at sample %s; source=%s",
                            delta, absolute_index, self.config.trigger_source)
            self._feed(chunk, segment_start, index)
            if self.reducer is not None:
                self.reducer.close()
                if self.reducer.complete:
                    completed.append((self.cycle_start, self.reducer.result()))
                    self.cycles_completed += 1
                else:
                    self.cycles_skipped += 1
                    duration_ms = (self.samples_seen + index - self.cycle_start) * 1000 / self.config.sample_rate
                    log.warning("Skipped cycle at sample %s: next trigger after %.3f ms, before all windows finished",
                                self.cycle_start, duration_ms)
            self.cycle_start = self.samples_seen + index
            self.reducer = WindowReducer(self.config.channels, self.windows, self.config.sample_rate)
            segment_start = index
        self._feed(chunk, segment_start, size)
        self.samples_seen += size
        return completed

    def discard_open_cycle(self):
        if self.reducer is not None:
            log.info("Discarded final cycle: no closing trigger was received")
            self.reducer = None
            self.cycle_start = None
