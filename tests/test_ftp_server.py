"""Tests for the camera upload FTP server."""

import ftplib
import io
import threading

import pytest

from ftp_server import _parse_ssid, _parse_wifi_device, make_server


@pytest.mark.parametrize('output', [
    # ipconfig getsummary en0 (BSSID line must not match)
    '<dictionary> {\n  BSSID : aa:bb:cc:dd:ee:ff\n  SSID : Logan iPhone\n  Security : WPA2\n}',
    # networksetup -getairportnetwork en0
    'Current Wi-Fi Network: Logan iPhone\n',
    # system_profiler SPAirPortDataType
    '        en0:\n          Status: Connected\n          Current Network Information:\n'
    '            Logan iPhone:\n              PHY Mode: 802.11ax\n',
])
def test_parse_ssid(output):
    assert _parse_ssid(output) == 'Logan iPhone'


@pytest.mark.parametrize('output', [
    '  SSID : <redacted>\n',
    'You are not associated with an AirPort network.\n',
    '',
])
def test_parse_ssid_unknown(output):
    assert _parse_ssid(output) is None


def test_parse_wifi_device():
    ports = ('Hardware Port: Ethernet\nDevice: en0\nEthernet Address: x\n\n'
             'Hardware Port: Wi-Fi\nDevice: en1\nEthernet Address: y\n')
    assert _parse_wifi_device(ports) == 'en1'
    assert _parse_wifi_device('') == 'en0'


def test_anonymous_upload_lands_in_upload_dir(tmp_path):
    upload_dir = tmp_path / 'uploads'
    server = make_server(upload_dir, port=0, host='127.0.0.1')
    port = server.address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={'timeout': 0.1}, daemon=True)
    thread.start()
    try:
        with ftplib.FTP() as ftp:
            ftp.connect('127.0.0.1', port, timeout=5)
            ftp.login()  # anonymous
            ftp.mkd('DCIM')
            ftp.cwd('DCIM')
            ftp.storbinary('STOR IMG_0001.CR3', io.BytesIO(b'raw bytes'))
    finally:
        server.close_all()
        thread.join(timeout=5)

    assert (upload_dir / 'DCIM' / 'IMG_0001.CR3').read_bytes() == b'raw bytes'
