"""Tests for the camera upload FTP server."""

import ftplib
import io
import threading

from ftp_server import make_server


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
