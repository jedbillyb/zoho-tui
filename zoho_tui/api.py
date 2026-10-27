"""Thin Zoho Mail REST client."""
import os
import re
import time
from pathlib import Path

import requests

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "zoho-tui" / "config.env"


class ZohoError(Exception):
    pass


def load_config():
    cfg = {}
    if CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        if k.startswith("ZOHO_"):
            cfg[k] = v
    missing = [k for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN") if not cfg.get(k)]
    if missing:
        raise ZohoError(f"Missing {', '.join(missing)} in {CONFIG_PATH}")
    return cfg


class Zoho:
    def __init__(self, cfg):
        self.client_id = cfg["ZOHO_CLIENT_ID"]
        self.client_secret = cfg["ZOHO_CLIENT_SECRET"]
        self.refresh_token = cfg["ZOHO_REFRESH_TOKEN"]
        domain = cfg.get("ZOHO_DOMAIN", "com")
        self.accounts_url = f"https://accounts.zoho.{domain}"
        self.mail_url = f"https://mail.zoho.{domain}/api"
        self.account_id = cfg.get("ZOHO_ACCOUNT_ID")
        self.from_address = cfg.get("ZOHO_FROM_ADDRESS")
        self._token = None
        self._expiry = 0
        self._folders = None
        self.session = requests.Session()
        if not self.account_id or not self.from_address:
            acct = self._req("GET", "/accounts")[0]
            self.account_id = self.account_id or acct["accountId"]
            self.from_address = self.from_address or acct.get("primaryEmailAddress") or acct["emailAddress"][0]["mailId"]

    def _auth(self):
        if time.time() < self._expiry - 60:
            return self._token
        r = self.session.post(f"{self.accounts_url}/oauth/v2/token", data={
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }, timeout=20)
        d = r.json()
        if "access_token" not in d:
            raise ZohoError(f"Auth failed: {d}")
        self._token = d["access_token"]
        self._expiry = time.time() + d.get("expires_in", 3600)
        return self._token

    def _req(self, method, path, **kw):
        r = self.session.request(
            method, self.mail_url + path,
            headers={"Authorization": f"Zoho-oauthtoken {self._auth()}"},
            timeout=30, **kw,
        )
        try:
            d = r.json()
        except ValueError:
            raise ZohoError(f"HTTP {r.status_code}: {r.text[:200]}")
        code = d.get("status", {}).get("code")
        if code != 200:
            raise ZohoError(d.get("data", {}).get("errorCode") or d.get("status", {}).get("description") or str(d))
        return d.get("data")

    def _acct(self, path):
        return f"/accounts/{self.account_id}{path}"

    def folders(self, refresh=False):
        if self._folders is None or refresh:
            self._folders = self._req("GET", self._acct("/folders"))
        return self._folders

    def folder_id(self, name):
        for f in self.folders():
            if f["folderName"].lower() == name.lower():
                return f["folderId"]
        raise ZohoError(f"No folder named {name}")

    def messages(self, folder_id, start=1, limit=50):
        return self._req("GET", self._acct("/messages/view"),
                         params={"folderId": folder_id, "start": start, "limit": limit}) or []

    def search(self, query, limit=50):
        # Zoho only matches whole words, so plain queries become prefix matches on
        # every word, ANDed: "ortho appointment" -> entire:ortho*::entire:appointment*
        if ":" not in query:
            words = re.findall(r"[\w@.'-]+", query)
            query = "::".join(f"entire:{w}*" for w in words) or query
        return self._req("GET", self._acct("/messages/search"),
                         params={"searchKey": query, "limit": limit}) or []

    def details(self, folder_id, message_id):
        return self._req("GET", self._acct(f"/folders/{folder_id}/messages/{message_id}/details"))

    def content(self, folder_id, message_id):
        return self._req("GET", self._acct(f"/folders/{folder_id}/messages/{message_id}/content"))["content"]

    def _update(self, ids, mode, **extra):
        self._req("PUT", self._acct("/updatemessage"), json={"messageId": ids, "mode": mode, **extra})

    def mark_read(self, message_id, read=True):
        self._update([message_id], "markAsRead" if read else "markAsUnread")

    def flag(self, message_id, on=True):
        self._update([message_id], "setFlag", flagid="important" if on else "flag_not_set")

    def move(self, message_id, folder_id):
        self._update([message_id], "moveMessage", destfolderId=folder_id)

    def delete(self, folder_id, message_id):
        self._req("DELETE", self._acct(f"/folders/{folder_id}/messages/{message_id}"))

    def send(self, to, subject, body, cc="", bcc=""):
        payload = {"fromAddress": self.from_address, "toAddress": to, "subject": subject,
                   "content": body, "mailFormat": "plaintext"}
        if cc:
            payload["ccAddress"] = cc
        if bcc:
            payload["bccAddress"] = bcc
        self._req("POST", self._acct("/messages"), json=payload)

    def reply(self, message_id, to, subject, body, cc=""):
        payload = {"fromAddress": self.from_address, "toAddress": to, "subject": subject,
                   "content": body, "mailFormat": "plaintext", "action": "reply"}
        if cc:
            payload["ccAddress"] = cc
        self._req("POST", self._acct(f"/messages/{message_id}"), json=payload)

    def forward(self, message_id, to, subject, body, cc=""):
        payload = {"fromAddress": self.from_address, "toAddress": to, "subject": subject,
                   "content": body, "mailFormat": "plaintext", "action": "forward"}
        if cc:
            payload["ccAddress"] = cc
        self._req("POST", self._acct(f"/messages/{message_id}"), json=payload)
