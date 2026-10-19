"""Minimal SNMPv2c client (stdlib only) for reading interface octet counters.

Used because the ONT's web API exposes no traffic counters; the agent is the
only source of per-interface byte volumes.
"""

from __future__ import annotations

import socket
from typing import Iterator

# ifXTable (RFC 2863) columns
IF_NAME = 1
IF_HC_IN_OCTETS = 6
IF_HC_OUT_OCTETS = 10
IFX_BASE = "1.3.6.1.2.1.31.1.1.1"

_INT_TAGS = {0x02, 0x44}
_NUM_TAGS = {0x21, 0x41, 0x22, 0x42, 0x25, 0x46, 0x27, 0x47, 0x43, 0x23}


def _enc_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _enc_len(len(value)) + value


def _int(n: int) -> bytes:
    if n == 0:
        return _tlv(0x02, b"\x00")
    return _tlv(0x02, n.to_bytes((n.bit_length() + 15) // 8, "big"))


def _enc_oid(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    out = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.insert(0, (p & 0x7F) | 0x80)
            p >>= 7
        out.extend(chunk)
    return _tlv(0x06, bytes(out))


def _dec_oid(b: bytes, i: int):
    first = b[i]
    i += 1
    oid = [first // 40, first % 40]
    while i < len(b):
        v = 0
        while True:
            c = b[i]
            i += 1
            v = (v << 7) | (c & 0x7F)
            if not c & 0x80:
                break
        oid.append(v)
    return ".".join(map(str, oid)), i


def _read(b: bytes, i: int):
    """Return (tag, content, next_offset)."""
    tag = b[i]
    n = b[i + 1]
    j = i + 2
    if n >= 0x80:
        k = n & 0x7F
        n = int.from_bytes(b[j:j + k], "big")
        j += k
    return tag, b[j:j + n], j + n


def _value(tag: int, raw: bytes):
    if tag in _INT_TAGS:
        return int.from_bytes(raw, "big", signed=True) if raw else 0
    if tag in _NUM_TAGS:
        return int.from_bytes(raw, "big") if raw else 0
    if tag == 0x06:
        return _dec_oid(raw, 0)[0]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.hex()


class SnmpAgent:
    def __init__(self, host: str = "192.168.1.1", community: str = "public",
                 port: int = 161, timeout: float = 2.5):
        self.host = host
        self.community = community
        self.port = port
        self.timeout = timeout

    def _exchange(self, oids: list[str], getnext: bool = False,
                  req_id: int = 1) -> list[tuple[str, object]]:
        vb = b"".join(_tlv(0x30, _enc_oid(o) + _tlv(0x05, b"")) for o in oids)
        body = _int(req_id) + _int(0) + _int(len(oids)) + _tlv(0x30, vb)
        pdu = _tlv(0xA1 if getnext else 0xA0, body)
        msg = _tlv(0x30, _tlv(0x02, b"\x01") + _tlv(0x04, self.community.encode()) + pdu)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout)
            sock.sendto(msg, (self.host, self.port))
            data, _ = sock.recvfrom(65535)
        _, envelope, _ = _read(data, 0)
        i = 0
        for _ in range(2):  # version, community
            _, _, i = _read(envelope, i)
        _, pdu_raw, _ = _read(envelope, i)
        j = 0
        _, _, j = _read(pdu_raw, j)                       # request id
        _, err_raw, j = _read(pdu_raw, j)                 # error status
        _, err_idx_raw, j = _read(pdu_raw, j)             # error index
        err = int.from_bytes(err_raw, "big")
        if err:
            raise SnmpError(err, int.from_bytes(err_idx_raw, "big"))
        _, vb_list, _ = _read(pdu_raw, j)
        out = []
        k = 0
        while k < len(vb_list):
            _, single, k2 = _read(vb_list, k)
            m = 0
            _, oid_raw, m = _read(single, m)
            oid, _ = _dec_oid(oid_raw, 0)
            tag, val_raw, m = _read(single, m)
            out.append((oid, _value(tag, val_raw)))
            k = k2
        return out

    def get(self, *oids: str) -> dict[str, object]:
        return dict(self._exchange(list(oids)))

    def walk(self, subtree: str, limit: int = 300) -> Iterator[tuple[str, object]]:
        """GETNEXT-walk every row whose OID starts with `subtree`."""
        prefix = subtree.rstrip(".")
        cur = subtree
        for _ in range(limit):
            try:
                res = self._exchange([cur], getnext=True)
            except (OSError, SnmpError):
                return
            if not res:
                return
            oid, val = res[0]
            if not oid.startswith(prefix + "."):
                return
            yield oid, val
            cur = oid

    def interface_table(self) -> dict[int, dict]:
        """ifIndex -> {name, in_octets, out_octets} using 64-bit counters."""
        table: dict[int, dict] = {}
        for col, key in ((IF_NAME, "name"), (IF_HC_IN_OCTETS, "in_octets"),
                         (IF_HC_OUT_OCTETS, "out_octets")):
            for oid, val in self.walk(f"{IFX_BASE}.{col}"):
                idx = int(oid.rsplit(".", 1)[1])
                row = table.setdefault(idx, {"name": f"#{idx}", "in_octets": 0, "out_octets": 0})
                row[key] = val
        return dict(sorted(table.items()))

    def sys_info(self) -> dict:
        res = self.get("1.3.6.1.2.1.1.1", "1.3.6.1.2.1.1.3", "1.3.6.1.2.1.1.5")
        return {"descr": res.get("1.3.6.1.2.1.1.1"),
                "uptime_ticks": res.get("1.3.6.1.2.1.1.3"),
                "name": res.get("1.3.6.1.2.1.1.5")}


class SnmpError(RuntimeError):
    def __init__(self, status: int, index: int):
        super().__init__(f"SNMP error-status={status} varbind-index={index}")
        self.status, self.index = status, index
