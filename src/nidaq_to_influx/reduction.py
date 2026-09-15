"""Reduce one trigger's sample stream without retaining raw sample arrays."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import math


def sample_bounds(start_ms, stop_ms, sample_rate):
    """Indices whose sample times fall in [start_ms, stop_ms)."""
    if start_ms is None and stop_ms is None:
        return 0, None # End is determined by the next trigger, not elapsed samples.
    if start_ms is None or stop_ms is None:
        raise ValueError("Whole-cycle windows require both start and stop to be None")
    rate = Decimal(str(sample_rate)) / Decimal(1000)
    return tuple(int((Decimal(str(value)) * rate).to_integral_value(rounding=ROUND_CEILING))
                 for value in (start_ms, stop_ms))


@dataclass
class _Statistics:
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    minimum: float = math.inf
    maximum: float = -math.inf

    def add(self, value):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("Cannot reduce a non-finite voltage sample")
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

    def result(self):
        if not self.count:
            raise ValueError("Cannot reduce an empty window")
        return {"mean": self.mean, "min": self.minimum, "max": self.maximum,
                "std": math.sqrt(max(0.0, self.m2 / self.count))}


class WindowReducer:
    """Feed consecutive DAQ reads starting at sample zero of one trigger.

    Create a new reducer for each trigger. Only completed windows may be emitted.
    Single-channel reads use DAQmx's flat-list representation.
    """

    def __init__(self, channels, windows, sample_rate):
        self.channels = tuple(channels)
        self.bounds = {channel: sample_bounds(window["start"], window["stop"], sample_rate)
                       for channel, window in windows.items()}
        if not self.bounds or set(self.bounds) - set(self.channels):
            raise ValueError("Windows must reference acquired channels")
        if any(start < 0 or (stop is not None and stop <= start) for start, stop in self.bounds.values()):
            raise ValueError("Each window must contain at least one sample after the trigger")
        self.required_samples = max((stop for start, stop in self.bounds.values() if stop is not None), default=0)
        self.has_whole_cycle = any(stop is None for start, stop in self.bounds.values())
        self._closed = False
        self.samples_seen = 0
        self._statistics = {channel: _Statistics() for channel in self.bounds}

    @property
    def complete(self):
        return (self.samples_seen > 0 and self.samples_seen >= self.required_samples
                and (not self.has_whole_cycle or self._closed))

    def close(self):
        """Mark the next trigger boundary; whole-cycle statistics can now finish."""
        self._closed = True

    def add(self, chunk):
        if self.complete or self._closed:
            raise ValueError("This trigger has already been fully reduced")
        if len(self.channels) == 1:
            chunk = [chunk]
        if len(chunk) != len(self.channels):
            raise ValueError("DAQ returned an unexpected channel count")
        size = len(chunk[0])
        if not size or any(len(samples) != size for samples in chunk):
            raise ValueError("DAQ channel arrays must have equal, nonzero lengths")
        for channel, samples in zip(self.channels, chunk):
            if channel not in self.bounds:
                continue
            start, stop = self.bounds[channel]
            first = max(0, start - self.samples_seen)
            last = size if stop is None else min(size, stop - self.samples_seen)
            for index in range(first, last):
                self._statistics[channel].add(samples[index])
        self.samples_seen += size

    def result(self):
        if not self.complete:
            raise ValueError("Trigger windows are incomplete; no statistics will be published")
        return {channel: stats.result() for channel, stats in self._statistics.items()}


def make_statistics_points(config, timestamp, statistics, point_type, precision):
    """Build one point per channel; no raw voltage samples enter the writer."""
    points = []
    for channel, values in statistics.items():
        if set(values) != {"mean", "min", "max", "std"}:
            raise ValueError("Each channel must supply exactly mean, min, max, and std")
        point = (point_type(config.measurement)
                 .tag("channel", channel)
                 .tag("label", config.channel_settings[channel]["label"]))
        for name, value in values.items():
            point.field(name, float(value))
        points.append(point.time(timestamp, precision))
    return points
