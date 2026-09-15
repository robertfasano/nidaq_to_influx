# NI-DAQ to InfluxDB

Continuously acquire analog voltages from NI-DAQmx and write per-cycle statistics
to InfluxDB. Each rising trigger edge closes the previous experimental cycle.
The `nidaq-to-influx` terminal command works from any directory after installation.

## Install

Use Python 3.9 or newer on the acquisition computer, with the NI-DAQmx driver
installed and the device configured in NI MAX. InfluxDB must also be reachable.
The Python package installs the Python clients; it does not install the hardware driver.

For an isolated command-line installation, use pipx:

```sh
pipx install "/absolute/path/to/Yb2 Logging"
pipx ensurepath
```

Open a new terminal after `ensurepath` if needed. Alternatively, install into
your Python environment using `python -m pip install "/absolute/path/to/Yb2 Logging"`.
That environment's scripts directory must be on PATH; activate it first if using a virtual environment.

## Configure and start

Print the bundled settings to a file in a permanent location:

```sh
nidaq-to-influx --print-default-config > "$HOME/nidaq-to-influx.toml"
```

Edit that file for your hardware and database. Set the token in the environment
of the terminal or service that launches the command. For example, in Bash or Zsh,
this prompts without displaying the token or putting it in shell history:

```sh
printf 'InfluxDB token: '
read -rs INFLUX_TOKEN
printf '\n'
export INFLUX_TOKEN
nidaq-to-influx --config "$HOME/nidaq-to-influx.toml"
```

In PowerShell, set `$env:INFLUX_TOKEN` before launching the same command, and use
an absolute Windows path for `--config`.

`INFLUX_TOKEN` is required at startup and is never read from the config file.
With no `--config`, the logger uses the bundled settings regardless of your
current directory. Relative config paths are relative to your current directory;
use an absolute path when launching elsewhere. Config files may override individual
DAQ, trigger, and Influx settings. A supplied `[channels]` table replaces the
default channel settings in full.

```sh
nidaq-to-influx --config "$HOME/nidaq-to-influx.toml" --check-config
nidaq-to-influx --help
```

Config checks do not require a token, hardware, or a database connection.
`python -m nidaq_to_influx` also runs the logger.

## Channels, triggers, and reduction

Labels and reduction windows belong to the same channel entry:

```toml
[trigger]
terminal = "PFI0"
counter = "ctr0"

[channels]
ai0 = { label = "EOM Driver Monitor", start = 500, stop = 550 }
ai1 = { label = "Sisy PD 4", start = 200, stop = 250 }
```

`start` and `stop` are milliseconds relative to the detected rising edge.
Windows include `start` and exclude `stop`. At 1000 Hz, 500–550 ms selects
samples 500 through 549: 50 values. For non-integer boundaries, the first selected
sample is the first whose sample time is at or after `start`; samples at or after
`stop` are excluded. Empty windows are rejected.

For the **whole sequence**, regardless of its duration, set both bounds to the
quoted string `"null"` (TOML has no native null value):

```toml
[channels]
ai0 = { label = "EOM Driver Monitor", start = "null", stop = "null" }
ai1 = { label = "Sisy PD 4", start = 500, stop = 550 }
```

Whole-cycle channels include all samples from the opening trigger up to, but
excluding, the next trigger. They can be mixed with fixed windows. Both bounds
must be `"null"`; a single null bound is rejected. The loader converts these
strings to Python `None` internally. Statistics still use constant memory and
are published only after the closing edge. An unfinished final cycle is discarded.

The `[channels]` table is the only list of inputs: the logger acquires exactly
those inputs in table order, then reduces and publishes each one. Add or remove
entries to change what the DAQ polls. Each entry must have all three keys:
`label`, `start`, and `stop`. The bundled defaults use the whole sequence on every input; set numeric
windows for your experiment as needed.

**Existing configs:** replace the old `[labels]` and `[windows]` tables with
combined `[channels]` entries, and remove the redundant `channels` list from
`[daq]`. The old settings now produce configuration errors.
After updating the source, reinstall the package into the environment where you
run the command (for pipx, `pipx install --force "/absolute/path/to/Yb2 Logging"`).

## Continuous acquisition across cycles

The logger uses two continuous hardware tasks on the USB-6343:

