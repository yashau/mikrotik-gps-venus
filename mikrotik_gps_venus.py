#!/usr/bin/env python3
"""
MikroTik GPS to Victron GX Bridge
=================================
Polls GPS coordinates from a MikroTik router over the RouterOS API and feeds
them into Victron's native gps-dbus service through a virtual serial device.

Requirements on the GX:
  - Python 3 (pre-installed on Venus OS)
  - socat (pre-installed on Venus OS)
  - RouterOS API enabled on the MikroTik router

Architecture:
  MikroTik router (/system/gps/monitor over RouterOS API)
    -> Parse GPS monitor response
    -> Generate NMEA 0183 sentences (GPGGA + GPRMC)
    -> Write to virtual serial device (socat PTY pair)
    -> Victron gps-dbus reads virtual serial
    -> D-Bus -> VRM Portal
"""

import argparse
import datetime
import hashlib
import logging
import os
import re
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV_FILE = os.path.join(SCRIPT_DIR, '.env')

GPS_DBUS_BINARY_DEFAULT = '/opt/victronenergy/gps-dbus/gps_dbus'


# ============================================================================
# CONFIGURATION
# ============================================================================


class AppConfig:
    """Runtime configuration loaded from .env, environment, and CLI overrides."""

    def __init__(self):
        self.env_file = DEFAULT_ENV_FILE

        self.mikrotik_host = ''
        self.mikrotik_port = 8728
        self.mikrotik_user = 'admin'
        self.mikrotik_password = ''
        self.mikrotik_tls = False
        self.mikrotik_tls_verify = False
        self.mikrotik_timeout = 10
        self.mikrotik_gps_format = 'dd'
        self.require_valid_fix = True
        self.max_data_age = 0
        self.reject_zero_coordinates = True

        self.gps_source = 'mikrotik'
        self.manual_lat = None
        self.manual_lon = None
        self.manual_alt = 0.0

        self.poll_interval = 30
        self.startup_timeout = 0
        self.nmea_interval = 1
        self.virtual_device = '/dev/ttyACM0'
        self.write_device = '/tmp/mikrotik_gps_write'
        self.gps_dbus_binary = GPS_DBUS_BINARY_DEFAULT

    @property
    def api_mode(self):
        return 'api-ssl' if self.mikrotik_tls else 'api'

    def masked_password(self):
        if not self.mikrotik_password:
            return '(empty)'
        return '*' * min(len(self.mikrotik_password), 8)


def load_env_file(path):
    """Load a small .env file without external dependencies."""
    values = {}
    if not path or not os.path.exists(path):
        return values

    with open(path, encoding='utf-8') as env_file:
        for line_number, raw_line in enumerate(env_file, start=1):
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[len('export ') :].strip()
            if '=' not in line:
                raise ValueError(f'{path}:{line_number}: expected KEY=VALUE')

            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()
            if not key:
                raise ValueError(f'{path}:{line_number}: empty key')

            try:
                parsed = shlex.split(value, comments=True, posix=True)
                values[key] = parsed[0] if parsed else ''
            except ValueError:
                values[key] = value.strip('"').strip("'")

    return values


def env_value(file_env, name, default=None, aliases=None):
    names = [name] + (aliases or [])
    for candidate in names:
        if candidate in os.environ:
            return os.environ[candidate]
    for candidate in names:
        if candidate in file_env:
            return file_env[candidate]
    return default


def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ('1', 'true', 'yes', 'y', 'on', 'enable', 'enabled'):
        return True
    if text in ('0', 'false', 'no', 'n', 'off', 'disable', 'disabled'):
        return False
    return default


