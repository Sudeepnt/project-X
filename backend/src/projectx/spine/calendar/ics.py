"""iCalendar (RFC 5545) invitations: the attachment Gmail, Outlook and Apple Calendar turn into an event."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")
    )


def _param(value: str) -> str:
    """A parameter value (like CN): quoted, with characters that would break the line removed."""
    return '"' + value.replace('"', "'").replace("\r", " ").replace("\n", " ") + '"'


def _fold(line: str) -> str:
    """Lines longer than 75 octets continue on the next line after a space."""
    data = line.encode()
    if len(data) <= 75:
        return line
    parts: list[str] = []
    while data:
        size = 75 if not parts else 74
        cut = min(size, len(data))
        while cut < len(data) and (data[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        parts.append(data[:cut].decode())
        data = data[cut:]
    return "\r\n ".join(parts)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_invite(
    *,
    uid: str,
    method: str,
    sequence: int,
    title: str,
    description: str,
    location: str | None,
    starts_at: datetime,
    ends_at: datetime,
    organizer_email: str,
    organizer_name: str,
    attendees: list[dict[str, Any]],
    now: datetime,
) -> bytes:
    if method not in ("REQUEST", "CANCEL"):
        raise ValueError("method is REQUEST or CANCEL")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Project X//Calendar//EN",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SEQUENCE:{sequence}",
        f"DTSTAMP:{_stamp(now)}",
        f"DTSTART:{_stamp(starts_at)}",
        f"DTEND:{_stamp(ends_at)}",
        f"SUMMARY:{_escape(title)}",
        f"STATUS:{'CANCELLED' if method == 'CANCEL' else 'CONFIRMED'}",
        f"ORGANIZER;CN={_param(organizer_name)}:mailto:{organizer_email}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    for a in attendees:
        cn = f";CN={_param(a['name'])}" if a.get("name") else ""
        lines.append(f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE{cn}:mailto:{a['email']}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(_fold(line) for line in lines) + "\r\n").encode()