1. Analog inputs run on the internal AI sample clock.
2. A dedicated counter counts rising edges on the configured PFI terminal and
   records its cumulative count on that same AI sample clock. The counter is
   armed by the internal AI Start Trigger, with its task started before AI.

There is no per-cycle task stop, restart, or software rearm. The counter captures
edges in hardware, including pulses shorter than one analog sample period,
subject to the device's input pulse specifications. Its recorded count aligns
each observed edge with an analog scan. This arrangement uses the buffered edge
counting and internal clock/arm routes described in the
[NI X Series manual, chapter 7](https://docs-be.ni.com/bundle/pcie-pxie-usb-63xx-features/raw/resource/enus/370784k.pdf).

The first edge begins a cycle; the second closes it and begins the next. Data
before the first edge is discarded. A sample at a detected boundary belongs to
the new cycle. Windows can cross any number of reads. Statistics accumulate
while acquisition continues, but nothing is published until the next edge.
Raw arrays are discarded after each read; only statistics enter the writer queue.

Each completed cycle produces one point per configured, acquired channel with
`channel` and `label` tags and exactly four floating-point fields:
`mean`, `min`, `max`, and `std`. Standard deviation is the population value
(`ddof=0`); mean/min/max/std are in volts. No new `voltage` field or raw samples
are written. Existing raw data in the database is unaffected; update dashboards
to query the new fields.

If a cycle ends before every configured window finishes, the entire cycle is
skipped with a warning. A final cycle without a closing edge is also discarded,
even if its windows have finished. A long pause between triggers does not grow
the retained raw data, and does not cause a trigger-wait timeout: both tasks keep
sampling. More than one edge between scans is ambiguous and stops acquisition
with an error; use a faster sample rate or fix trigger noise. A normal 32-bit
counter wrap is handled.

### Timing and hardware limits

At 1000 Hz, a trigger is located to approximately one analog sample interval
(1 ms). Windows are relative to the first scan showing the incremented count,
not an exact sub-sample edge timestamp. The USB-6343 also multiplexes its analog
inputs, so channels within a scan have conversion-time offsets. NI-DAQmx's
actual sample rate is used if it differs from the requested rate.

Points are timestamped at the estimated start of their cycle using one host UTC
startup time plus the hardware sample index. Absolute UTC is approximate and
includes host/device startup latency; this is not a hardware UTC timestamp.
Publication follows the closing edge by up to roughly one read chunk plus
processing and database latency (100 ms chunks at the default settings).

The code avoids rearm gaps, but sustained acquisition still requires enough
processing speed and hardware buffer capacity. Reserve the configured counter
exclusively for the logger. The clock/trigger routing has been checked against
NI documentation and simulated tests; it still needs validation on your actual
USB-6343 and NI-DAQmx installation.

For a bench check, apply a known voltage and rising PFI0 edges spaced farther
apart than the latest window stop. Expect no point before the second edge, then
one statistics point per configured channel per completed cycle. Confirm the
channel means, count the resulting cycles, and test a deliberately short cycle
to verify the skip warning before using it for experiment data.

## Defaults and shutdown

The defaults acquire the notebook's labeled Dev2 inputs ai0–ai7 and ai17–ai23,
1000 Hz per channel, 100 samples per read, database settings, and labels.
Channel count comes from the master `[channels]` table. The unlabeled ai16 input
is no longer acquired; add a channel entry to include it. Tags follow the actual
physical input, fixing the notebook's incorrect mapping for the second input bank.

Press **Ctrl+C** to stop (SIGTERM is also handled). The current DAQ read finishes
or times out, both hardware tasks close, and queued completed cycles drain before
the client closes. Shutdown can take longer than the read timeout if many writes are queued.
Writes run on a separate thread and count as written only after InfluxDB confirms
them. A full writer queue drops newly completed cycles with a warning without
blocking acquisition. `queue_size` counts cycles, not raw read chunks. A DAQ or write failure stops
the logger with a nonzero exit status. Queued data is in memory and is not persisted
across failures; there is no automatic retry or restart policy.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

The original notebook has been replaced with a migration note so it no longer
contains a separate implementation or an embedded credential. Core code lives in
`src/nidaq_to_influx`: config loading, reduction, cycle segmentation,
acquisition/writing, and the CLI are separate modules. Unit tests use simulated
hardware and database clients.