def parse_int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_float(value, default=None):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def apply_env(config, file_env):
    config.gps_source = env_value(file_env, 'GPS_SOURCE', config.gps_source).strip().lower()
    config.mikrotik_host = env_value(file_env, 'MIKROTIK_HOST', config.mikrotik_host)
    config.mikrotik_port = parse_int(
        env_value(file_env, 'MIKROTIK_PORT', config.mikrotik_port),
        config.mikrotik_port,
    )
    config.mikrotik_user = env_value(file_env, 'MIKROTIK_USER', config.mikrotik_user)
    config.mikrotik_password = env_value(
        file_env,
        'MIKROTIK_PASSWORD',
        config.mikrotik_password,
        aliases=['MIKROTIK_PASS'],
    )
    config.mikrotik_tls = parse_bool(
        env_value(
            file_env,
            'MIKROTIK_TLS',
            config.mikrotik_tls,
            aliases=['MIKROTIK_SSL', 'MIKROTIK_USE_SSL'],
        ),
        config.mikrotik_tls,
    )
    config.mikrotik_tls_verify = parse_bool(
        env_value(file_env, 'MIKROTIK_TLS_VERIFY', config.mikrotik_tls_verify),
        config.mikrotik_tls_verify,
    )
    config.mikrotik_timeout = parse_int(
        env_value(file_env, 'MIKROTIK_TIMEOUT', config.mikrotik_timeout),
        config.mikrotik_timeout,
    )
    config.mikrotik_gps_format = env_value(
        file_env,
        'MIKROTIK_GPS_FORMAT',
        config.mikrotik_gps_format,
    )
    config.require_valid_fix = parse_bool(
        env_value(file_env, 'MIKROTIK_REQUIRE_VALID', config.require_valid_fix),
        config.require_valid_fix,
    )
    config.max_data_age = parse_int(
        env_value(file_env, 'MIKROTIK_MAX_DATA_AGE', config.max_data_age),
        config.max_data_age,
    )
    config.reject_zero_coordinates = parse_bool(
        env_value(file_env, 'REJECT_ZERO_COORDINATES', config.reject_zero_coordinates),
        config.reject_zero_coordinates,
    )

    config.manual_lat = parse_float(env_value(file_env, 'MANUAL_LAT', config.manual_lat))
    config.manual_lon = parse_float(env_value(file_env, 'MANUAL_LON', config.manual_lon))
    config.manual_alt = parse_float(
        env_value(file_env, 'MANUAL_ALT', config.manual_alt),
        config.manual_alt,
    )

    config.poll_interval = parse_int(
        env_value(file_env, 'POLL_INTERVAL', config.poll_interval),
        config.poll_interval,
    )
    config.startup_timeout = parse_int(
        env_value(file_env, 'STARTUP_TIMEOUT', config.startup_timeout),
        config.startup_timeout,
    )
    config.nmea_interval = parse_int(
        env_value(file_env, 'NMEA_WRITE_INTERVAL', config.nmea_interval),
        config.nmea_interval,
    )
    config.virtual_device = env_value(file_env, 'NMEA_VIRTUAL_DEVICE', config.virtual_device)
    config.write_device = env_value(file_env, 'NMEA_WRITE_DEVICE', config.write_device)
    config.gps_dbus_binary = env_value(file_env, 'GPS_DBUS_BINARY', config.gps_dbus_binary)


def apply_cli(config, args):
    if args.host:
        config.mikrotik_host = args.host
    if args.port:
        config.mikrotik_port = args.port
    if args.user:
        config.mikrotik_user = args.user
    if args.password is not None:
        config.mikrotik_password = args.password
    if args.tls:
        config.mikrotik_tls = True
    if args.no_tls:
        config.mikrotik_tls = False
    if args.tls_verify:
        config.mikrotik_tls_verify = True
    if args.no_tls_verify:
        config.mikrotik_tls_verify = False
    if args.interval:
        config.poll_interval = args.interval
    if args.startup_timeout is not None:
        config.startup_timeout = args.startup_timeout
    if args.gps_format is not None:
        config.mikrotik_gps_format = args.gps_format
    if args.source:
        config.gps_source = args.source
    if args.manual:
        config.gps_source = 'manual'
    if args.lat is not None:
        config.manual_lat = args.lat
        config.gps_source = 'manual'
    if args.lon is not None:
        config.manual_lon = args.lon
        config.gps_source = 'manual'
    if args.alt is not None:
        config.manual_alt = args.alt


RUNTIME_CONFIG_FIELDS = (
    'mikrotik_host',
    'mikrotik_port',
    'mikrotik_user',
    'mikrotik_password',
    'mikrotik_tls',
    'mikrotik_tls_verify',
    'mikrotik_timeout',
    'mikrotik_gps_format',
    'require_valid_fix',
    'max_data_age',
    'reject_zero_coordinates',
    'gps_source',
    'manual_lat',
    'manual_lon',
    'manual_alt',
    'poll_interval',
    'startup_timeout',
    'nmea_interval',
)

RESTART_ONLY_CONFIG_FIELDS = (
    'virtual_device',
    'write_device',
    'gps_dbus_binary',
)


def build_config_from_env_and_args(env_file, args):
    config = AppConfig()
    config.env_file = env_file
    apply_env(config, load_env_file(env_file))
    apply_cli(config, args)
    return config


def apply_runtime_config(target, source):
    changed = []
    for field in RUNTIME_CONFIG_FIELDS:
        old_value = getattr(target, field)
        new_value = getattr(source, field)
        if old_value != new_value:
            setattr(target, field, new_value)
            changed.append(field)
    return changed


class ConfigReloader:
    """Reloads .env changes while keeping restart-only device settings stable."""

    def __init__(self, config, args):
        self.config = config
        self.args = args
        self.logger = logging.getLogger('config')
        self.last_signature = self._env_signature()

    def load_if_changed(self):
        signature = self._env_signature()
        if signature == self.last_signature:
            return None

        self.last_signature = signature
        try:
            new_config = build_config_from_env_and_args(self.config.env_file, self.args)
        except Exception as exc:
            self.logger.error(f'Ignoring .env reload; could not parse config: {exc}')
            return None

        for field in RESTART_ONLY_CONFIG_FIELDS:
            old_value = getattr(self.config, field)
            new_value = getattr(new_config, field)
            if old_value != new_value:
                self.logger.warning(
                    f'Ignoring {field} change from .env; restart required for device paths'
                )
                setattr(new_config, field, old_value)

        return new_config

    def _env_signature(self):
        try:
            stat = os.stat(self.config.env_file)
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size


