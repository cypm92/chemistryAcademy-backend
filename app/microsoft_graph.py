"""Isolated Microsoft Graph event primitive; not connected to application routes."""
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx


class GraphRequestError(Exception):
    """Sanitized failure: never include request credentials or response content."""

    def __init__(self, status_code: int | None, retry_after: str | None = None):
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"Microsoft Graph request failed (status {status_code})")


def _error(response: httpx.Response) -> GraphRequestError:
    retry_after = response.headers.get("Retry-After", "") if response.status_code == 429 else ""
    if not (retry_after.isascii() and retry_after.isdecimal() and len(retry_after) <= 10):
        retry_after = None
    return GraphRequestError(response.status_code, retry_after)


def _event_url(event_id: str) -> str:
    if not isinstance(event_id, str) or not event_id or event_id in (".", ".."):
        raise ValueError("Invalid event ID")
    return f"https://graph.microsoft.com/v1.0/me/events/{quote(event_id, safe='')}"


def _utc(value: datetime) -> dict[str, str]:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")
    return {"dateTime": value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(), "timeZone": "UTC"}


def validate_join_url(join_url: str | None) -> str | None:
    """Accept only HTTPS links to the exact commercial Teams host."""
    if join_url is None:
        return None
    if not isinstance(join_url, str) or any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in join_url):
        raise ValueError("Invalid Teams join URL")
    parts = urlsplit(join_url)
    if (parts.scheme.lower() != "https" or parts.netloc.lower() not in
            ("teams.microsoft.com", "teams.microsoft.com:443") or not parts.path.startswith("/")):
        raise ValueError("Invalid Teams join URL")
    return join_url


def _join_url(data: dict) -> str | None:
    if not isinstance(data, dict):
        raise ValueError
    meeting = data.get("onlineMeeting")
    if meeting is None:
        return None
    if not isinstance(meeting, dict):
        raise ValueError
    return validate_join_url(meeting.get("joinUrl"))


def create_event(client: httpx.Client, access_token: str, subject: str, start: datetime,
                 end: datetime, transaction_id: str) -> tuple[str, str | None]:
    """Create a delegated online event; transaction_id must be persisted by caller."""
    start_value, end_value = _utc(start), _utc(end)
    if start >= end or not transaction_id:
        raise ValueError("Invalid event interval or transaction ID")
    payload = {
        "subject": subject, "start": start_value, "end": end_value,
        "isOnlineMeeting": True, "onlineMeetingProvider": "teamsForBusiness",
        "transactionId": transaction_id,
    }
    try:
        response = client.post("https://graph.microsoft.com/v1.0/me/events", json=payload,
                               headers={"Authorization": f"Bearer {access_token}", "Prefer": 'IdType="ImmutableId"'}, follow_redirects=False)
    except httpx.HTTPError:
        raise GraphRequestError(None) from None
    if response.status_code != 201:
        raise _error(response)
    try:
        data = response.json()
        event_id = data["id"]
        if not isinstance(event_id, str) or not event_id:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GraphRequestError(201) from None
    try:
        join_url = _join_url(data)
    except (ValueError, TypeError, AttributeError):
        # The event already exists: return its ID so callers can retry GET safely.
        join_url = None
    return event_id, join_url


def update_event(client: httpx.Client, access_token: str, event_id: str, subject: str,
                 start: datetime, end: datetime) -> str | None:
    start_value, end_value = _utc(start), _utc(end)
    if start >= end or not event_id:
        raise ValueError("Invalid event interval or ID")
    try:
        response = client.patch(
            _event_url(event_id),
            json={"subject": subject, "start": start_value, "end": end_value},
            headers={"Authorization": f"Bearer {access_token}", "Prefer": 'IdType="ImmutableId"'}, follow_redirects=False)
    except httpx.HTTPError:
        raise GraphRequestError(None) from None
    if response.status_code != 200:
        raise _error(response)
    try:
        return _join_url(response.json())
    except (ValueError, TypeError, AttributeError):
        raise GraphRequestError(response.status_code) from None


def get_event(client: httpx.Client, access_token: str, event_id: str) -> str | None:
    """Read a commercial Teams join URL without following redirects."""
    try:
        response = client.get(_event_url(event_id),
                              headers={"Authorization": f"Bearer {access_token}", "Prefer": 'IdType="ImmutableId"'},
                              follow_redirects=False)
    except httpx.HTTPError:
        raise GraphRequestError(None) from None
    if response.status_code != 200:
        raise _error(response)
    try:
        return _join_url(response.json())
    except (ValueError, TypeError, AttributeError):
        raise GraphRequestError(response.status_code) from None


def get_event_state(client: httpx.Client, access_token: str, event_id: str):
    """Read comparable event fields in UTC; malformed responses fail closed."""
    try:
        response = client.get(_event_url(event_id), headers={
            "Authorization": f"Bearer {access_token}",
            "Prefer": 'outlook.timezone="UTC", IdType="ImmutableId"',
        }, follow_redirects=False)
    except httpx.HTTPError:
        raise GraphRequestError(None) from None
    if response.status_code != 200:
        raise _error(response)
    try:
        data = response.json()
        subject = data['subject']
        if not isinstance(subject, str):
            raise ValueError
        def instant(field):
            value = data[field]
            if value['timeZone'].upper() not in ('UTC', 'ETC/UTC'):
                raise ValueError
            dt = datetime.fromisoformat(value['dateTime'].replace('Z', '+00:00'))
            return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc))
        return subject, instant('start'), instant('end'), _join_url(data)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GraphRequestError(response.status_code) from None


def delete_event(client: httpx.Client, access_token: str, event_id: str) -> None:
    """Delete an event; 404 remains visible to the caller as an error."""
    try:
        response = client.delete(_event_url(event_id),
                                 headers={"Authorization": f"Bearer {access_token}", "Prefer": 'IdType="ImmutableId"'},
                                 follow_redirects=False)
    except httpx.HTTPError:
        raise GraphRequestError(None) from None
    if response.status_code != 204:
        raise _error(response)
