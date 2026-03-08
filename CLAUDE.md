# StarfixVenus

## Project overview

Single-file Python script (`starfixvenus.py`) that runs on a Victron GX device (Cerbo GX, etc.) to pull GPS coordinates from a Starlink dish and inject them into the Victron VRM portal — no external MQTT brokers, GPS dongles, or gateways needed.

## Architecture

```
Starlink dish (192.168.100.1:9200)
  -> grpcurl (gRPC over HTTP/2)
  -> Parse JSON -> Generate NMEA 0183 (GPGGA + GPRMC)
  -> socat PTY pair (virtual serial)
  -> Victron gps-dbus reads virtual serial
  -> D-Bus -> VRM Portal
```

## Key files

- `starfixvenus.py` — the entire application (single file, no dependencies beyond Python 3 stdlib)
- `grpcurl` — must be cross-compiled for the target platform (armv7l for Cerbo GX), deployed to `/data/starlink-gps/grpcurl`

## Important details

- Target platform: Venus OS (armv7), runs as root on the GX device
- Uses `grpcurl` to call `SpaceX.API.Device.Device/Handle` with `{"getLocation":{}}`
- Virtual serial created at `/dev/ttyACM0` (read side) and `/tmp/starlink_gps_write` (write side)
- Victron's native `/opt/victronenergy/gps-dbus/gps_dbus` consumes the NMEA stream
- Default poll interval: 30s from Starlink, 1s NMEA write cadence
- Config is at the top of the script in `STARLINK_CONFIG` and `NMEA_CONFIG` dicts
- No external Python packages — stdlib only (subprocess, json, threading, argparse, etc.)
