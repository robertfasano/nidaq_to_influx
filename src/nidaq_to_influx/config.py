"""Load and validate non-secret configuration independently of the working directory."""

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
import math
import re

from .reduction import sample_bounds

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10
    import tomli as tomllib


def default_config_text():
    return files("nidaq_to_influx").joinpath("default.toml").read_text(encoding="utf-8")


@dataclass(frozen=True)
class Config:
    device: str
    sample_rate: float
    chunk_size: int
    read_timeout: float
    buffer_chunks: int
    queue_size: int
    url: str
    org: str
    bucket: str
    measurement: str
    timeout_ms: int
    channel_settings: dict
    trigger_terminal: str
    trigger_counter: str
    trigger_min_interval_ms: float
    trigger_filter_min_pulse_width_us: float

    @property
    def channels(self):
        """Physical input order comes exclusively from the master config table."""
        return list(self.channel_settings)

    @property
    def trigger_source(self):
        if self.trigger_terminal.startswith("/"):
            return self.trigger_terminal
        return f"/{self.device}/{self.trigger_terminal}"


def load_config(path=None):
    data = tomllib.loads(default_config_text())
    if path is not None:
        overrides = tomllib.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        for section, values in overrides.items():
            if section in ("labels", "windows"):
                raise ValueError("Replace [labels] and [windows] with [channels] entries containing label, start, stop")
            if section not in data or not isinstance(values, dict):
                raise ValueError(f"Unknown or invalid config section: {section}")
            if section == "channels":
                data[section] = values
            else:
                unknown = values.keys() - data[section].keys()
                if unknown:
                    raise ValueError(f"Unknown {section} settings: {', '.join(sorted(unknown))}")
                data[section].update(values)
    config = Config(**data["daq"], **data["influx"], channel_settings=data["channels"],
                    trigger_terminal=data["trigger"]["terminal"],
                    trigger_counter=data["trigger"]["counter"],
                    trigger_min_interval_ms=data["trigger"]["min_interval_ms"],
                    trigger_filter_min_pulse_width_us=data["trigger"]["filter_min_pulse_width_us"])
    for name in ("device", "url", "org", "bucket", "measurement"):
        value = getattr(config, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty string")
    for name in ("chunk_size", "buffer_chunks", "queue_size", "timeout_ms"):
        value = getattr(config, name)
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    for name in ("sample_rate", "read_timeout"):
        value = getattr(config, name)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")
    terminal = config.trigger_terminal
    for name in ("trigger_min_interval_ms", "trigger_filter_min_pulse_width_us"):
        value = getattr(config, name)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a nonnegative finite number (0 disables it)")
    terminal_pattern = rf"(?:/{re.escape(config.device)}/)?PFI(?:[0-9]|1[0-5])"
    if not isinstance(terminal, str) or not re.fullmatch(terminal_pattern, terminal):
        raise ValueError("trigger.terminal must be PFI0–PFI15, optionally prefixed with /device/")
    if not isinstance(config.trigger_counter, str) or not re.fullmatch(r"ctr[0-3]", config.trigger_counter):
        raise ValueError("trigger.counter must be ctr0, ctr1, ctr2, or ctr3")
    if not config.channel_settings:
        raise ValueError("At least one [channels] entry is required")
    for channel, window in config.channel_settings.items():
        if not re.fullmatch(r"ai\d+", channel):
            raise ValueError(f"Invalid analog input in [channels]: {channel}")
        if not isinstance(window, dict) or set(window) != {"label", "start", "stop"}:
            raise ValueError(f"Channel {channel} must contain label, start, and stop (milliseconds)")
        if not isinstance(window["label"], str) or not window["label"].strip():
            raise ValueError(f"Label for {channel} must be a nonempty string")
        start, stop = window["start"], window["stop"]
        if start == "null" and stop == "null":
            window["start"] = window["stop"] = None
            continue
        if start == "null" or stop == "null":
            raise ValueError(f"Whole-cycle window for {channel} requires both start and stop to be \"null\"")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, stop)):
            raise ValueError(f"Window for {channel} must use finite numbers")
        if start < 0 or stop <= start:
            raise ValueError(f"Window for {channel} requires 0 <= start < stop")
        first, last = sample_bounds(start, stop, config.sample_rate)
        if first >= last:
            raise ValueError(f"Window for {channel} contains no samples at the configured sample rate")
    return config
