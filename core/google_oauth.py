"""Google OAuth 2.0 authorization-code flow.

Hand-rolled rather than pulled from a framework so the scopes, the offline
refresh token and the account-creation rules are all visible in one place.
"""

import datetime

import requests
from django.conf import settings
from django.utils import timezone
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"


class GoogleAuthError(Exception):
    pass


def is_configured():
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def build_auth_url(state):
    """URL to send the executive to for consent.

    `access_type=offline` plus `prompt=consent` is what makes Google hand back
    a refresh token — without it we could only send while the user is actively
    signed in, which defeats scheduled sending.
    """
    from urllib.parse import urlencode

    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(settings.GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code(code):
    """Trade the one-time code for access/refresh tokens."""
    response = requests.post(
        TOKEN_ENDPOINT,
        data={
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise GoogleAuthError(f"Token exchange failed: {response.text}")
    return response.json()


def fetch_userinfo(access_token):
    response = requests.get(
        USERINFO_ENDPOINT,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if response.status_code != 200:
        raise GoogleAuthError(f"Could not read Google profile: {response.text}")
    return response.json()


def store_tokens(user, token_payload, scopes):
    """Persist tokens against the user, preserving an existing refresh token."""
    from .models import GoogleCredential

    credential, _ = GoogleCredential.objects.get_or_create(user=user)
    credential.access_token = token_payload.get("access_token", "")

    # Google omits refresh_token on re-consent in some flows. Keep the old one.
    new_refresh = token_payload.get("refresh_token")
    if new_refresh:
        credential.refresh_token = new_refresh

    expires_in = token_payload.get("expires_in")
    if expires_in:
        credential.token_expiry = timezone.now() + datetime.timedelta(
            seconds=int(expires_in)
        )
    credential.scopes = token_payload.get("scope", " ".join(scopes))
    credential.save()
    return credential


def credentials_for(user):
    """Return refreshed google-auth Credentials for an executive.

    Raises GoogleAuthError if the account was never connected or if Google has
    revoked the grant (in which case the executive must reconnect).
    """
    from .models import GoogleCredential

    try:
        stored = user.google_credential
    except GoogleCredential.DoesNotExist:
        raise GoogleAuthError(
            f"{user.friendly_name} has not connected a Google account yet."
        )

    if not stored.refresh_token:
        raise GoogleAuthError(
            "No Google refresh token on file — the executive needs to sign in "
            "with Google again to re-grant access."
        )

    credentials = Credentials(
        token=stored.access_token or None,
        refresh_token=stored.refresh_token,
        token_uri=TOKEN_ENDPOINT,
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=stored.scope_list or settings.GOOGLE_SCOPES,
    )

    if not credentials.valid:
        try:
            credentials.refresh(GoogleRequest())
        except Exception as exc:
            raise GoogleAuthError(
                f"Google refused to refresh access for {user.friendly_name}: {exc}. "
                "The executive needs to sign in with Google again."
            ) from exc
        stored.access_token = credentials.token or ""
        if credentials.expiry:
            stored.token_expiry = timezone.make_aware(
                credentials.expiry, datetime.timezone.utc
            )
        stored.save(update_fields=["access_token", "token_expiry", "updated_at"])

    return credentials
