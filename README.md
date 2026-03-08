# StarfixVenus

Get your Starlink dish's GPS location into the Victron VRM portal — directly on the Cerbo GX (or any Venus OS device), with no external gateways, MQTT brokers, or USB GPS dongles.

## How it works

The script polls GPS coordinates from the Starlink dish over its local gRPC API, converts them to standard NMEA 0183 sentences, and feeds them into Victron's built-in `gps-dbus` service through a virtual serial port. From there, Venus OS publishes the position to the VRM portal automatically.

```
Starlink dish ──gRPC──> grpcurl ──JSON──> starfixvenus.py ──NMEA──> virtual serial ──> gps-dbus ──> D-Bus ──> VRM
```

Everything runs on the GX device itself. No extra hardware or services required.

## Prerequisites

- A Victron GX device running Venus OS (Cerbo GX, Venus GX, etc.) with root/SSH access
- A Starlink dish with **"Allow access on local network"** enabled in the Starlink app
- The GX device must be on the Starlink network (connected via Ethernet or Wi-Fi)

## Installation

### 1. Build `grpcurl` for your platform

You need to cross-compile [grpcurl](https://github.com/fullstorydev/grpcurl) for your GX device's architecture. The Cerbo GX uses armv7l (ARM 32-bit). You can check yours with `uname -m` over SSH.

Requires [Go](https://go.dev/dl/) installed on your build machine:

```bash
git clone https://github.com/fullstorydev/grpcurl.git
cd grpcurl/cmd/grpcurl

# For Cerbo GX (armv7l):
GOOS=linux GOARCH=arm GOARM=7 go build -o grpcurl .

# For other platforms, adjust GOOS/GOARCH accordingly, e.g.:
# GOOS=linux GOARCH=arm64 go build -o grpcurl .   # 64-bit ARM (aarch64)
# GOOS=linux GOARCH=amd64 go build -o grpcurl .   # x86_64
```

Verify the binary is correct:

```bash
file grpcurl
# Should show: ELF 32-bit LSB executable, ARM, ... for armv7l
```

### 2. Copy files to the GX device

```bash
scp grpcurl root@<cerbo-ip>:/data/starlink-gps/grpcurl
scp starfixvenus.py root@<cerbo-ip>:/data/starlink-gps/starfixvenus.py
```

### 3. Set permissions

```bash
ssh root@<cerbo-ip>
chmod +x /data/starlink-gps/grpcurl
chmod +x /data/starlink-gps/starfixvenus.py
```

### 4. Test connectivity

```bash
./starfixvenus.py test
```

This will fetch GPS from the Starlink dish and display the coordinates and generated NMEA sentences. If it fails, check that:
- "Allow access on local network" is enabled in the Starlink app
- The dish is reachable at `192.168.100.1:9200`
- The `grpcurl` binary is executable

### 5. Start the bridge

```bash
./starfixvenus.py -v start
```

Check VRM — your GPS position should appear within a minute.

### 6. Auto-start on boot

Add to `/data/rc.local` (create the file if it doesn't exist):

```bash
#!/bin/bash
/data/starlink-gps/starfixvenus.py start &
```

Make it executable:

```bash
chmod +x /data/rc.local
```

The script will survive reboots and Venus OS firmware updates (the `/data` partition is persistent).

## Usage

```
starfixvenus.py [-v | -d] {start,stop,test}

Commands:
  start     Start the GPS bridge (runs in foreground)
  stop      Stop all GPS services
  test      Test fetching GPS from Starlink

Options:
  -v        Verbose output
  -d        Debug output
  --host    Starlink dish IP (default: 192.168.100.1)
  --port    gRPC port (default: 9200)
  --interval  Poll interval in seconds (default: 30)
```

## Configuration

Edit the config dicts at the top of `starfixvenus.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `host` | `192.168.100.1` | Starlink dish IP |
| `grpc_port` | `9200` | Starlink gRPC port |
| `poll_interval` | `30` | Seconds between GPS polls |
| `write_interval` | `1` | NMEA write cadence (seconds) |
| `virtual_device` | `/dev/ttyACM0` | Virtual serial read side |
| `write_device` | `/tmp/starlink_gps_write` | Virtual serial write side |

## Troubleshooting

**GPS not showing on VRM**
- Run `./starfixvenus.py -d start` for debug logs
- Ensure no other GPS device or gps-dbus instance is running (`killall gps_dbus`)
- Verify the virtual serial device exists: `ls -la /dev/ttyACM0`

**grpcurl fails / times out**
- Confirm the dish is reachable: `ping 192.168.100.1`
- Ensure "Allow access on local network" is ON in the Starlink app
- Check that the grpcurl binary is the correct architecture (`file /data/starlink-gps/grpcurl`)

**gps-dbus exits immediately**
- Another gps-dbus process may be running — stop it first: `./starfixvenus.py stop`
- Check that `/dev/ttyACM0` isn't already in use by a physical USB device

## How it works (detailed)

1. **GPS polling**: Uses `grpcurl` to call `SpaceX.API.Device.Device/Handle` with `{"getLocation":{}}` over gRPC. The Starlink dish returns latitude, longitude, and altitude.

2. **NMEA generation**: Converts decimal-degree coordinates into standard NMEA 0183 sentences (GPGGA for position/altitude, GPRMC for navigation data), complete with checksums.

3. **Virtual serial**: Uses `socat` to create a PTY pair. One end acts as `/dev/ttyACM0` (the "GPS device"), the other is the write endpoint.

4. **Victron integration**: Launches Victron's native `gps-dbus` binary pointed at the virtual serial device. This is the same service that handles real GPS hardware — it reads NMEA, publishes to D-Bus, and VRM picks it up.

5. **Health monitoring**: The main loop watches both `socat` and `gps-dbus` and restarts them if they crash.

## Credits

Inspired by [victron-gps-mqtt-bridge](https://github.com/octaviospain/victron-gps-mqtt-bridge) — this project eliminates the need for an external MQTT broker by using Victron's native GPS service directly.

## License

MIT
