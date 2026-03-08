#!/usr/bin/env python3
"""
StarfixVenus — Starlink GPS to Victron GX Bridge
===================================================
All-in-one script that polls GPS from a Starlink dish and feeds it into
Victron's native gps-dbus service via a virtual serial device (NMEA).

Requirements on the GX:
  - grpcurl binary at /data/starlink-gps/grpcurl (cross-compiled for armv7)
  - socat (pre-installed on Venus OS)
  - Python 3 (pre-installed on Venus OS)

Install on Victron GX:
  1. Copy grpcurl (armv7) to /data/starlink-gps/grpcurl
  2. Copy this script to /data/starlink-gps/starfixvenus.py
  3. chmod +x /data/starlink-gps/starfixvenus.py
  4. chmod +x /data/starlink-gps/grpcurl
  5. Test: ./starfixvenus.py test
  6. Start: ./starfixvenus.py -v start
  7. Add to /data/rc.local for auto-start on boot

Architecture:
  Starlink dish (192.168.100.1:9200)
    -> grpcurl (gRPC over HTTP/2)
    -> Parse JSON response
    -> Generate NMEA 0183 sentences (GPGGA + GPRMC)
    -> Write to virtual serial device (socat PTY pair)
    -> Victron gps-dbus reads virtual serial
    -> D-Bus -> VRM Portal

Based on the approach from:
  https://github.com/octaviospain/victron-gps-mqtt-bridge
"""

import argparse
import datetime
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

# ============================================================================
# CONFIGURATION
# ============================================================================

STARLINK_CONFIG = {
    'host': '192.168.100.1',        # Starlink dish IP
    'grpc_port': 9200,              # gRPC port (HTTP/2)
    'poll_interval': 30,            # seconds between GPS polls
}

NMEA_CONFIG = {
    'write_interval': 1,            # write NMEA sentence every N seconds
    'virtual_device': '/dev/ttyACM0',
    'write_device': '/tmp/starlink_gps_write',
}

# Path to the grpcurl binary (cross-compiled for armv7)
GRPCURL_BINARY = '/data/starlink-gps/grpcurl'

# Victron's native GPS service binary
GPS_DBUS_BINARY = '/opt/victronenergy/gps-dbus/gps_dbus'


# ============================================================================
# STARLINK GPS POLLER
# ============================================================================

