"""OAuth 1.0a request signing (HMAC-SHA1).

X still accepts OAuth 1.0a user context on the v2 endpoints this bot uses. The four values
on the app's "Keys and tokens" tab need no browser flow and never expire, which makes them
the easier credential for a cron job. The catch is whose account they are: an access token
generated in the portal belongs to the account that owns the app, so to post as a separate
bot account you need the OAuth 2.0 flow in :mod:`socagents.social.x.oauth` instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

SIGNATURE_METHOD = "HMAC-SHA1"


def encode(value: Any) -> str:
    """Percent-encoding per RFC 3986: only A-Z a-z 0-9 - . _ ~ stay as they are."""
    return quote(str(value), safe="~")


def signature_base(method: str, url: str, params: dict[str, Any]) -> str:
    split = urlsplit(url)
    base_url = f"{split.scheme}://{split.netloc}{split.path}"
    pairs = [*parse_qsl(split.query, keep_blank_values=True), *params.items()]
    joined = "&".join(f"{k}={v}" for k, v in sorted((encode(k), encode(v)) for k, v in pairs))
    return "&".join([method.upper(), encode(base_url), encode(joined)])


def authorization_header(
    *,
    method: str,
    url: str,
    params: dict[str, Any] | None,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    nonce: str | None = None,
    timestamp: int | None = None,
) -> str:
    """The ``Authorization: OAuth ...`` header for one request.

    A JSON body is not part of the signature; only the query and the oauth parameters are,
    which is why ``params`` must be exactly what the client puts on the wire.
    """
    oauth: dict[str, str] = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": SIGNATURE_METHOD,
        "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    base = signature_base(method, url, {**(params or {}), **oauth})
    key = f"{encode(consumer_secret)}&{encode(token_secret)}".encode()
    digest = hmac.new(key, base.encode(), hashlib.sha1).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode()
    return "OAuth " + ", ".join(f'{encode(k)}="{encode(v)}"' for k, v in sorted(oauth.items()))
