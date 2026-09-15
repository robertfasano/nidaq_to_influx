"""Identify experimental cycles from sample-clocked, cumulative edge counts."""

import logging
from numbers import Real

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
            if delta != 1:
                raise ValueError("Multiple trigger edges between analog scans; increase sample_rate or check trigger noise")
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
            self.last_count = count
            segment_start = index
        self._feed(chunk, segment_start, size)
        self.samples_seen += size
        return completed

    def discard_open_cycle(self):
        if self.reducer is not None:
            log.info("Discarded final cycle: no closing trigger was received")
            self.reducer = None
            self.cycle_start = None
