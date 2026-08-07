"""WeChat platform glue — identity and content moderation.

Two environments, one interface:

* **Inside WeChat Cloud Run** (微信云托管). The platform terminates the
  `callContainer` call and injects `X-WX-OPENID`, so there is no login exchange
  to perform. Requests to `http://api.weixin.qq.com/...` are authenticated by
  the platform, so there is no access token to manage either. Set `WX_CLOUD=1`.

* **Anywhere else** (local dev, your own server). The client sends the `code`
  from `wx.login()`, we exchange it via `code2session`, and we fetch and cache
  an access token ourselves. Needs `WX_APPID` and `WX_SECRET`.

Neither path is exercised by the test suite — both need real credentials. What
the tests do cover is that the API layer degrades correctly when this module is
unavailable or unconfigured.
"""

from __future__ import annotations

import os
import threading
import time

import requests

TIMEOUT = 8
_token = {"value": None, "expires": 0}
_token_lock = threading.Lock()


def in_cloud() -> bool:
    """True when running inside WeChat Cloud Run."""
    return os.environ.get("WX_CLOUD", "").lower() in ("1", "true", "yes")


def _api_base() -> str:
    # Cloud Run intercepts plain http calls to this host and signs them.
    return "http://api.weixin.qq.com" if in_cloud() else "https://api.weixin.qq.com"


def openid_from_request(headers, code: str = None) -> str | None:
    """Resolve the caller's openid. Header first, then code exchange."""
    oid = headers.get("X-WX-OPENID") or headers.get("x-wx-openid")
    if oid:
        return oid
    if code:
        return code2session(code)
    return None


def code2session(code: str) -> str | None:
    """wx.login() code -> openid. Only needed outside Cloud Run."""
    appid, secret = os.environ.get("WX_APPID"), os.environ.get("WX_SECRET")
    if not (appid and secret):
        raise RuntimeError("WX_APPID and WX_SECRET are required to exchange a "
                           "login code (or run in Cloud Run, which injects "
                           "X-WX-OPENID and needs neither)")
    r = requests.get(f"{_api_base()}/sns/jscode2session",
                     params={"appid": appid, "secret": secret, "js_code": code,
                             "grant_type": "authorization_code"}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    if d.get("errcode"):
        raise RuntimeError(f"code2session failed: {d.get('errcode')} "
                           f"{d.get('errmsg')}")
    return d.get("openid")


def access_token() -> str:
    """Cached token. Unused in Cloud Run, where the platform signs calls."""
    with _token_lock:
        if _token["value"] and time.time() < _token["expires"]:
            return _token["value"]
        appid, secret = os.environ.get("WX_APPID"), os.environ.get("WX_SECRET")
        if not (appid and secret):
            raise RuntimeError("WX_APPID and WX_SECRET are required")
        r = requests.get(f"{_api_base()}/cgi-bin/token",
                         params={"grant_type": "client_credential",
                                 "appid": appid, "secret": secret}, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        if not d.get("access_token"):
            raise RuntimeError(f"token request failed: {d}")
        _token["value"] = d["access_token"]
        _token["expires"] = time.time() + int(d.get("expires_in", 7200)) - 300
        return _token["value"]


def check_text(content: str, openid: str, scene: int = 3) -> tuple[bool, str]:
    """Content moderation. Returns (allowed, reason).

    Required for anything user-generated that gets displayed — which includes a
    custom topic's description and label.

    **Fails closed.** If moderation is unreachable or misconfigured, this
    returns False. Letting unmoderated user text through because a dependency
    was down is the failure mode that gets a mini program taken down.
    Scene codes: 1 profile, 2 comment, 3 forum, 4 social log.
    """
    content = (content or "").strip()
    if not content:
        return False, "empty"
    if len(content) > 2500:  # msg_sec_check hard limit
        return False, "too long"

    url = f"{_api_base()}/wxa/msg_sec_check"
    params = {} if in_cloud() else {"access_token": access_token()}
    try:
        r = requests.post(url, params=params, json={
            "version": 2, "openid": openid, "scene": scene, "content": content,
        }, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
    except Exception as e:  # noqa: BLE001
        return False, f"moderation unavailable: {e}"

    if d.get("errcode") not in (0, None):
        return False, f"moderation error {d.get('errcode')}: {d.get('errmsg')}"
    label = (d.get("result") or {}).get("suggest", "pass")
    return label == "pass", label


def send_subscribe_message(openid: str, template_id: str, data: dict,
                           page: str = "pages/index/index") -> dict:
    """One-time subscription message push (一次性订阅消息).

    Each user authorization spends down to one message. Long-term subscriptions
    are not open to digest-style content, so this is the only push channel a
    mini program has — see SERVICE.md.
    """
    url = f"{_api_base()}/cgi-bin/message/subscribe/send"
    params = {} if in_cloud() else {"access_token": access_token()}
    r = requests.post(url, params=params, json={
        "touser": openid, "template_id": template_id, "page": page,
        "data": data, "miniprogram_state": os.environ.get("WX_MP_STATE", "formal"),
    }, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()
