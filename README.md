# MikroTik GPS Venus

Get GPS location from a MikroTik router into the Victron VRM portal on a Cerbo GX or other Venus OS device.

The script polls `/system/gps/monitor` over the RouterOS API, converts the position to NMEA 0183, and feeds Victron's built-in `gps-dbus` service through a virtual serial port.

```text
MikroTik router -> RouterOS API -> mikrotik_gps_venus.py -> NMEA -> virtual serial -> gps-dbus -> VRM
```

No Python packages, MQTT broker, USB GPS dongle, or external gateway are required.

## Prerequisites

- Victron GX device running Venus OS with root/SSH access
- MikroTik router with GPS working under `/system gps`
- RouterOS API enabled on the MikroTik router
- Network route from the GX device to the MikroTik router

For manual Cerbo testing, the MikroTik does not need to be reachable yet.

The service is intended to be set-and-forget. If the MikroTik is down at boot,
each API attempt times out and the service keeps retrying until the first valid
fix appears. If the MikroTik drops out later, Venus keeps receiving the last
good fix while the poller retries in the background.

The running service watches `.env`. Changes to MikroTik connection details,
manual coordinates, timeouts, `GPS_SOURCE`, and polling intervals are reloaded
automatically. Restart only after changing virtual serial device paths or after
copying in a new script version.

## MikroTik Setup

On RouterOS, confirm GPS and API access:

```routeros
/system gps monitor once
/ip service print where name~"api"
```

If needed, enable the plain API service for the vessel LAN:

```routeros
/ip service enable api
/ip service set api port=8728
```

API-SSL on port `8729` is also supported by setting `MIKROTIK_TLS=true`.

## Install On Venus OS

Copy the project to the GX device:

```bash
ssh root@<cerbo-ip> "mkdir -p /data/mikrotik-gps-venus"
scp -r mikrotik_gps_venus.py .env.example scripts root@<cerbo-ip>:/data/mikrotik-gps-venus/
ssh root@<cerbo-ip>
cd /data/mikrotik-gps-venus
cp .env.example .env
chmod +x mikrotik_gps_venus.py
sh scripts/install-helpers.sh
```

Edit `.env` on the GX device:

```bash
vi .env
```

At minimum, set:

```dotenv
MIKROTIK_HOST=<router-ip>
MIKROTIK_PORT=8728
MIKROTIK_USER=admin
MIKROTIK_PASSWORD=your-router-password
GPS_SOURCE=mikrotik
```

Do not commit the real `.env`; it is ignored by git.

## Test With The MikroTik

Once the GX can reach the router:

```bash
./mikrotik_gps_venus.py -v test
```

The test prints latitude, longitude, altitude, and the generated NMEA sentences.

## Manual Coordinate Test

Use this before the MikroTik is physically on the vessel network. Provide any
safe test coordinate you want Venus OS to receive:

```bash
./mikrotik_gps_venus.py -v test --lat <latitude> --lon <longitude> --alt <altitude_m>
```

To push those fixed coordinates into Venus OS through `gps-dbus`:

```bash
./mikrotik_gps_venus.py -v start --manual --lat <latitude> --lon <longitude> --alt <altitude_m>
```

You can also set manual mode in `.env`:

```dotenv
GPS_SOURCE=manual
MANUAL_LAT=<latitude>
MANUAL_LON=<longitude>
MANUAL_ALT=<altitude_m>
```

Switch back to `GPS_SOURCE=mikrotik` after the MikroTik is reachable from the Cerbo.

## Start The Bridge

```bash
./mikrotik_gps_venus.py -v start
```

Check VRM after a minute. Stop the bridge with:

```bash
./mikrotik_gps_venus.py stop
```

After installing the helper scripts, the short commands are:

```bash
./start
./stop
./restart
./status
./logs
```

Usually, after changing `.env`, just wait for the next poll or run:

```bash
./logs
```

Use `./restart` after changing virtual serial device paths or updating the script.

