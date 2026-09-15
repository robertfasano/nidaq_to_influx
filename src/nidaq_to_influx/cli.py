"""Command-line entry point."""

import argparse
import logging
import os
import signal
import threading

from .config import default_config_text, load_config
from .logger import run_logger


def main(argv=None):
    parser = argparse.ArgumentParser(description="Log NI-DAQ cycle statistics to InfluxDB on successive rising trigger edges.")
    parser.add_argument("--config", metavar="PATH", help="TOML config overrides (default: packaged settings)")
    parser.add_argument("--print-default-config", action="store_true", help="Print a config template and exit")
    parser.add_argument("--check-config", action="store_true", help="Validate config without connecting to hardware")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    args = parser.parse_args(argv)
    if args.print_default_config:
        print(default_config_text(), end="")
        return 0
    try:
        config = load_config(args.config)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f"Invalid configuration: {exc}")
    if args.check_config:
        print(f"Configuration valid: {len(config.channels)} channels on {config.device}; "
              f"rising trigger {config.trigger_source}, counter {config.trigger_counter}")
        return 0
    token = os.environ.get("INFLUX_TOKEN", "").strip()
    if not token:
        parser.error("Set the INFLUX_TOKEN environment variable before starting the logger")
    logging.basicConfig(level=args.log_level, format="%(asctime)s [%(levelname)s] %(message)s")
    stop_event = threading.Event()
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda signum, frame: stop_event.set())
        run_logger(config, token, stop_event)
    except Exception:
        # Third-party exception messages can contain HTTP headers; do not log credentials.
        logging.error("Logger failed. Check NI-DAQmx hardware/driver, InfluxDB connectivity and permissions.")
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0