# ============================================================================
# ROUTEROS API CLIENT
# ============================================================================


class RouterOSAPIError(Exception):
    """Raised when the RouterOS API returns an error or cannot be reached."""


class RouterOSAPI:
    """Minimal RouterOS API client implemented with Python stdlib only."""

    def __init__(self, host, port, user, password, use_tls=False, verify_tls=False, timeout=10):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.use_tls = use_tls
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def connect(self):
        self._open_socket()
        self._login()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _open_socket(self):
        try:
            raw_sock = socket.create_connection((self.host, self.port), self.timeout)
            raw_sock.settimeout(self.timeout)
        except OSError as exc:
            raise RouterOSAPIError(f'Cannot connect to {self.host}:{self.port}: {exc}')

        if not self.use_tls:
            self.sock = raw_sock
            return

        try:
            if self.verify_tls:
                context = ssl.create_default_context()
            else:
                context = ssl._create_unverified_context()
            self.sock = context.wrap_socket(raw_sock, server_hostname=self.host)
            self.sock.settimeout(self.timeout)
        except OSError as exc:
            raw_sock.close()
            raise RouterOSAPIError(f'TLS handshake failed: {exc}')

    def _login(self):
        try:
            self.command(
                '/login',
                {
                    'name': self.user,
                    'password': self.password,
                },
            )
            return
        except RouterOSAPIError as modern_error:
            self.close()
            self._open_socket()
            try:
                self._legacy_login()
                return
            except RouterOSAPIError as legacy_error:
                self.close()
                raise RouterOSAPIError(
                    f'Login failed: {modern_error}; legacy login also failed: {legacy_error}'
                )

    def _legacy_login(self):
        replies = self._talk_raw(['/login'])
        ret = None
        for sentence in replies:
            if sentence and sentence[0] == '!done':
                ret = self._parse_sentence(sentence).get('ret')
                break
        if not ret:
            raise RouterOSAPIError('legacy challenge was not returned')

        challenge = bytes.fromhex(ret)
        digest = hashlib.md5(b'\x00' + self.password.encode('utf-8') + challenge).hexdigest()
        self.command(
            '/login',
            {
                'name': self.user,
                'response': '00' + digest,
            },
        )

    def command(self, command, attrs=None):
        words = [command]
        for key, value in (attrs or {}).items():
            if value is None:
                continue
            words.append(f'={key}={value}')

        try:
            replies = self._talk_raw(words)
        except OSError as exc:
            raise RouterOSAPIError(f'RouterOS API I/O failed: {exc}') from exc

        records = []
        traps = []

        for sentence in replies:
            if not sentence:
                continue
            reply_type = sentence[0]
            attrs = self._parse_sentence(sentence)
            if reply_type == '!re':
                records.append(attrs)
            elif reply_type == '!trap':
                traps.append(attrs)
            elif reply_type == '!fatal':
                message = attrs.get('message', 'fatal RouterOS API error')
                raise RouterOSAPIError(message)
            elif reply_type in ('!done', '!empty'):
                if traps:
                    raise RouterOSAPIError(self._format_traps(traps))
                return records

        if traps:
            raise RouterOSAPIError(self._format_traps(traps))
        raise RouterOSAPIError('RouterOS API response ended without !done')

    def _talk_raw(self, words):
        self._write_sentence(words)
        replies = []
        while True:
            sentence = self._read_sentence()
            replies.append(sentence)
            if sentence and sentence[0] in ('!done', '!fatal', '!empty'):
                return replies

    def _require_socket(self):
        if self.sock is None:
            raise RouterOSAPIError('RouterOS API socket is not connected')
        return self.sock

    def _write_sentence(self, words):
        sock = self._require_socket()
        try:
            for word in words:
                data = word.encode('utf-8')
                sock.sendall(self._encode_length(len(data)))
                sock.sendall(data)
            sock.sendall(b'\x00')
        except OSError as exc:
            raise RouterOSAPIError(f'RouterOS API write failed: {exc}') from exc

    def _read_sentence(self):
        words = []
        while True:
            length = self._read_length()
            if length == 0:
                return words
            data = self._recv_exact(length)
            words.append(data.decode('utf-8', errors='replace'))

    def _recv_exact(self, length):
        sock = self._require_socket()
        chunks = []
        remaining = length
        while remaining:
            try:
                chunk = sock.recv(remaining)
            except socket.timeout as exc:
                raise RouterOSAPIError(
                    f'RouterOS API read timed out after {self.timeout}s'
                ) from exc
            except OSError as exc:
                raise RouterOSAPIError(f'RouterOS API read failed: {exc}') from exc

            if not chunk:
                raise RouterOSAPIError('RouterOS API connection closed')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    def _read_length(self):
        first = self._recv_exact(1)[0]
        if first < 0x80:
            return first
        if first < 0xC0:
            second = self._recv_exact(1)[0]
            return ((first & 0x7F) << 8) | second
        if first < 0xE0:
            rest = self._recv_exact(2)
            return ((first & 0x3F) << 16) | (rest[0] << 8) | rest[1]
        if first < 0xF0:
            rest = self._recv_exact(3)
            return ((first & 0x1F) << 24) | (rest[0] << 16) | (rest[1] << 8) | rest[2]
        if first == 0xF0:
            rest = self._recv_exact(4)
            return int.from_bytes(rest, 'big')
        raise RouterOSAPIError('unsupported RouterOS API length control byte')

    def _encode_length(self, length):
        if length < 0x80:
            return bytes([length])
        if length < 0x4000:
            length |= 0x8000
            return length.to_bytes(2, 'big')
        if length < 0x200000:
            length |= 0xC00000
            return length.to_bytes(3, 'big')
        if length < 0x10000000:
            length |= 0xE0000000
            return length.to_bytes(4, 'big')
        return b'\xf0' + length.to_bytes(4, 'big')

    def _parse_sentence(self, sentence):
        attrs = {}
        for word in sentence[1:]:
            if word.startswith('='):
                body = word[1:]
                key, _, value = body.partition('=')
                attrs[key] = clean_routeros_value(value)
            elif word.startswith('.'):
                key, _, value = word.partition('=')
                attrs[key] = clean_routeros_value(value)
        return attrs

    def _format_traps(self, traps):
        messages = []
        for trap in traps:
            message = trap.get('message') or trap.get('category') or str(trap)
            messages.append(message)
        return '; '.join(messages)