class StarlinkGPS:
    """Polls GPS coordinates from the Starlink dish via grpcurl."""

    def __init__(self, host=None, port=None):
        self.host = host or STARLINK_CONFIG['host']
        self.port = port or STARLINK_CONFIG['grpc_port']
        self.logger = logging.getLogger('starlink')

    def fetch_location(self):
        """
        Fetch GPS location from Starlink dish using grpcurl.
        Returns (lat, lon, alt) or (None, None, None) on failure.
        """
        try:
            target = f'{self.host}:{self.port}'
            result = subprocess.run(
                [
                    GRPCURL_BINARY,
                    '-plaintext',
                    '-emit-defaults',
                    '-d', '{"getLocation":{}}',
                    target,
                    'SpaceX.API.Device.Device/Handle',
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )

            if result.returncode != 0:
                self.logger.error(f'grpcurl failed: {result.stderr.strip()}')
                return None, None, None

            data = json.loads(result.stdout)
            lla = data.get('getLocation', {}).get('lla', {})
            lat = lla.get('lat')
            lon = lla.get('lon')
            alt = lla.get('alt', 0.0)

            if lat is not None and lon is not None and (lat != 0.0 or lon != 0.0):
                self.logger.info(f'GPS: lat={lat:.6f} lon={lon:.6f} alt={alt:.1f}')
                return lat, lon, alt

            self.logger.warning('GPS returned zero coordinates')
            return None, None, None

        except json.JSONDecodeError as e:
            self.logger.error(f'Failed to parse grpcurl output: {e}')
            return None, None, None
        except subprocess.TimeoutExpired:
            self.logger.error('grpcurl timed out')
            return None, None, None
        except Exception as e:
            self.logger.error(f'Starlink poll failed: {e}')
            return None, None, None


# ============================================================================
# NMEA 0183 SENTENCE GENERATORS
# ============================================================================

def nmea_checksum(sentence):
    """Calculate NMEA 0183 checksum (XOR of all chars between $ and *)."""
    chk = 0
    for ch in sentence:
        chk ^= ord(ch)
    return format(chk, '02X')


def make_gpgga(lat, lon, alt):
    """
    Generate a GPGGA NMEA sentence from decimal degree coordinates.

    Format: $GPGGA,hhmmss.ss,ddmm.mmmm,N,dddmm.mmmm,E,1,08,0.9,alt,M,0.0,M,,*XX
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    time_str = now.strftime('%H%M%S.00')

    # Latitude: decimal degrees -> DDMM.MMMM
    lat_dir = 'N' if lat >= 0 else 'S'
    lat_abs = abs(lat)
    lat_deg = int(lat_abs)
    lat_min = (lat_abs - lat_deg) * 60
    lat_str = f'{lat_deg:02d}{lat_min:07.4f}'

    # Longitude: decimal degrees -> DDDMM.MMMM
    lon_dir = 'E' if lon >= 0 else 'W'
    lon_abs = abs(lon)
    lon_deg = int(lon_abs)
    lon_min = (lon_abs - lon_deg) * 60
    lon_str = f'{lon_deg:03d}{lon_min:07.4f}'

    alt_str = f'{alt:.1f}' if alt is not None else '0.0'

    body = (
        f'GPGGA,{time_str},{lat_str},{lat_dir},{lon_str},{lon_dir},'
        f'1,08,0.9,{alt_str},M,0.0,M,,'
    )

    checksum = nmea_checksum(body)
    return f'${body}*{checksum}\r\n'


def make_gprmc(lat, lon):
    """
    Generate a GPRMC NMEA sentence (recommended minimum navigation data).

    Format: $GPRMC,hhmmss.ss,A,ddmm.mmmm,N,dddmm.mmmm,E,0.0,0.0,ddmmyy,,,A*XX
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    time_str = now.strftime('%H%M%S.00')
    date_str = now.strftime('%d%m%y')

    lat_dir = 'N' if lat >= 0 else 'S'
    lat_abs = abs(lat)
    lat_deg = int(lat_abs)
    lat_min = (lat_abs - lat_deg) * 60
    lat_str = f'{lat_deg:02d}{lat_min:07.4f}'

    lon_dir = 'E' if lon >= 0 else 'W'
    lon_abs = abs(lon)
    lon_deg = int(lon_abs)
    lon_min = (lon_abs - lon_deg) * 60
    lon_str = f'{lon_deg:03d}{lon_min:07.4f}'

    body = (
        f'GPRMC,{time_str},A,{lat_str},{lat_dir},{lon_str},{lon_dir},'
        f'0.0,0.0,{date_str},,,A'
    )

    checksum = nmea_checksum(body)
    return f'${body}*{checksum}\r\n'


# ============================================================================
# VIRTUAL SERIAL + GPS-DBUS BRIDGE
# ============================================================================

class GPSBridge:
    """
    Manages the full GPS pipeline:
      1. Polls Starlink for GPS coordinates (via grpcurl)
      2. Creates a virtual serial device pair (via socat)
      3. Writes NMEA sentences to the virtual serial device
      4. Starts Victron's native gps-dbus service to read from it
    """

    def __init__(self, starlink, poll_interval=None, nmea_interval=None):
        self.starlink = starlink
        self.poll_interval = poll_interval or STARLINK_CONFIG['poll_interval']
        self.nmea_interval = nmea_interval or NMEA_CONFIG['write_interval']
        self.logger = logging.getLogger('bridge')

        # Current GPS position
        self.lat = None
        self.lon = None
        self.alt = None
        self.last_update = 0

        # Subprocess handles
        self.socat_proc = None
        self.gps_dbus_proc = None
        self.running = False
        self._lock = threading.Lock()

    def start(self):
        """Start the full GPS bridge pipeline."""
        self.running = True

        # Fetch initial GPS position — retry indefinitely
        self.logger.info('Fetching initial GPS position from Starlink...')
        attempt = 0
        while self.running:
            attempt += 1
            lat, lon, alt = self.starlink.fetch_location()
            if lat is not None:
                self.lat, self.lon, self.alt = lat, lon, alt
                self.last_update = time.time()
                self.logger.info(f'Got initial position: {lat:.6f}, {lon:.6f}')
                break
            # Back off gradually: 5s, 10s, 15s, ... up to 60s max
            wait = min(5 * attempt, 60)
            self.logger.warning(f'Attempt {attempt} failed, retrying in {wait}s...')
            time.sleep(wait)

        # Start virtual serial device
        if not self._start_socat():
            return False

        # Start NMEA writer thread
        self._nmea_thread = threading.Thread(target=self._nmea_writer_loop, daemon=True)
        self._nmea_thread.start()

        # Give NMEA writer time to start producing data — gps-dbus needs
        # to see data flowing when it connects or it will timeout
        time.sleep(5)

        # Start Victron's native GPS service
        if not self._start_gps_dbus():
            return False

        # Start background GPS poller thread
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        self.logger.info('GPS bridge started successfully!')
        return True

    def stop(self):
        """Stop all services and clean up."""
        self.running = False

        if self.gps_dbus_proc:
            self.logger.info('Stopping gps-dbus...')
            try:
                self.gps_dbus_proc.terminate()
                self.gps_dbus_proc.wait(timeout=5)
            except Exception:
                self.gps_dbus_proc.kill()
            self.gps_dbus_proc = None

        if self.socat_proc:
            self.logger.info('Stopping socat...')
            try:
                self.socat_proc.terminate()
                self.socat_proc.wait(timeout=5)
            except Exception:
                self.socat_proc.kill()
            self.socat_proc = None

        # Clean up symlinks
        for path in [NMEA_CONFIG['virtual_device'], NMEA_CONFIG['write_device']]:
            if os.path.islink(path) or os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

        self.logger.info('GPS bridge stopped.')

    def _start_socat(self):
        """Start socat to create a virtual serial device pair."""
        dev_link = NMEA_CONFIG['virtual_device']
        write_link = NMEA_CONFIG['write_device']

        # Clean up old symlinks
        for path in [dev_link, write_link]:
            if os.path.islink(path) or os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

        try:
            # Create a PTY pair:
            #   - dev_link: gps-dbus reads from this side
            #   - write_link: we write NMEA sentences to this side
            self.socat_proc = subprocess.Popen(
                [
                    'socat',
                    f'PTY,raw,echo=0,link={dev_link}',
                    f'PTY,raw,echo=0,link={write_link}',
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Wait for both devices to appear
            for _ in range(20):
                if os.path.exists(dev_link) and os.path.exists(write_link):
                    self.logger.info(f'Virtual serial device created: {dev_link}')
                    return True
                time.sleep(0.5)

            self.logger.error('Timeout waiting for virtual serial device')
            return False

        except FileNotFoundError:
            self.logger.error('socat not found — is it installed?')
            return False
        except Exception as e:
            self.logger.error(f'Failed to start socat: {e}')
            return False

    def _start_gps_dbus(self):
        """Start Victron's native gps-dbus service pointing at our virtual device.

        Uses the same flags as /opt/victronenergy/gps-dbus/start-gps.sh:
          gps_dbus -v --banner --dbus system --timeout 2 -s /dev/<tty> -b <baud>
        """
        dev_link = NMEA_CONFIG['virtual_device']

        if not os.path.exists(GPS_DBUS_BINARY):
            self.logger.error(f'gps-dbus not found at {GPS_DBUS_BINARY}')
            return False

        # Kill any existing gps-dbus instances to avoid conflicts
        subprocess.run(['killall', 'gps_dbus'], capture_output=True)
        time.sleep(1)

        try:
            self.gps_dbus_proc = subprocess.Popen(
                [
                    GPS_DBUS_BINARY,
                    '-v', '--banner', '--dbus', 'system',
                    '--timeout', '5',
                    '-s', dev_link,
                    '-b', '9600',
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # gps-dbus may take a moment to connect — give it time
            # but don't fail if it hasn't printed anything yet
            time.sleep(3)

            if self.gps_dbus_proc.poll() is not None:
                stderr = self.gps_dbus_proc.stderr.read().decode()
                self.logger.error(f'gps-dbus exited immediately: {stderr}')
                return False

            self.logger.info(f'gps-dbus started (PID {self.gps_dbus_proc.pid})')
            return True

        except Exception as e:
            self.logger.error(f'Failed to start gps-dbus: {e}')
            return False

    def _nmea_writer_loop(self):
        """Continuously write NMEA sentences to the virtual serial device.

        Uses os.open with O_WRONLY|O_NONBLOCK for reliable PTY writes.
        """
        write_dev = NMEA_CONFIG['write_device']

        while self.running:
            try:
                with self._lock:
                    lat, lon, alt = self.lat, self.lon, self.alt

                if lat is not None and lon is not None:
                    gpgga = make_gpgga(lat, lon, alt)
                    gprmc = make_gprmc(lat, lon)
                    data = gpgga.encode() + gprmc.encode()

                    fd = os.open(write_dev, os.O_WRONLY | os.O_NONBLOCK)
                    try:
                        os.write(fd, data)
                    finally:
                        os.close(fd)

            except Exception as e:
                self.logger.debug(f'NMEA write error: {e}')

            time.sleep(self.nmea_interval)

    def _poll_loop(self):
        """Periodically poll Starlink for updated GPS coordinates."""
        while self.running:
            time.sleep(self.poll_interval)

            try:
                lat, lon, alt = self.starlink.fetch_location()
                if lat is not None:
                    with self._lock:
                        self.lat = lat
                        self.lon = lon
                        self.alt = alt
                        self.last_update = time.time()
            except Exception as e:
                self.logger.error(f'Poll error: {e}')

    def status(self):
        """Print current status."""
        print('\n=== Starlink GPS Bridge Status ===')

        socat_ok = self.socat_proc and self.socat_proc.poll() is None
        print(f'Socat:       {"RUNNING" if socat_ok else "STOPPED"}')

        dbus_ok = self.gps_dbus_proc and self.gps_dbus_proc.poll() is None
        print(f'gps-dbus:    {"RUNNING" if dbus_ok else "STOPPED"}')

        dev_link = NMEA_CONFIG['virtual_device']
        dev_ok = os.path.exists(dev_link)
        print(f'Device:      {dev_link} {"(present)" if dev_ok else "(missing)"}')

        with self._lock:
            if self.lat is not None:
                age = time.time() - self.last_update
                print(f'Position:    {self.lat:.6f}, {self.lon:.6f} (alt: {self.alt:.1f}m)')
                print(f'Last update: {age:.0f}s ago')
            else:
                print('Position:    No data')

        print()


# ============================================================================
# MAIN
# ============================================================================

def setup_logging(verbose=False, debug=False):
    """Configure logging — stderr only, errors by default."""
    level = logging.DEBUG if debug else (logging.INFO if verbose else logging.ERROR)

    formatter = logging.Formatter('[%(levelname)s] %(message)s')

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)


def main():
    parser = argparse.ArgumentParser(
        description='StarfixVenus — Starlink GPS to Victron GX Bridge',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s start              Start the GPS bridge
  %(prog)s -v start           Start with verbose output
  %(prog)s -d start           Start with debug output
  %(prog)s test               Test fetching GPS from Starlink
  %(prog)s stop               Stop all GPS services

Configuration:
  Edit the STARLINK_CONFIG section at the top of this script.
  Default: polls Starlink every 30s, writes NMEA every 2s.
        """
    )

    parser.add_argument('command', choices=['start', 'stop', 'test'],
                        help='Command to run')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose output')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='Debug output')
    parser.add_argument('--host', default=STARLINK_CONFIG['host'],
                        help=f'Starlink dish IP (default: {STARLINK_CONFIG["host"]})')
    parser.add_argument('--port', type=int, default=STARLINK_CONFIG['grpc_port'],
                        help=f'Starlink gRPC port (default: {STARLINK_CONFIG["grpc_port"]})')
    parser.add_argument('--interval', type=int, default=STARLINK_CONFIG['poll_interval'],
                        help=f'Poll interval in seconds (default: {STARLINK_CONFIG["poll_interval"]})')

    args = parser.parse_args()
    setup_logging(verbose=args.verbose, debug=args.debug)
    logger = logging.getLogger('main')

    # Verify grpcurl binary exists
    if not os.path.exists(GRPCURL_BINARY):
        print(f'ERROR: grpcurl not found at {GRPCURL_BINARY}')
        print('Download or cross-compile grpcurl for armv7 and place it there.')
        sys.exit(1)

    starlink = StarlinkGPS(host=args.host, port=args.port)

    # ---- TEST command ----
    if args.command == 'test':
        print('Testing Starlink GPS fetch...')
        lat, lon, alt = starlink.fetch_location()
        if lat is not None:
            print(f'  Latitude:  {lat:.6f}')
            print(f'  Longitude: {lon:.6f}')
            print(f'  Altitude:  {alt:.1f}m')
            print()
            print('NMEA sentences:')
            print(f'  {make_gpgga(lat, lon, alt).strip()}')
            print(f'  {make_gprmc(lat, lon).strip()}')
            print()
            print('Starlink GPS is working!')
        else:
            print('  FAILED — could not fetch GPS from Starlink.')
            print('  Check:')
            print('    1. "Allow access on local network" is ON in Starlink app')
            print(f'    2. Starlink dish is reachable at {args.host}:{args.port}')
            print(f'    3. grpcurl binary exists at {GRPCURL_BINARY}')
            sys.exit(1)
        return

    # ---- STOP command ----
    if args.command == 'stop':
        logger.info('Stopping GPS services...')
        subprocess.run(['killall', 'gps_dbus'], capture_output=True)
        subprocess.run(['killall', 'socat'], capture_output=True)

        for path in [NMEA_CONFIG['virtual_device'], NMEA_CONFIG['write_device']]:
            if os.path.islink(path) or os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

        print('GPS services stopped.')
        return

    # ---- START command ----
    if args.command == 'start':
        bridge = GPSBridge(starlink, poll_interval=args.interval)

        # Handle signals for clean shutdown
        def signal_handler(sig, frame):
            logger.info('Received signal, shutting down...')
            bridge.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        if not bridge.start():
            logger.error('Failed to start GPS bridge')
            bridge.stop()
            sys.exit(1)

        bridge.status()

        # Keep main thread alive and monitor health
        while True:
            try:
                time.sleep(60)

                # Health check: restart socat if it crashed
                if bridge.socat_proc and bridge.socat_proc.poll() is not None:
                    logger.warning('socat died, restarting...')
                    bridge._start_socat()
                    time.sleep(2)
                    bridge._start_gps_dbus()

                # Health check: restart gps-dbus if it crashed
                if bridge.gps_dbus_proc and bridge.gps_dbus_proc.poll() is not None:
                    logger.warning('gps-dbus died, restarting...')
                    bridge._start_gps_dbus()

            except KeyboardInterrupt:
                break

        bridge.stop()


if __name__ == '__main__':
    main()
