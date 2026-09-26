"""Defaults shared by the viewer (server.py) and the FTP server (ftp_server.py)."""

from datetime import date
from pathlib import Path


def default_shoot_folder() -> Path:
    """Today's shoot folder, e.g. ~/Pictures/2026/2026-05-07."""
    today = date.today()
    return Path.home() / 'Pictures' / str(today.year) / today.isoformat()
