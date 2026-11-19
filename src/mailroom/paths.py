"""Where mailroom keeps things. Nothing private ever lives in the repo."""

import os
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("MAILROOM_CONFIG") or Path.home() / ".config/mailroom"
    return Path(base)


def data_dir() -> Path:
    base = os.environ.get("MAILROOM_DATA") or Path.home() / ".local/share/mailroom"
    return Path(base)


def client_secret() -> Path:
    return config_dir() / "client_secret.json"


def token() -> Path:
    return config_dir() / "token.json"


def database() -> Path:
    return data_dir() / "mail.db"
