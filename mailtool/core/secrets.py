"""Password storage: OS keyring when available and asked for, otherwise memory only.

Windows -> Credential Manager, KDE -> KWallet, GNOME -> Secret Service (via `keyring`).
A password is never written to settings.json."""
from __future__ import annotations

from mailtool import APP_NAME
from mailtool.core.util import log

try:
    import keyring  # type: ignore
    from keyring.errors import KeyringError  # type: ignore
except Exception:
    keyring = None
    KeyringError = Exception

_session = {}


def _key(server, username):
    return f"{(username or '').strip().lower()}@{(server or '').strip().lower()}"


def keyring_available():
    if keyring is None:
        return False
    try:
        kr = keyring.get_keyring()
        # the "fail" / null backends mean nothing usable is installed
        return "fail" not in type(kr).__module__ and "null" not in type(kr).__module__.lower()
    except Exception:
        return False


def get_password(server, username):
    k = _key(server, username)
    if k in _session:
        return _session[k]
    if keyring_available():
        try:
            pw = keyring.get_password(APP_NAME, k)
            if pw:
                _session[k] = pw
                return pw
        except KeyringError as e:
            log.warning("keyring read failed: %s", e)
        except Exception as e:
            log.warning("keyring read failed: %s", e)
    return ""


def set_password(server, username, password, remember):
    """Keep for this session; also store in the keyring if remember is True.
    Returns True if it was saved to the keyring."""
    k = _key(server, username)
    _session[k] = password
    if not keyring_available():
        return False
    try:
        if remember and password:
            keyring.set_password(APP_NAME, k, password)
            return True
        try:
            keyring.delete_password(APP_NAME, k)
        except Exception:
            pass
    except Exception as e:
        log.warning("keyring write failed: %s", e)
    return False


def forget(server, username):
    k = _key(server, username)
    _session.pop(k, None)
    if keyring_available():
        try:
            keyring.delete_password(APP_NAME, k)
        except Exception:
            pass


def forget_session(server, username):
    """Drop the in-memory copy only (the keyring entry, if any, is kept)."""
    _session.pop(_key(server, username), None)
