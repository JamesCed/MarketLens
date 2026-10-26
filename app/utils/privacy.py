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

   ROTATING THE KEY. Put the new secret in PII_ENCRYPTION_KEY and the
   old one(s) in PII_ENCRYPTION_PREVIOUS_KEYS (comma-separated; the old
   SECRET_KEY too, if details were written before PII_ENCRYPTION_KEY
   existed and SECRET_KEY is being changed as well). On the next start
   rotate_encrypted_values() re-encrypts every stored detail under the
   new key (startup_migrations); after that the previous keys can be
   removed. Keep PII_ENCRYPTION_KEY different from SECRET_KEY and out of
   every file -- set it in the host's environment settings only.

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

_MASK = "•••"


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


_PREFIX = "enc1:"
_fernet_lock = threading.Lock()
_fernet_cache = {}


def _key_materials():
    """The secrets to derive keys from, newest first: PII_ENCRYPTION_KEY
    when it is set, then each of PII_ENCRYPTION_PREVIOUS_KEYS, then
    SECRET_KEY. Encryption always uses the first; decryption tries each
    in turn (MultiFernet), so setting or rotating PII_ENCRYPTION_KEY does
    not lock anyone's details away -- old values still decrypt, and
    rotate_encrypted_values() re-encrypts them under the new key."""
    from flask import current_app, has_app_context

    if not has_app_context():
        return ()
    previous = re.split(r"[\s,]+", current_app.config.get("PII_ENCRYPTION_PREVIOUS_KEYS") or "")
    materials = []
    for value in (current_app.config.get("PII_ENCRYPTION_KEY"), *previous, current_app.config.get("SECRET_KEY")):
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
    from cryptography.fernet import MultiFernet

    with _fernet_lock:
        f = MultiFernet([_single_fernet(m) for m in materials])
        _fernet_cache[materials] = f
        return f


def _single_fernet(material):
    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"marketlens-pii-v1|" + material.encode("utf-8")).digest()))


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
        import logging

        logging.getLogger(__name__).warning(
            "A stored personal detail could not be decrypted -- has PII_ENCRYPTION_KEY or SECRET_KEY changed?")
        return None


ENCRYPTED_USER_COLUMNS = ("contact_number_enc", "gender_enc", "birthdate_enc",
                          "address_line_enc", "address_barangay_enc")


def rotate_encrypted_values():
    """Re-encrypt every stored personal detail under the newest key.

    Reads the raw ciphertext (not through EncryptedText), leaves values
    already under the newest key alone, and re-encrypts the rest with
    MultiFernet.rotate(). Returns {"rotated": n, "unreadable": n};
    unreadable values (written under a key that is no longer configured)
    are left untouched so restoring the old key can still recover them.
    Does nothing when only one key is configured."""
    materials = _key_materials()
    result = {"rotated": 0, "unreadable": 0}
    if len(materials) < 2:
        return result
    from cryptography.fernet import InvalidToken
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    from app.extensions import db

    available = {column["name"] for column in sa_inspect(db.engine).get_columns("user")}
    columns = [column for column in ENCRYPTED_USER_COLUMNS if column in available]
    if not columns:
        return result
    multi = _fernet()
    newest = _single_fernet(materials[0])
    quoted = ", ".join(db.engine.dialect.identifier_preparer.quote(c) for c in columns)
    table = db.engine.dialect.identifier_preparer.quote("user")
    rows = db.session.execute(text(f"SELECT user_id, {quoted} FROM {table}")).all()
    for user_id, *values in rows:
        changes = {}
        for column, stored in zip(columns, values):
            if not stored or not str(stored).startswith(_PREFIX):
                continue
            token = str(stored)[len(_PREFIX):].encode("ascii")
            try:
                newest.decrypt(token)
                continue
            except InvalidToken:
                pass
            try:
                changes[column] = _PREFIX + multi.rotate(token).decode("ascii")
            except InvalidToken:
                result["unreadable"] += 1
        if changes:
            assignments = ", ".join(f"{db.engine.dialect.identifier_preparer.quote(c)} = :{c}" for c in changes)
            db.session.execute(text(f"UPDATE {table} SET {assignments} WHERE user_id = :user_id"),
                               {**changes, "user_id": user_id})
            result["rotated"] += len(changes)
    db.session.commit()
    return result


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