# ============================================================================
# MIKROTIK GPS POLLER
# ============================================================================


class MikroTikGPS:
    """Poll GPS coordinates from /system/gps/monitor over RouterOS API."""

    name = 'MikroTik'

    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger('mikrotik')

    def fetch_location(self):
        """
        Return (lat, lon, alt) from the MikroTik router, or (None, None, None)
        when the API is unreachable or GPS does not have a usable fix.
        """
        try:
            with RouterOSAPI(
                self.config.mikrotik_host,
                self.config.mikrotik_port,
                self.config.mikrotik_user,
                self.config.mikrotik_password,
                use_tls=self.config.mikrotik_tls,
                verify_tls=self.config.mikrotik_tls_verify,
                timeout=self.config.mikrotik_timeout,
            ) as api:
                record = self._read_gps_record(api)
        except RouterOSAPIError as exc:
            self.logger.error(f'MikroTik API error: {exc}')
            return None, None, None

        if not record:
            self.logger.warning('MikroTik GPS monitor returned no data')
            return None, None, None

        valid = record.get('valid')
        if self.config.require_valid_fix and valid is not None and not parse_bool(valid):
            self.logger.warning(f'MikroTik GPS fix is not valid yet: valid={valid}')
            return None, None, None

        data_age = parse_int(record.get('data-age'), 0)
        if self.config.max_data_age > 0 and data_age > self.config.max_data_age:
            self.logger.warning(
                f'MikroTik GPS data is stale: data-age={data_age}s '
                f'(max {self.config.max_data_age}s)'
            )
            return None, None, None

        lat = parse_coordinate(record.get('latitude'), 'lat')
        lon = parse_coordinate(record.get('longitude'), 'lon')
        alt = parse_altitude(record.get('altitude'))

        if lat is None or lon is None:
            self.logger.warning(f'MikroTik GPS returned unusable coordinates: {record}')
            return None, None, None
        if not coordinates_are_usable(lat, lon, self.config.reject_zero_coordinates):
            self.logger.warning(f'MikroTik GPS returned bogus coordinates: lat={lat} lon={lon}')
            return None, None, None

        satellites = record.get('satellites', 'unknown')
        self.logger.info(f'GPS: lat={lat:.6f} lon={lon:.6f} alt={alt:.1f}m satellites={satellites}')
        return lat, lon, alt

    def _read_gps_record(self, api):
        attrs = {'once': ''}
        if self.config.mikrotik_gps_format:
            attrs['format'] = self.config.mikrotik_gps_format

        try:
            records = api.command('/system/gps/monitor', attrs)
        except RouterOSAPIError as exc:
            if 'format' not in str(exc).lower() or not self.config.mikrotik_gps_format:
                raise
            self.logger.debug('GPS monitor format argument was rejected; retrying without it')
            records = api.command('/system/gps/monitor', {'once': ''})

        return records[0] if records else None


class ManualGPS:
    """Fixed coordinate source for testing the Venus OS NMEA/gps-dbus path."""

    name = 'Manual'

    def __init__(self, lat, lon, alt=0.0):
        self.lat = lat
        self.lon = lon
        self.alt = alt
        self.logger = logging.getLogger('manual')

    def fetch_location(self):
        self.logger.info(f'GPS: lat={self.lat:.6f} lon={self.lon:.6f} alt={self.alt:.1f}m (manual)')
        return self.lat, self.lon, self.alt


