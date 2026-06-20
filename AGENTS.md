# Agent Guide

This repository contains a small, unattended bridge from MikroTik RouterOS GPS
data to Victron Venus OS. Treat reliability on the Cerbo GX as the main product
requirement: if the MikroTik is down, GPS has no fix, or the network route is
missing, the service must keep running and recover automatically.

## Project Shape

- `mikrotik_gps_venus.py` is the runtime application. It intentionally uses only
  the Python standard library on Venus OS.
- `scripts/` contains operator helpers installed on the Cerbo as `./start`,
  `./stop`, `./restart`, `./status`, and `./logs`.
- `.env.example` is the tracked configuration template. Real `.env` files are
  ignored and may contain router credentials.
- `pyproject.toml` and `uv.lock` are for local development tooling only.
- `CLAUDE.md` is a compatibility stub pointing to this file.

## Runtime Architecture

```text
MikroTik router
  -> RouterOS API /system/gps/monitor once
  -> parse and validate coordinates
  -> generate GPGGA/GPRMC NMEA 0183
  -> socat PTY pair
  -> Victron gps-dbus
  -> D-Bus / VRM
```

The RouterOS API client is implemented in `RouterOSAPI`; do not add Python
runtime dependencies unless there is a very strong reason and the Venus OS
deployment story is updated.

## Reliability Rules

- The service is set-and-forget. Do not make startup exit by default when the
  MikroTik is unreachable. `STARTUP_TIMEOUT=0` means retry forever.
- Each API attempt must be bounded by `MIKROTIK_TIMEOUT`; no socket read or write
  should block indefinitely.
- When polling fails after a good fix, keep writing the last good NMEA fix while
  retrying in the background.
- Reject bogus coordinates before updating state. `0,0` is rejected by default
  via `REJECT_ZERO_COORDINATES=true`.
- Respect `valid=no` from RouterOS when `MIKROTIK_REQUIRE_VALID=true`.
- Device path changes (`NMEA_VIRTUAL_DEVICE`, `NMEA_WRITE_DEVICE`,
  `GPS_DBUS_BINARY`) require restart. Do not try to swap the live `socat` pair in
  place without a deliberate design change.

## Live Configuration

The running bridge watches `.env` through `ConfigReloader`.

Runtime-reloadable fields include:

- MikroTik connection settings and credentials
- `GPS_SOURCE`
- manual coordinates
- API timeout, poll interval, NMEA write interval
- GPS validity/staleness/zero-coordinate guards

Restart-only fields are listed in `RESTART_ONLY_CONFIG_FIELDS`.

If you add a new setting, decide whether it is safe to live-reload. Add it to
`RUNTIME_CONFIG_FIELDS` only if changing it cannot invalidate the current
virtual serial/gps-dbus pipeline.

## Testing

Run these before handing changes back:

```bash
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
pnpm dlx shellcheck scripts/start scripts/stop scripts/restart scripts/status scripts/logs scripts/install-helpers.sh
python -m py_compile mikrotik_gps_venus.py
```

Useful behavior checks:

```bash
python mikrotik_gps_venus.py -v test
python mikrotik_gps_venus.py test --manual --lat 1.234567 --lon 2.345678 --alt 10
python mikrotik_gps_venus.py test --manual --lat 0 --lon 0
```

The `0,0` manual test should fail. For an unreachable-router timeout check, use a
short timeout and expect command failure, not a hang:

```bash
MIKROTIK_TIMEOUT=1 python mikrotik_gps_venus.py test --host 192.0.2.1
```

## Cerbo Deployment Notes

Current test Cerbo:

- Host: `172.20.73.199`
- App path: `/data/mikrotik-gps-venus`
- Log: `/data/log/mikrotik-gps-venus.log`
- Boot hook: `/data/rc.local`

The boot hook should stay short:

```sh
#!/bin/sh
sleep 15
cd /data/mikrotik-gps-venus || exit 0
./start
```

After copying helper scripts, run:

```sh
cd /data/mikrotik-gps-venus
sh scripts/install-helpers.sh
```

Operator commands on the Cerbo:

```sh
./status
./logs
./restart
```

Usually `.env` edits do not need `./restart`; wait for the next poll or check
`./logs` for `Reloaded .env`.

## Style

- Keep runtime code compatible with the Python available on Venus OS; current
  project target is Python 3.8+.
- Preserve LF endings. `.gitattributes` enforces this for source, docs, scripts,
  TOML, and lock files.
- Keep shell helpers POSIX `sh`, not Bash.
- Avoid committing `.env`, logs, virtualenvs, caches, or router credentials.
- Prefer small, explicit changes over framework-style abstractions. This project
  is intentionally boring because it runs unattended on a boat.
