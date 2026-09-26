#!/usr/bin/env python3
"""
Local FTP server for Canon wifi tethering.

The camera (e.g. EOS R3) uploads each shot over FTP into the upload folder —
by default today's shoot folder, the same default as the WifiTether viewer. Anonymous login with full write
access — only run on a private network (e.g. your own hotspot).
"""

import argparse
import re
import socket
import subprocess
from pathlib import Path

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from defaults import default_shoot_folder

DEFAULT_PORT = 2121
PASSIVE_PORTS = range(60000, 60100)


def local_ip() -> str:
    """Best-guess LAN IP address (no packets are actually sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(('10.255.255.255', 1))
            return s.getsockname()[0]
        except OSError:
            return '127.0.0.1'


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def _parse_wifi_device(hardware_ports: str) -> str:
    """Wi-Fi interface name from `networksetup -listallhardwareports` (usually en0)."""
    match = re.search(r'Hardware Port: (?:Wi-Fi|AirPort)\nDevice: (\S+)', hardware_ports)
    return match.group(1) if match else 'en0'


def _parse_ssid(output: str) -> str | None:
    """Network name from the output of any of the macOS commands tried in
    wifi_network_name(). Newer macOS versions may redact it."""
    for pattern in (
        r'^\s*SSID : (.+)$',                                  # ipconfig getsummary
        r'^Current Wi-Fi Network: (.+)$',                     # networksetup -getairportnetwork
        r'Current Network Information:\n\s*(.+?):\s*$',       # system_profiler SPAirPortDataType
    ):
        match = re.search(pattern, output, re.MULTILINE)
        if match and match.group(1).strip() not in ('', '<redacted>'):
            return match.group(1).strip()
    return None


def wifi_network_name() -> str | None:
    """Name of the Wi-Fi network this Mac is on, or None if it can't be read.

    macOS has changed (and restricted) how this is exposed over time, so try
    each known method in turn, fastest first.
    """
    device = _parse_wifi_device(_run(['networksetup', '-listallhardwareports']))
    for cmd in (
        ['ipconfig', 'getsummary', device],
        ['networksetup', '-getairportnetwork', device],
        ['system_profiler', 'SPAirPortDataType'],
    ):
        ssid = _parse_ssid(_run(cmd))
        if ssid:
            return ssid
    return None


def make_server(upload_dir: Path, port: int, host: str = '0.0.0.0') -> FTPServer:
    upload_dir.mkdir(parents=True, exist_ok=True)

    authorizer = DummyAuthorizer()
    authorizer.add_anonymous(str(upload_dir), perm='elradfmwMT')

    # Subclass so settings don't leak onto the shared FTPHandler class
    class Handler(FTPHandler):
        pass

    Handler.authorizer = authorizer
    Handler.passive_ports = PASSIVE_PORTS

    return FTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--dir', type=Path, default=default_shoot_folder(),
                        help='upload folder (default: %(default)s)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'FTP port (default: {DEFAULT_PORT})')
    args = parser.parse_args()
    upload_dir = args.dir.expanduser()

    try:
        server = make_server(upload_dir, args.port)
    except OSError as e:
        raise SystemExit(f'Could not start FTP server on port {args.port}: {e}\n'
                         'Is it already running? Stop it with: pkill -f ftp_server.py')

    network = wifi_network_name()
    host = local_ip()

    print('Starting FTP server...')
    print(f'   Wi-Fi network: {network or "unknown"}')
    print(f'   Host:          {host}')
    print(f'   Port:          {args.port}')
    print(f'   Passive ports: {PASSIVE_PORTS.start}-{PASSIVE_PORTS.stop - 1}')
    print(f'   Upload folder: {upload_dir}')
    print('   Username:      anonymous (no password)')
    print()
    if network:
        print(f'Connect the camera to {network}')
    else:
        print('Connect the camera to the same Wi-Fi network as this Mac')
    print(f'Point the camera at {host}:{args.port}')
    print('Press Ctrl+C to stop.')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close_all()


if __name__ == '__main__':
    main()
