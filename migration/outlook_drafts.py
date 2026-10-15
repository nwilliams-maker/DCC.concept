"""Save Route Request emails straight into an Outlook mailbox's Drafts folder
through Microsoft Graph (app-only / client credentials).

Railway variables (all four required):
  MS_TENANT_ID            Azure AD tenant (Directory) ID
  MS_CLIENT_ID            App registration (Application) ID
  MS_CLIENT_SECRET        App client secret value
  OUTLOOK_DRAFTS_MAILBOX  Mailbox the drafts are saved in, e.g. nwilliams@terraboost.biz

The app registration needs the Microsoft Graph *application* permission
Mail.ReadWrite with admin consent. Nothing here sends mail: a draft is only
created; the dispatcher reviews and sends it from Outlook.
"""
from __future__ import annotations

import html
import os
import re
import threading
import time
from typing import Callable

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
_ENV = ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "OUTLOOK_DRAFTS_MAILBOX")
_token_lock = threading.Lock()
_token_cache: dict = {}


class DraftError(RuntimeError):
    pass


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def missing_settings() -> list[str]:
    return [name for name in _ENV if not _env(name)]


def configured() -> bool:
    return not missing_settings()


def mailbox() -> str:
    return _env("OUTLOOK_DRAFTS_MAILBOX")


def _access_token(request_post: Callable = requests.post) -> str:
    with _token_lock:
        cached = _token_cache.get("token")
        if cached and _token_cache.get("expires", 0) - 60 > time.time():
            return cached
        resp = request_post(
            f"https://login.microsoftonline.com/{_env('MS_TENANT_ID')}/oauth2/v2.0/token",
            data={"client_id": _env("MS_CLIENT_ID"), "client_secret": _env("MS_CLIENT_SECRET"),
                  "scope": "https://graph.microsoft.com/.default",
                  "grant_type": "client_credentials"},
            timeout=20,
        )
        body = _json(resp)
        token = body.get("access_token")
        if not token:
            raise DraftError("Outlook sign-in failed: "
                             + str(body.get("error_description") or body.get("error") or resp.status_code)[:200])
        _token_cache.update(token=token, expires=time.time() + int(body.get("expires_in") or 3600))
        return token


def _json(resp):
    try:
        return resp.json() or {}
    except ValueError:
        return {}


_URL = re.compile(r"https?://[^\s<>\"']+")


def text_to_html(text: str) -> str:
    """Plain email text -> safe HTML with clickable links and line breaks."""
    out, last = [], 0
    for m in _URL.finditer(text or ""):
        out.append(html.escape(text[last:m.start()]))
        url = m.group(0)
        out.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>')
        last = m.end()
    out.append(html.escape((text or "")[last:]))
    return "".join(out).replace("\r\n", "\n").replace("\n", "<br>")


def save_draft(to: str, subject: str, body_text: str, box: str | None = None,
               request_post: Callable = requests.post) -> dict:
    """Create one draft. Returns {"id", "web_link"}; raises DraftError."""
    missing = missing_settings()
    if missing:
        raise DraftError("Outlook drafts are not set up (missing " + ", ".join(missing) + ")")
    to = (to or "").strip()
    if not to or "@" not in to:
        raise DraftError("The IC has no email address on file")
    box = (box or mailbox()).strip()
    token = _access_token(request_post)
    resp = request_post(
        f"{GRAPH}/users/{requests.utils.quote(box, safe='@')}/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"subject": subject,
              "body": {"contentType": "HTML", "content": text_to_html(body_text)},
              "toRecipients": [{"emailAddress": {"address": to}}]},
        timeout=20,
    )
    body = _json(resp)
    if resp.status_code not in (200, 201) or not body.get("id"):
        err = (body.get("error") or {}) if isinstance(body.get("error"), dict) else {}
        if resp.status_code == 401:
            _token_cache.clear()
        raise DraftError(f"Outlook refused the draft (HTTP {resp.status_code}): "
                         + str(err.get("message") or err.get("code") or "")[:200])
    return {"id": body["id"], "web_link": body.get("webLink", "")}
