#!/usr/bin/env python3
"""
Local FTP server for Canon wifi tethering.

The camera (e.g. EOS R3) uploads each shot over FTP into the upload folder —
by default today's shoot folder, the same default as the WifiTether viewer. Anonymous login with full write
access — only run on a private network (e.g. your own hotspot).
"""

import argparse
import socket
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

    print('Starting FTP server...')
    print(f'   Host:          {local_ip()}')
    print(f'   Port:          {args.port}')
    print(f'   Passive ports: {PASSIVE_PORTS.start}-{PASSIVE_PORTS.stop - 1}')
    print(f'   Upload folder: {upload_dir}')
    print('   Username:      anonymous (no password)')
    print()
    print(f'Point the camera at {local_ip()}:{args.port}')
    print('Press Ctrl+C to stop.')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close_all()


if __name__ == '__main__':
    main()
