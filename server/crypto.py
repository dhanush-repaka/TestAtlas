"""At-rest encryption for stored secrets (ADO Personal Access Tokens).

A local symmetric key is generated on first use and kept in data/secret.key
(gitignored). This keeps PATs out of plaintext in the SQLite DB; it is NOT a
substitute for a real secrets manager if this is ever deployed multi-tenant --
see README "Security notes" before hosting this anywhere shared.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet

_ROOT = Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent.parent)))
DATA_DIR = _ROOT / "data"
KEY_PATH = DATA_DIR / "secret.key"


def _get_fernet() -> Fernet:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY_PATH.exists():
        KEY_PATH.write_bytes(Fernet.generate_key())
        KEY_PATH.chmod(0o600)
    return Fernet(KEY_PATH.read_bytes())


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _get_fernet().decrypt(ciphertext.encode()).decode()