## Auto-Start On Boot

Add this to `/data/rc.local` on the GX device:

```bash
#!/bin/bash
cd /data/mikrotik-gps-venus
./start
```

Make it executable:

```bash
chmod +x /data/rc.local
```

The `/data` partition survives Venus OS firmware updates.

## Configuration

| Setting | Default | Description |
| --- | --- | --- |
| `GPS_SOURCE` | `mikrotik` | `mikrotik` or `manual` |
| `MIKROTIK_HOST` | empty | Router address; required for `GPS_SOURCE=mikrotik` |
| `MIKROTIK_PORT` | `8728` | RouterOS API port |
| `MIKROTIK_USER` | `admin` | RouterOS API username |
| `MIKROTIK_PASSWORD` | empty | RouterOS API password |
| `MIKROTIK_TLS` | `false` | Use API-SSL |
| `MIKROTIK_TLS_VERIFY` | `false` | Verify API-SSL certificate |
| `MIKROTIK_TIMEOUT` | `10` | Seconds before one API attempt fails |
| `MIKROTIK_REQUIRE_VALID` | `true` | Require `valid=yes` when RouterOS returns it |
| `MIKROTIK_MAX_DATA_AGE` | `0` | Reject stale GPS data when greater than zero |
| `REJECT_ZERO_COORDINATES` | `true` | Reject bogus `0,0` coordinates |
| `MANUAL_LAT` | empty | Manual latitude for testing |
| `MANUAL_LON` | empty | Manual longitude for testing |
| `MANUAL_ALT` | `0.0` | Manual altitude in meters |
| `POLL_INTERVAL` | `30` | Seconds between GPS polls |
| `STARTUP_TIMEOUT` | `0` | Seconds to wait for first fix before exiting; `0` retries forever |
| `NMEA_WRITE_INTERVAL` | `1` | Seconds between NMEA writes |
| `NMEA_VIRTUAL_DEVICE` | `/dev/ttyACM0` | Virtual serial read side for gps-dbus |
| `NMEA_WRITE_DEVICE` | `/tmp/mikrotik_gps_write` | Virtual serial write side |
| `GPS_DBUS_BINARY` | `/opt/victronenergy/gps-dbus/gps_dbus` | Victron gps-dbus binary |

## Commands

```text
mikrotik_gps_venus.py [-v | -d] [--env-file PATH] {config,test,start,stop}

Commands:
  config    Print effective configuration
  test      Fetch or use one GPS fix and print NMEA
  start     Start the GPS bridge
  stop      Stop gps-dbus/socat bridge services
```

Useful overrides:

```bash
./mikrotik_gps_venus.py test --host <router-ip> --user admin --password secret
./mikrotik_gps_venus.py test --manual --lat 1.234567 --lon 2.345678 --alt 10
./mikrotik_gps_venus.py test --tls --port 8729
```

## Troubleshooting

**Manual test works but MikroTik mode fails**

- Confirm the GX can route to the MikroTik on the vessel network
- Confirm `/ip service print where name~"api"` shows the selected API service enabled
- Confirm the RouterOS user has `api` and `read` permissions
- Run `/system gps monitor once` on the MikroTik and check that it has a valid fix

**GPS not showing on VRM**

- Run `./mikrotik_gps_venus.py -d start` for debug logs
- Stop other GPS services first with `./mikrotik_gps_venus.py stop`
- Verify the virtual serial device exists: `ls -la /dev/ttyACM0`

**gps-dbus exits immediately**

- Ensure NMEA is flowing by running manual mode first
- Check that `/dev/ttyACM0` is not already used by a physical USB GPS
- Increase `NMEA_WRITE_INTERVAL` only after the default works

## Development

This project uses `uv` for dev tooling. Runtime on Venus OS remains Python stdlib only.

```bash
uv run ruff check .
uv run ruff format .
uv run basedpyright
python -m py_compile mikrotik_gps_venus.py
```

## License

MIT
