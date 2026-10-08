"""Dates and timezones. Every date MailTool shows or stores as "local" is in the
timezone chosen in Settings (default Africa/Nairobi), not the machine's own zone."""
from __future__ import annotations

import email.utils
import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

try:
    from dateutil import parser as _dateparser  # type: ignore
except Exception:
    _dateparser = None

DATE_FMT = "%d-%b-%Y"           # 01-Aug-2026, what the date fields accept
TIME_FMT = "%H:%M"
STORE_FMT = "%Y-%m-%d %H:%M:%S"  # how local times are written to CSV / shown


def resolve_timezone(tz_text):
    """IANA name ('Africa/Nairobi') or fixed offset ('+03:00', 'UTC+3').
    Falls back to the machine's local zone if neither parses."""
    tz_text = (tz_text or "").strip()
    if tz_text and ZoneInfo is not None:
        try:
            return ZoneInfo(tz_text)
        except Exception:
            pass
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", tz_text, re.IGNORECASE)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return timezone(sign * delta)
    return datetime.now().astimezone().tzinfo


def timezone_ok(tz_text):
    """True if tz_text is a real zone/offset (not just the silent fallback)."""
    tz_text = (tz_text or "").strip()
    if ZoneInfo is not None:
        try:
            ZoneInfo(tz_text)
            return True
        except Exception:
            pass
    return bool(re.fullmatch(r"(?:UTC|GMT)?\s*[+-]\d{1,2}(?::?\d{2})?", tz_text, re.IGNORECASE))


def get_received_datetime(msg):
    """Arrival time from the topmost Received: header (the final hop into the
    mailbox - what webmail shows). The timestamp is whatever follows the LAST
    ';'. Falls back to later Received headers, then Date:.
    Returns (aware datetime or None, source) with source received|date|none."""
    for value in msg.get_all("Received") or []:
        value = re.sub(r"\s+", " ", str(value))
        if ";" not in value:
            continue
        stamp = value.rsplit(";", 1)[1].strip()
        try:
            dt = email.utils.parsedate_to_datetime(stamp)
            if dt is not None:
                return dt, "received"
        except (TypeError, ValueError, IndexError):
            continue
    dt = parse_header_date(msg.get("Date"))
    if dt is not None:
        return dt, "date"
    return None, "none"


def parse_header_date(value):
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return parse_loose(str(value))


def to_local(dt, tz):
    """Aware -> the chosen zone. A naive datetime is assumed to already be local."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def parse_loose(value):
    """Parse a date typed by a human or written in a CSV. Naive result if no zone given."""
    if not value:
        return None
    value = str(value).strip()
    if _dateparser is not None:
        try:
            return _dateparser.parse(value)
        except Exception:
            pass
    for fmt in (STORE_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%d-%b-%Y %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return email.utils.parsedate_to_datetime(value)
    except Exception:
        return None


def parse_user_range(from_date, from_time, to_date, to_time):
    """Parse the From/To fields. Raises ValueError with a readable message."""
    try:
        start = datetime.strptime(f"{from_date.strip()} {(from_time or '00:00').strip()}", f"{DATE_FMT} {TIME_FMT}")
        end = datetime.strptime(f"{to_date.strip()} {(to_time or '23:59').strip()}", f"{DATE_FMT} {TIME_FMT}")
    except ValueError:
        raise ValueError("Dates must look like 01-Aug-2026 and times like 09:30 (24h).")
    if start > end:
        raise ValueError("The 'From' date/time must be before the 'To' date/time.")
    # inclusive to the end of the stated minute
    return start, end.replace(second=59)


def in_range(local_dt, start, end):
    """Messages with no usable time at all are kept rather than silently dropped."""
    if local_dt is None:
        return True
    return start <= local_dt.replace(tzinfo=None) <= end


def imap_search_window(start, end):
    """IMAP SINCE/BEFORE are date-only and in the server's own zone, so pad a day
    each side; the exact local-time filter runs client side."""
    since = (start - timedelta(days=1)).strftime(DATE_FMT)
    before = (end + timedelta(days=2)).strftime(DATE_FMT)
    return since, before


def format_long(dt):
    """'Wednesday July 15, 2026 10:58:57 AM' (webmail print style, portable)."""
    if dt is None:
        return "(unknown date)"
    return f"{dt:%A %B} {dt.day}, {dt:%Y %I:%M:%S %p}"


def delta_seconds(a, b, tz):
    """Absolute seconds between two datetimes; naive ones are taken as local (tz)."""
    if a is None or b is None:
        return None
    a = to_local(a, tz)
    b = to_local(b, tz)
    return abs((a - b).total_seconds())


def today_str():
    return datetime.now().strftime(DATE_FMT)
