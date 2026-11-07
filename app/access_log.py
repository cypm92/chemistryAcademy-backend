"""Protect the Microsoft OAuth callback in Uvicorn's standard access records."""
import logging


class OAuthCallbackFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn emits (client, method, path_with_query, http_version, status).
        # Only change the log record, never the ASGI request or its query string.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, target, version, status = record.args
            if isinstance(target, str):
                path, separator, _ = target.partition('?')
                if separator and path == '/integrations/microsoft/callback':
                    record.args = (client, method, path + '?[REDACTED]', version, status)
        return True
