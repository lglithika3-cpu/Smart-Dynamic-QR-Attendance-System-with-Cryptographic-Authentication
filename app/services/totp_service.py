import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet
from flask import current_app


def generate_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _fernet():
    key_material = current_app.config["QR_SIGNING_SECRET"].encode("utf-8")
    key = hmac.new(key_material, b"privacy-attendance/totp-encryption/v1", hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(secret):
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(encrypted_secret):
    return _fernet().decrypt(encrypted_secret.encode("ascii")).decode("ascii")


def matching_counter(secret, code, *, timestamp=None):
    if not isinstance(code, str) or not code.isdigit() or len(code) != 6:
        return None
    current_time = time.time() if timestamp is None else timestamp
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    for offset in (-30, 0, 30):
        counter = int((current_time + offset) // 30)
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset_index = digest[-1] & 0x0F
        value = struct.unpack(">I", digest[offset_index:offset_index + 4])[0] & 0x7FFFFFFF
        expected = f"{value % 1_000_000:06d}"
        if secrets.compare_digest(expected, code):
            return counter
    return None


def provisioning_uri(secret, account_name):
    issuer = "Privacy Attendance"
    label = quote(f"{issuer}:{account_name}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        "&algorithm=SHA1&digits=6&period=30"
    )
