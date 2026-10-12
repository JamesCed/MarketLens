"""
app/utils/privacy.py
----------------------
Keeping personal data confidential -- the IT expert's review: "if we
ever want to display something, hash or encrypt it ... always remember
that what we hold is very confidential."

Two tools:

1. MASKING, for display. A screen that only needs to tell records
   apart shows `ju•••@gmail.com` or `0917•••4567`, never the whole value.
   Registered as Jinja filters (|mask_email, |mask_phone) in create_app().

2. ENCRYPTION AT REST, for the personal details collected at sign-up
   (birthday, gender, address, contact number). EncryptedText is a
   column type that encrypts on the way into the database and decrypts
   on the way out, so the rest of the code reads and writes plain
   strings while the table holds only ciphertext. A copy of the database
   -- a leaked backup, a stolen dump -- shows nothing readable without
   the key.

   The key comes from PII_ENCRYPTION_KEY (any long random string, set
   once on the host). Without it the key is derived from SECRET_KEY,
   which Render already keeps stable. Adding PII_ENCRYPTION_KEY later is
   safe: values written under the SECRET_KEY fallback still decrypt (see
   _key_materials). Removing or changing a key that values were written
   under makes them unreadable -- they then read back as empty rather
   than crashing a page.

   Fernet (AES-128-CBC + HMAC-SHA256, from the `cryptography` package
   already in requirements.txt) authenticates as well as encrypts: a
   value altered in the database fails to decrypt instead of reading
   back as something else.
"""

import base64
import hashlib
import re
import threading

from sqlalchemy.types import Text, TypeDecorator

_MASK = "•••"  # •••


def mask_email(value):
    """`juan.delacruz@gmail.com` -> `ju•••@gmail.com`."""
    text = (value or "").strip()
    if "@" not in text:
        return mask_text(text)
    local, _, domain = text.partition("@")
    keep = local[:2] if len(local) > 3 else local[:1]
    return f"{keep}{_MASK}@{domain}"


def mask_phone(value):
    """`09171234567` -> `0917•••4567`; anything short is fully masked."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 7:
        return _MASK if digits else ""
    return f"{digits[:4]}{_MASK}{digits[-4:]}"


def mask_text(value, keep=1):
    text = (value or "").strip()
    if not text:
        return ""
    return f"{text[:keep]}{_MASK}"


# ---------------------------------------------------------------------
# Encryption at rest
# ---------------------------------------------------------------------
_PREFIX = "enc1:"
_fernet_lock = threading.Lock()
_fernet_cache = {}


def _key_materials():
    """The secrets to derive keys from, newest first: PII_ENCRYPTION_KEY
    when it is set, then SECRET_KEY. Encryption always uses the first;
    decryption tries each in turn (MultiFernet), so setting
    PII_ENCRYPTION_KEY on a deployment that has been running on the
    SECRET_KEY fallback does not lock anyone's details away -- old values
    still decrypt, and each is re-encrypted under the new key the next
    time it is saved."""
    from flask import current_app, has_app_context

    if not has_app_context():
        return ()
    materials = []
    for value in (current_app.config.get("PII_ENCRYPTION_KEY"), current_app.config.get("SECRET_KEY")):
        value = (value or "").strip()
        if value and value not in materials:
            materials.append(value)
    return tuple(materials)


def _fernet():
    materials = _key_materials()
    if not materials:
        return None
    hit = _fernet_cache.get(materials)
    if hit is not None:
        return hit
    from cryptography.fernet import Fernet, MultiFernet

    with _fernet_lock:
        # A fixed context string keeps these keys distinct from anything
        # else derived from the same secrets (Flask's own session signing
        # uses SECRET_KEY directly).
        keys = [Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"marketlens-pii-v1|" + m.encode("utf-8")).digest()))
                for m in materials]
        f = MultiFernet(keys)
        _fernet_cache[materials] = f
        return f


def encrypt_value(plain):
    if plain is None or plain == "":
        return plain
    f = _fernet()
    if f is None:
        raise RuntimeError("No encryption key available (no application context).")
    return _PREFIX + f.encrypt(str(plain).encode("utf-8")).decode("ascii")


def decrypt_value(stored):
    """Plain text for a stored value. A value written before encryption
    existed (no prefix) is returned as it is; one that cannot be
    decrypted (wrong key, tampered) reads back as None."""
    if stored is None or stored == "":
        return stored
    if not str(stored).startswith(_PREFIX):
        return stored
    f = _fernet()
    if f is None:
        return None
    from cryptography.fernet import InvalidToken

    try:
        return f.decrypt(str(stored)[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Loud in the log, quiet on the page: this means the key the value
        # was written under is not configured any more (see the module
        # docstring) -- restore PII_ENCRYPTION_KEY / SECRET_KEY to recover.
        import logging

        logging.getLogger(__name__).warning(
            "A stored personal detail could not be decrypted -- has PII_ENCRYPTION_KEY or SECRET_KEY changed?")
        return None


class EncryptedText(TypeDecorator):
    """A TEXT column whose contents are encrypted at rest. Read and write
    it as an ordinary string. Not searchable or sortable in SQL -- which
    is the point: nothing in this app looks people up by birthday or
    address."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_value(value)

    def process_result_value(self, value, dialect):
        return decrypt_value(value)