def create_gps_source(config):
    if config.gps_source == 'manual':
        if config.manual_lat is None or config.manual_lon is None:
            raise ValueError('manual GPS source requires MANUAL_LAT/MANUAL_LON or --lat/--lon')
        if not coordinates_are_usable(
            config.manual_lat,
            config.manual_lon,
            config.reject_zero_coordinates,
        ):
            raise ValueError('manual coordinates are out of range or look like 0,0 bogus data')
        return ManualGPS(config.manual_lat, config.manual_lon, config.manual_alt)

    if config.gps_source == 'mikrotik':
        if not config.mikrotik_host.strip():
            raise ValueError('MIKROTIK_HOST or --host is required when GPS_SOURCE=mikrotik')
        return MikroTikGPS(config)

    raise ValueError('GPS_SOURCE must be "mikrotik" or "manual"')


def clean_routeros_value(value):
    """Remove RouterOS padding artifacts sometimes seen in GPS fields."""
    if value is None:
        return ''
    return str(value).replace('\x00', '').strip()


def coordinates_are_usable(lat, lon, reject_zero=True):
    if lat is None or lon is None:
        return False
    if not (-90 <= lat <= 90):
        return False
    if not (-180 <= lon <= 180):
        return False
    if reject_zero and abs(lat) < 0.000001 and abs(lon) < 0.000001:
        return False
    return True


