"""Ready-made authenticators for ``McpServerOptions.authenticator``."""

import hmac
from typing import Any, Callable, Iterable, Mapping, Optional, Union

from .transport import HttpRequest

__all__ = ["bearer_token_authenticator"]


def bearer_token_authenticator(
    tokens: Union[Iterable[str], Mapping[str, Any]],
) -> Callable[[HttpRequest], Optional[Any]]:
    """Accept ``Authorization: Bearer <token>`` for any of ``tokens`` (constant-time comparison).

    If ``tokens`` is a mapping, the matched value is returned as the request principal
    (available as ``get_current_request().user`` inside handlers).
    """
    mapping = dict(tokens) if isinstance(tokens, Mapping) else {t: t for t in tokens}

    def authenticate(request: HttpRequest) -> Optional[Any]:
        header = request.header("Authorization") or ""
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return None
        presented = presented.strip()
        principal = None
        for token, value in mapping.items():
            if hmac.compare_digest(token.encode("utf-8"), presented.encode("utf-8")):
                principal = value if value is not None else token
        return principal

    return authenticate
