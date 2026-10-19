"""Minimal authenticated client for the Zyxel ONT web API (DAL interface).

The web UI wraps every cgi-bin request in AES-256-CBC, with the session key
RSA-encrypted to the device on login. Credentials are never stored in this
file; they come from arguments or the environment.
"""

from __future__ import annotations

import base64
import html
import json
import os
import urllib3
from typing import Any

import requests
from cryptography.hazmat.primitives import padding as sympad
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_pem_public_key

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_HOST = "https://192.168.1.1"


def _pkcs7_pad(data: bytes) -> bytes:
    p = sympad.PKCS7(128).padder()
    return p.update(data) + p.finalize()


def _pkcs7_unpad(data: bytes) -> bytes:
    u = sympad.PKCS7(128).unpadder()
    return u.update(data) + u.finalize()


def _cbc(key: bytes, iv: bytes) -> tuple[Cipher, Any]:
    # CryptoJS CBC consumes only the leading 16 bytes of the IV word array.
    return Cipher(algorithms.AES(key), modes.CBC(iv[:16]))


class ZyxelONT:
    def __init__(self, host: str | None = None, username: str | None = None,
                 password: str | None = None, timeout: int = 20):
        self.base = (host or os.getenv("ZX_HOST", DEFAULT_HOST)).rstrip("/")
        self.username = username or os.getenv("ZX_USER", "admin")
        self.password = password or os.getenv("ZX_PW") or os.getenv("ZX_PASS")
        if not self.password:
            raise ValueError("no password: pass password= or set ZX_PW")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["If-Modified-Since"] = "Thu, 01 Jun 1970 00:00:00 GMT"
        # The ONT serves a self-signed certificate; ZX_CA trusts a specific PEM.
        ca = os.getenv("ZX_CA")
        self.session.verify = ca if ca else False
        self._key: bytes | None = None
        self._csrf: str | None = None
        self.login_info: dict = {}
        self.logged_in = False

    # --- crypto -----------------------------------------------------------
    @staticmethod
    def _encrypt(plain: str, key: bytes, iv: bytes) -> str:
        enc = _cbc(key, iv).encryptor()
        return base64.b64encode(enc.update(_pkcs7_pad(plain.encode())) + enc.finalize()).decode()

    @staticmethod
    def _decrypt(cipher_b64: str, key: bytes, iv: bytes) -> str:
        dec = _cbc(key, iv).decryptor()
        raw = dec.update(base64.b64decode(cipher_b64)) + dec.finalize()
        return _pkcs7_unpad(raw).decode()

    # --- transport --------------------------------------------------------
    def _unwrap(self, resp: requests.Response) -> dict:
        """Return a decoded JSON payload, decrypting when the device wraps it."""
        text = resp.text
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            resp.raise_for_status()
            raise RuntimeError(f"expected JSON from {resp.url!r}, got: {text[:200]!r}")
        if isinstance(data, dict) and "content" in data and "iv" in data and self._key:
            data = json.loads(self._decrypt(data["content"], self._key,
                                            base64.b64decode(data["iv"])))
        if isinstance(data, str):
            data = json.loads(html.unescape(data))
        return data

    def _send(self, method: str, path: str, *, params: dict | None = None,
              body: Any = None, csrf: bool = False) -> dict:
        headers = {}
        if csrf and self._csrf:
            headers["CSRFToken"] = self._csrf
        payload = None
        if body is not None:
            if self._key:
                iv = os.urandom(32)
                blob = json.dumps(body) if not isinstance(body, str) else body
                payload = json.dumps({
                    "content": self._encrypt(blob, self._key, iv),
                    "iv": base64.b64encode(iv).decode(),
                })
                headers["Content-Type"] = "application/json"
            else:
                payload = json.dumps(body)
        resp = self.session.request(method, self.base + path, params=params,
                                    data=payload, headers=headers, timeout=self.timeout)
        data = self._unwrap(resp)
        if isinstance(data, dict) and data.get("sessionkey"):
            self._csrf = data["sessionkey"]
        if isinstance(data, dict) and data.get("result") not in (None, "ZCFG_SUCCESS"):
            raise DalError(data.get("result", "unknown"), path, data)
        return data

    # --- public API -------------------------------------------------------
    def login(self) -> dict:
        pub_pem = self.session.get(f"{self.base}/getRSAPublickKey",
                                   timeout=self.timeout).json()["RSAPublicKey"]
        if pub_pem.strip() in ("", "None"):
            raise RuntimeError("device returned no RSA public key")
        public = load_pem_public_key(pub_pem.encode())

        self._key = os.urandom(32)
        iv = os.urandom(32)
        key_b64 = base64.b64encode(self._key).decode()
        body = {
            "Input_Account": self.username,
            "Input_Passwd": base64.b64encode(self.password.encode()).decode(),
            "currLang": "en",
            "RememberPassword": 0,
            "SHA512_password": False,
        }
        envelope = {
            "content": self._encrypt(json.dumps(body), self._key, iv),
            "key": base64.b64encode(public.encrypt(key_b64.encode(), PKCS1v15())).decode(),
            "iv": base64.b64encode(iv).decode(),
        }
        resp = self.session.post(f"{self.base}/UserLogin", json=envelope,
                                 timeout=self.timeout)
        data = self._unwrap(resp)
        if data.get("result") != "ZCFG_SUCCESS":
            self._key = None
            raise RuntimeError(f"login failed: {data.get('result') or data}")
        self._csrf = data.get("sessionkey")
        self.login_info = data
        self.logged_in = True
        return data

    def logout(self) -> None:
        try:
            self._send("GET", "/cgi-bin/UserLogout", params={"Logout": "1"}, csrf=True)
        finally:
            self.session.close()

    def get(self, path: str, **params) -> dict:
        """Plain endpoint under /cgi-bin (e.g. /cgi-bin/getBasicInformation)."""
        return self._send("GET", path, params=params or None, csrf=True)

    def oid(self, name: str, instance: int | None = None) -> dict:
        """Read an object from the Data Access Layer: /cgi-bin/DAL?oid=<name>."""
        params = {"oid": name}
        if instance is not None:
            params["instance"] = instance
        return self._send("GET", "/cgi-bin/DAL", params=params, csrf=True)


class DalError(RuntimeError):
    def __init__(self, result: str, path: str, payload: dict):
        super().__init__(f"{path}: {result}")
        self.result, self.path, self.payload = result, path, payload