def parse_coordinate(value, axis):
    """Parse decimal degrees or RouterOS ddmm monitor output into a float."""
    raw = clean_routeros_value(value)
    if not raw or raw.lower() == 'none':
        return None

    sign = -1 if any(direction in raw.upper() for direction in ('S', 'W')) else 1
    text = raw.upper()
    text = re.sub(r'[NSEW]', '', text)
    text = text.replace('DEG.', '').replace('DEG', '')
    text = text.replace(',', '.')
    number_match = re.search(r'-?\d+(?:\.\d+)?', text)
    if not number_match:
        return None

    try:
        number = float(number_match.group(0))
    except ValueError:
        return None

    if number < 0:
        sign = -1
    number = abs(number)
    max_degrees = 90 if axis == 'lat' else 180

    if number <= max_degrees:
        return sign * number

    degrees = int(number // 100)
    minutes = number - (degrees * 100)
    if minutes >= 60:
        return None

    coordinate = degrees + (minutes / 60.0)
    if coordinate > max_degrees:
        return None
    return sign * coordinate


def parse_altitude(value):
    raw = clean_routeros_value(value)
    if not raw or raw.lower() == 'none':
        return 0.0
    match = re.search(r'-?\d+(?:\.\d+)?', raw.replace(',', '.'))
    if not match:
        return 0.0
    return float(match.group(0))


# ============================================================================
# NMEA 0183 SENTENCE GENERATORS
# ============================================================================


def nmea_checksum(sentence):
    """Calculate NMEA 0183 checksum (XOR of all chars between $ and *)."""
    chk = 0
    for ch in sentence:
        chk ^= ord(ch)
    return format(chk, '02X')


def decimal_to_nmea(value, axis):
    direction = None
    if axis == 'lat':
        direction = 'N' if value >= 0 else 'S'
        width = 2
    else:
        direction = 'E' if value >= 0 else 'W'
        width = 3

    absolute = abs(value)
    degrees = int(absolute)
    minutes = (absolute - degrees) * 60
    return f'{degrees:0{width}d}{minutes:07.4f}', direction


def make_gpgga(lat, lon, alt):
    """
    Generate a GPGGA NMEA sentence from decimal degree coordinates.

    Format:
      $GPGGA,hhmmss.ss,ddmm.mmmm,N,dddmm.mmmm,E,1,08,0.9,alt,M,0.0,M,,*XX
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    time_str = now.strftime('%H%M%S.00')
    lat_str, lat_dir = decimal_to_nmea(lat, 'lat')
    lon_str, lon_dir = decimal_to_nmea(lon, 'lon')
    alt_str = f'{alt:.1f}' if alt is not None else '0.0'

    body = f'GPGGA,{time_str},{lat_str},{lat_dir},{lon_str},{lon_dir},1,08,0.9,{alt_str},M,0.0,M,,'
    return f'${body}*{nmea_checksum(body)}\r\n'


def make_gprmc(lat, lon):
    """
    Generate a GPRMC NMEA sentence (recommended minimum navigation data).

    Format:
      $GPRMC,hhmmss.ss,A,ddmm.mmmm,N,dddmm.mmmm,E,0.0,0.0,ddmmyy,,,A*XX
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    time_str = now.strftime('%H%M%S.00')
    date_str = now.strftime('%d%m%y')
    lat_str, lat_dir = decimal_to_nmea(lat, 'lat')
    lon_str, lon_dir = decimal_to_nmea(lon, 'lon')

    body = f'GPRMC,{time_str},A,{lat_str},{lat_dir},{lon_str},{lon_dir},0.0,0.0,{date_str},,,A'
    return f'${body}*{nmea_checksum(body)}\r\n'


# ============================================================================
# VIRTUAL SERIAL + GPS-DBUS BRIDGE
# ============================================================================


class GPSBridge:
    """
    Manages the full GPS pipeline:
      1. Polls MikroTik for GPS coordinates
      2. Creates a virtual serial device pair with socat
      3. Writes NMEA sentences to the virtual serial device
      4. Starts Victron's native gps-dbus service to read from it
    """

    def __init__(self, gps_source, config, config_reloader=None):
        self.gps_source = gps_source
        self.gps_source_name = getattr(gps_source, 'name', 'GPS')
        self.config = config
        self.config_reloader = config_reloader
        self.logger = logging.getLogger('bridge')

        self.lat = None
        self.lon = None
        self.alt = None
        self.last_update = 0

        self.socat_proc = None
        self.gps_dbus_proc = None
        self.running = False
        self._lock = threading.Lock()

    def _reload_config_if_changed(self):
        if not self.config_reloader:
            return False

        new_config = self.config_reloader.load_if_changed()
        if not new_config:
            return False

        try:
            create_gps_source(new_config)
        except ValueError as exc:
            self.logger.error(f'Ignoring .env reload; invalid GPS source config: {exc}')
            return False

        changed = apply_runtime_config(self.config, new_config)
        self.gps_source = create_gps_source(self.config)
        self.gps_source_name = getattr(self.gps_source, 'name', 'GPS')

        if changed:
            self.logger.info(f'Reloaded .env: {", ".join(changed)}')
        else:
            self.logger.info('Reloaded .env: no runtime setting changes')
        return True

    def _sleep_with_reload(self, seconds):
        end_time = time.monotonic() + max(0, seconds)
        while self.running:
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(1, remaining))
            if self._reload_config_if_changed():
                return True
        return False

    def start(self):
        """Start the full GPS bridge pipeline."""
        self.running = True

        self.logger.info(f'Fetching initial GPS position from {self.gps_source_name}...')
        if self.config.startup_timeout > 0:
            self.logger.info(f'Initial GPS fix timeout: {self.config.startup_timeout}s')
            deadline = time.monotonic() + self.config.startup_timeout
        else:
            self.logger.info('Initial GPS fix timeout: disabled')
            deadline = None

        attempt = 0
        while self.running:
            self._reload_config_if_changed()
            attempt += 1
            lat, lon, alt = self.gps_source.fetch_location()
            if lat is not None:
                self._update_position(lat, lon, alt)
                self.logger.info(f'Got initial position: {lat:.6f}, {lon:.6f}')
                break

            wait = min(5 * attempt, 60)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.logger.error(
                        f'No initial GPS fix after {self.config.startup_timeout}s; exiting'
                    )
                    return False
                wait = min(wait, remaining)

            self.logger.warning(f'Attempt {attempt} failed, retrying in {wait:.0f}s...')
            self._sleep_with_reload(wait)

        if not self.running:
            return False

        if not self._start_socat():
            return False

        self._nmea_thread = threading.Thread(target=self._nmea_writer_loop, daemon=True)
        self._nmea_thread.start()

        # gps-dbus expects data soon after opening the serial device.
        time.sleep(5)

        if not self._start_gps_dbus():
            return False

        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        self.logger.info('GPS bridge started successfully')
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

        for path in (self.config.virtual_device, self.config.write_device):
            if os.path.islink(path) or os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

        self.logger.info('GPS bridge stopped')

    def _update_position(self, lat, lon, alt):
        with self._lock:
            self.lat = lat
            self.lon = lon
            self.alt = alt
            self.last_update = time.time()

    def _start_socat(self):
        """Start socat to create a virtual serial device pair."""
        dev_link = self.config.virtual_device
        write_link = self.config.write_device

        for path in (dev_link, write_link):
            if os.path.islink(path) or os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

        try:
            self.socat_proc = subprocess.Popen(
                [
                    'socat',
                    f'PTY,raw,echo=0,link={dev_link}',
                    f'PTY,raw,echo=0,link={write_link}',
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            for _ in range(20):
                if os.path.exists(dev_link) and os.path.exists(write_link):
                    self.logger.info(f'Virtual serial device created: {dev_link}')
                    return True
                time.sleep(0.5)

            self.logger.error('Timeout waiting for virtual serial device')
            return False

        except FileNotFoundError:
            self.logger.error('socat not found')
            return False
        except Exception as exc:
            self.logger.error(f'Failed to start socat: {exc}')
            return False

    def _start_gps_dbus(self):
        """Start Victron's native gps-dbus service on the virtual serial device."""
        dev_link = self.config.virtual_device

        if not os.path.exists(self.config.gps_dbus_binary):
            self.logger.error(f'gps-dbus not found at {self.config.gps_dbus_binary}')
            return False

        subprocess.run(['killall', 'gps_dbus'], capture_output=True)
        time.sleep(1)

        try:
            self.gps_dbus_proc = subprocess.Popen(
                [
                    self.config.gps_dbus_binary,
                    '-v',
                    '--banner',
                    '--dbus',
                    'system',
                    '--timeout',
                    '5',
                    '-s',
                    dev_link,
                    '-b',
                    '9600',
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            time.sleep(3)

            if self.gps_dbus_proc.poll() is not None:
                stderr_pipe = self.gps_dbus_proc.stderr
                stderr = stderr_pipe.read().decode(errors='replace') if stderr_pipe else ''
                self.logger.error(f'gps-dbus exited immediately: {stderr}')
                return False

            self.logger.info(f'gps-dbus started (PID {self.gps_dbus_proc.pid})')
            return True

        except Exception as exc:
            self.logger.error(f'Failed to start gps-dbus: {exc}')
            return False

    def _nmea_writer_loop(self):
        """Continuously write NMEA sentences to the virtual serial device."""
        write_dev = self.config.write_device

        while self.running:
            try:
                with self._lock:
                    lat, lon, alt = self.lat, self.lon, self.alt

                if lat is not None and lon is not None:
                    data = make_gpgga(lat, lon, alt).encode()
                    data += make_gprmc(lat, lon).encode()

                    fd = os.open(write_dev, os.O_WRONLY | os.O_NONBLOCK)
                    try:
                        os.write(fd, data)
                    finally:
                        os.close(fd)

            except Exception as exc:
                self.logger.debug(f'NMEA write error: {exc}')

            time.sleep(self.config.nmea_interval)

    def _poll_loop(self):
        """Periodically poll MikroTik for updated GPS coordinates."""
        while self.running:
            self._sleep_with_reload(self.config.poll_interval)
            if not self.running:
                break

            try:
                self._reload_config_if_changed()
                lat, lon, alt = self.gps_source.fetch_location()
                if lat is not None:
                    self._update_position(lat, lon, alt)
            except Exception as exc:
                self.logger.error(f'Poll error: {exc}')

    def status(self):
        """Print current bridge status."""
        print('\n=== MikroTik GPS Bridge Status ===')

        socat_ok = self.socat_proc and self.socat_proc.poll() is None
        print(f'Socat:       {"RUNNING" if socat_ok else "STOPPED"}')

        dbus_ok = self.gps_dbus_proc and self.gps_dbus_proc.poll() is None
        print(f'gps-dbus:    {"RUNNING" if dbus_ok else "STOPPED"}')

        dev_ok = os.path.exists(self.config.virtual_device)
        print(f'Device:      {self.config.virtual_device} {"(present)" if dev_ok else "(missing)"}')

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
    """Configure logging to stderr."""
    level = logging.DEBUG if debug else (logging.INFO if verbose else logging.ERROR)
    formatter = logging.Formatter('[%(levelname)s] %(message)s')

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(level)

    root = logging.getLogger()
    root.handlers = []
    root.setLevel(level)
    root.addHandler(console)


def print_config(config):
    print('Configuration:')
    print(f'  Env file:     {config.env_file}')
    print(f'  GPS source:   {config.gps_source}')
    host = config.mikrotik_host or '(unset)'
    print(f'  MikroTik:     {host}:{config.mikrotik_port} ({config.api_mode})')
    print(f'  User:         {config.mikrotik_user}')
    print(f'  Password:     {config.masked_password()}')
    print(f'  GPS format:   {config.mikrotik_gps_format or "(router default)"}')
    print(f'  Reject 0,0:   {config.reject_zero_coordinates}')
    if config.gps_source == 'manual':
        print(
            f'  Manual GPS:   {config.manual_lat}, {config.manual_lon} (alt: {config.manual_alt}m)'
        )
    print(f'  Poll:         {config.poll_interval}s')
    print(f'  Startup wait: {startup_timeout_display(config)}')
    print(f'  NMEA write:   {config.nmea_interval}s')
    print(f'  GPS device:   {config.virtual_device}')
    print()


def source_display_name(config):
    return 'MikroTik' if config.gps_source == 'mikrotik' else 'Manual'


def startup_timeout_display(config):
    if config.startup_timeout <= 0:
        return 'forever'
    return f'{config.startup_timeout}s'


def build_parser(config):
    parser = argparse.ArgumentParser(
        description='MikroTik GPS to Victron GX Bridge',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  %(prog)s test
  %(prog)s test --lat 1.234567 --lon 2.345678 --alt 10
  %(prog)s -v test
  %(prog)s -v start --manual --lat 1.234567 --lon 2.345678 --alt 10
  %(prog)s -v start
  %(prog)s stop

Default .env path:
  {DEFAULT_ENV_FILE}
        """,
    )
    parser.add_argument(
        'command', choices=['start', 'stop', 'test', 'config'], help='Command to run'
    )
    parser.add_argument('--env-file', default=DEFAULT_ENV_FILE, help='Path to .env file')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    parser.add_argument('-d', '--debug', action='store_true', help='Debug output')
    parser.add_argument('--host', help='MikroTik host or IP address')
    parser.add_argument(
        '--port', type=int, help=f'RouterOS API port (default: {config.mikrotik_port})'
    )
    parser.add_argument('--user', help=f'RouterOS API user (default: {config.mikrotik_user})')
    parser.add_argument('--password', help='RouterOS API password')
    parser.add_argument('--tls', action='store_true', help='Use RouterOS api-ssl')
    parser.add_argument('--no-tls', action='store_true', help='Use RouterOS plain api')
    parser.add_argument(
        '--tls-verify', action='store_true', help='Verify RouterOS api-ssl certificate'
    )
    parser.add_argument(
        '--no-tls-verify', action='store_true', help='Do not verify RouterOS api-ssl certificate'
    )
    parser.add_argument(
        '--gps-format', help='GPS monitor format argument, usually dd; empty disables it'
    )
    parser.add_argument(
        '--interval', type=int, help=f'Poll interval in seconds (default: {config.poll_interval})'
    )
    parser.add_argument(
        '--startup-timeout',
        type=int,
        help=(
            'Seconds to wait for the first GPS fix before exiting; '
            f'0 retries forever (default: {config.startup_timeout})'
        ),
    )
    parser.add_argument('--source', choices=['mikrotik', 'manual'], help='GPS source to use')
    parser.add_argument(
        '--manual', action='store_true', help='Use fixed manual coordinates instead of MikroTik API'
    )
    parser.add_argument('--lat', type=float, help='Manual latitude in decimal degrees')
    parser.add_argument('--lon', type=float, help='Manual longitude in decimal degrees')
    parser.add_argument('--alt', type=float, help='Manual altitude in meters')
    return parser


def load_config_from_args(argv):
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument('--env-file', default=DEFAULT_ENV_FILE)
    pre_args, _ = pre_parser.parse_known_args(argv)

    config = AppConfig()
    config.env_file = pre_args.env_file
    file_env = load_env_file(config.env_file)
    apply_env(config, file_env)

    parser = build_parser(config)
    args = parser.parse_args(argv)
    config.env_file = args.env_file
    if args.env_file != pre_args.env_file:
        file_env = load_env_file(config.env_file)
        config = AppConfig()
        config.env_file = args.env_file
        apply_env(config, file_env)
    apply_cli(config, args)
    return config, args


def stop_services(config):
    subprocess.run(['killall', 'gps_dbus'], capture_output=True)
    subprocess.run(['killall', 'socat'], capture_output=True)

    for path in (config.virtual_device, config.write_device):
        if os.path.islink(path) or os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


def run_test(config):
    if config.gps_source == 'manual':
        print('Testing manual GPS coordinates...')
    else:
        print(
            f'Testing MikroTik GPS fetch from '
            f'{config.mikrotik_host}:{config.mikrotik_port} ({config.api_mode})...'
        )

    try:
        gps = create_gps_source(config)
    except ValueError as exc:
        print(f'  FAILED - {exc}')
        return 1

    lat, lon, alt = gps.fetch_location()
    if lat is not None:
        print(f'  Latitude:  {lat:.6f}')
        print(f'  Longitude: {lon:.6f}')
        print(f'  Altitude:  {alt:.1f}m')
        print()
        print('NMEA sentences:')
        print(f'  {make_gpgga(lat, lon, alt).strip()}')
        print(f'  {make_gprmc(lat, lon).strip()}')
        print()
        print(f'{source_display_name(config)} GPS source is working.')
        return 0

    print(f'  FAILED - could not fetch usable GPS from {config.gps_source}.')
    if config.gps_source == 'mikrotik':
        print('  Check:')
        print('    1. The GX device can route to the MikroTik on the vessel network')
        print('    2. RouterOS api/api-ssl service is enabled on the selected port')
        print('    3. The RouterOS user has api and read permissions')
        print('    4. /system gps monitor once returns valid=yes')
    return 1


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    config, args = load_config_from_args(argv)
    setup_logging(verbose=args.verbose, debug=args.debug)
    logger = logging.getLogger('main')

    if args.command == 'config':
        print_config(config)
        return 0

    if args.command == 'test':
        if args.verbose or args.debug:
            print_config(config)
        return run_test(config)

    if args.command == 'stop':
        logger.info('Stopping GPS services...')
        stop_services(config)
        print('GPS services stopped.')
        return 0

    if args.command == 'start':
        try:
            gps = create_gps_source(config)
        except ValueError as exc:
            logger.error(str(exc))
            return 1

        bridge = GPSBridge(gps, config, config_reloader=ConfigReloader(config, args))

        def signal_handler(sig, frame):
            logger.info('Received signal, shutting down...')
            bridge.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        if not bridge.start():
            logger.error('Failed to start GPS bridge')
            bridge.stop()
            return 1

        bridge.status()

        while True:
            try:
                time.sleep(60)

                if bridge.socat_proc and bridge.socat_proc.poll() is not None:
                    logger.warning('socat died, restarting...')
                    bridge._start_socat()
                    time.sleep(2)
                    bridge._start_gps_dbus()

                if bridge.gps_dbus_proc and bridge.gps_dbus_proc.poll() is not None:
                    logger.warning('gps-dbus died, restarting...')
                    bridge._start_gps_dbus()

            except KeyboardInterrupt:
                break

        bridge.stop()
        return 0

    return 1


if __name__ == '__main__':
    sys.exit(main())
