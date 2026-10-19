#!/usr/bin/env python3
"""Read live usage and health from a Zyxel fiber ONT and record history.

The ONT's own web API exposes no byte counters, so volumes come from the SNMP
agent's 64-bit interface counters while health/status comes from the DAL.
Cumulative counters are stored raw; deltas (and therefore monthly totals) are
derived afterwards by dashboard.py.

Password comes from --password, --password-file (chmod 600), $ZX_PW, or an
interactive prompt; it is never written to the history database or to any file
this tool produces.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import sqlite3
import sys
import time

from snmp_client import SnmpAgent
from zyxel_client import DalError, ZyxelONT

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts TEXT PRIMARY KEY,
  uptime_s INTEGER,
  cpu_percent REAL,
  mem_free_kb INTEGER,
  mem_total_kb INTEGER,
  rx_power_dbm REAL,
  tx_power_dbm REAL,
  temperature_c REAL,
  voltage REAL,
  wan_srv TEXT,
  wan_ip TEXT,
  devices INTEGER,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS counters (
  ts TEXT,
  iface TEXT,
  in_octets INTEGER,
  out_octets INTEGER,
  PRIMARY KEY (ts, iface)
);
"""

# Counters that proved to track real subscriber traffic on this ONT: the LAN
# access ports and the radio interfaces. Parents (eth0, br0) would double count.
LAN_PORT_PREFIXES = ("eth0.", "ath", "ra0", "rai0", "wlan")
WAN_PREFIXES = ("nas", "pon", "ppp", "gem", "wan_int")


def human(nbytes: float) -> str:
    sign = "-" if nbytes < 0 else ""
    n = abs(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{sign}{n:,.0f} {unit}" if unit == "B" else f"{sign}{n:,.1f} {unit}"
        n /= 1024
    return f"{sign}{n:.1f} PB"


def human_bps(bps: float) -> str:
    for unit, div in (("Gbit/s", 1e9), ("Mbit/s", 1e6), ("kbit/s", 1e3)):
        if bps >= div:
            return f"{bps / div:,.2f} {unit}"
    return f"{bps:,.0f} bit/s"


def classify(name: str) -> str:
    if name.startswith(LAN_PORT_PREFIXES):
        return "lan"
    if name.startswith(WAN_PREFIXES):
        return "wan"
    return "other"


def directions(role: str, d_in: int, d_out: int) -> tuple[int, int]:
    """Map SNMP in/out deltas to (down, up) for a given interface role.

    On a LAN access port ifInOctets is traffic arriving *from* the subscriber
    device, i.e. an upload, which is the opposite of an uplink interface.
    Verified against a controlled 50 MB download on this ONT.
    """
    if role == "lan":
        return d_out, d_in
    if role == "wan":
        return d_in, d_out
    return 0, 0  # eth0/br0 are aggregates; summing them would double count


def iso_now() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def read_ont(args) -> dict:
    ont = ZyxelONT(host=args.host, username=args.username, password=args.password)
    login = ont.login()
    status = ont.oid("status")["Object"][0]
    dev = status.get("DeviceInfo", {})
    sysinfo = status.get("SystemInfo", {})
    gpon = status.get("GponStatsInfo", {}).get("gponStatusObj", {})
    wan = [w for w in status.get("WanLanInfo", [])
           if w.get("Status") == "Up" and w.get("Name") == "WAN"]
    active = wan[0] if wan else {}
    out = {
        "identity": {
            "model": dev.get("ModelName"),
            "serial": dev.get("SerialNumber"),
            "sw_version": dev.get("SoftwareVersion"),
            "first_use": dev.get("FirstUseDate"),
            "wan_type": status.get("WanType"),
            "login_level": login.get("loginLevel"),
        },
        "uptime_s": dev.get("UpTime"),
        "cpu_percent": sysinfo.get("CPUUsage"),
        "mem_free_kb": sysinfo.get("Free"),
        "mem_total_kb": sysinfo.get("Total"),
        "optics": {
            "rx_power_dbm": _num(gpon.get("rxPower")),
            "tx_power_dbm": _num(gpon.get("txPower")),
            "temperature_c": _num(gpon.get("temperature")),
            "voltage": _num(gpon.get("voltage")),
        },
        "wan": {"service": active.get("X_ZYXEL_SrvName"),
                "ifname": active.get("X_ZYXEL_IfName"),
                "ip": next((a.get("IPAddress") for a in active.get("IPv4Address", [])
                            if a.get("IPAddress")), None)},
        "lan_ports": [{"port": p.get("X_ZYXEL_LanPort"), "status": p.get("Status"),
                       "speed_mbps": p.get("MaxBitRate"), "mac": p.get("MACAddress")}
                      for p in status.get("LanPortInfo", [])],
        "wifi": [{"ssid": w.get("SSID"), "band": w.get("OperatingFrequencyBand"),
                  "channel": w.get("Channel"), "enabled": w.get("Enable")}
                 for w in status.get("WiFiInfo", [])],
    }
    try:
        hosts = ont.oid("lanhosts")["Object"][0].get("lanhosts", [])
        out["devices"] = [{"mac": h.get("PhysAddress"),
                           "ip": h.get("IPAddress"),
                           "name": h.get("curHostName") or h.get("HostName") or "",
                           "connection": h.get("X_ZYXEL_ConnectionType"),
                           "active": h.get("X_ZYXEL_Active") or h.get("Active")}
                          for h in hosts]
    except DalError:
        out["devices"] = []
    ont.logout()
    return out


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def record(db_path: str, sample: dict, counters: dict[int, dict], prev_uptime: int | None):
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    ts = sample["ts"]
    opt = sample["optics"]
    con.execute(
        "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, sample["uptime_s"], sample["cpu_percent"], sample["mem_free_kb"],
         sample["mem_total_kb"], opt["rx_power_dbm"], opt["tx_power_dbm"],
         opt["temperature_c"], opt["voltage"],
         sample["wan"]["service"], sample["wan"]["ip"], len(sample["devices"]),
         json.dumps({"identity": sample["identity"], "reboot":
                     bool(prev_uptime and sample["uptime_s"] < prev_uptime)})))
    con.executemany("INSERT OR REPLACE INTO counters VALUES (?,?,?,?)",
                    [(ts, row["name"], row["in_octets"], row["out_octets"])
                     for row in counters.values()])
    con.commit()
    con.close()


def last_uptime(db_path: str) -> int | None:
    try:
        con = sqlite3.connect(db_path)
        row = con.execute("SELECT uptime_s FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="https://192.168.1.1")
    ap.add_argument("--snmp-host", default="192.168.1.1")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default=None, help="default: $ZX_PW")
    ap.add_argument("--password-file", default=None,
                    help="read the password from a file (chmod 600) for unattended runs")
    ap.add_argument("--community", default="public")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="seconds between the two live-rate samples")
    ap.add_argument("--record", metavar="DB", nargs="?", const="usage_history.db",
                    help="append this sample to the history database")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    if not args.password and args.password_file:
        with open(args.password_file, encoding="utf-8") as fh:
            args.password = fh.readline().strip()
    if not args.password:
        args.password = os.getenv("ZX_PW")
    if not args.password and sys.stdin.isatty():
        args.password = getpass.getpass(f"password for {args.username}@{args.host}: ")
    if not args.password:
        ap.error("no password: set ZX_PW, or pass --password / --password-file")

    sample = read_ont(args)

    agent = SnmpAgent(host=args.snmp_host, community=args.community)
    first = agent.interface_table()
    t_first = time.monotonic()
    time.sleep(args.interval)
    second = agent.interface_table()
    dt_s = max(time.monotonic() - t_first, 1e-6)

    rows = []
    for idx, cur in second.items():
        prev = first.get(idx, {"in_octets": cur["in_octets"], "out_octets": cur["out_octets"]})
        d_in = max(cur["in_octets"] - prev["in_octets"], 0)
        d_out = max(cur["out_octets"] - prev["out_octets"], 0)
        role = classify(cur["name"])
        d_down, d_up = directions(role, d_in, d_out)
        rows.append({"ifindex": idx, "name": cur["name"], "role": role,
                     "in_octets": cur["in_octets"], "out_octets": cur["out_octets"],
                     "d_in": d_in, "d_out": d_out, "d_down": d_down, "d_up": d_up,
                     "rate_down_bps": d_down * 8 / dt_s, "rate_up_bps": d_up * 8 / dt_s,
                     "counter_reset": cur["in_octets"] < prev["in_octets"]
                                      or cur["out_octets"] < prev["out_octets"]})
    sample["ts"] = iso_now()
    sample["sample_window_s"] = round(dt_s, 2)
    sample["interfaces"] = rows

    if args.record:
        sample["uptime_previous_s"] = last_uptime(args.record)
        record(args.record, sample, second, sample["uptime_previous_s"])

    if args.json:
        json.dump(sample, sys.stdout, indent=1, default=str)
        print()
        return 0

    ident = sample["identity"]
    up = sample["uptime_s"] or 0
    print(f"{ident['model']}  fw {ident['sw_version']}  WAN {ident['wan_type']}"
          f"  (session level: {ident['login_level']})")
    print(f"up {up // 86400}d {(up % 86400) // 3600}h   first use {ident['first_use']}")
    o = sample["optics"]
    print(f"optics: rx {o['rx_power_dbm']} dBm   tx {o['tx_power_dbm']} dBm   "
          f"{o['temperature_c']} C   {o['voltage']} V")
    w = sample["wan"]
    print(f"wan: {w['service']} via {w['ifname']}  ip {w['ip']}")
    print(f"cpu {sample['cpu_percent']}%   mem {human((sample['mem_total_kb'] or 0) * 1024)}"
          f" total   devices {len(sample['devices'])}")

    print(f"\nlive rates over {sample['sample_window_s']}s")
    print(f"  {'iface':<12}{'role':<6}{'down':>14}{'up':>14}{'since counter reset':>20}")
    for r in sorted(rows, key=lambda x: -(x["d_in"] + x["d_out"])):
        if r["role"] == "other":
            continue  # eth0/br0 are aggregates, omci is GPON management traffic
        if r["rate_down_bps"] < 1 and r["rate_up_bps"] < 1 and not r["in_octets"]:
            continue
        flag = " (reset)" if r["counter_reset"] else ""
        print(f"  {r['name']:<12}{r['role']:<6}"
              f"{human_bps(r['rate_down_bps']):>14}{human_bps(r['rate_up_bps']):>14}"
              f"{human(r['in_octets'] + r['out_octets']):>20}{flag}")

    moved = sum(r["d_down"] + r["d_up"] for r in rows if r["role"] == "lan")
    print(f"\n  moved in this window (LAN access ports): {human(moved)}"
          f"  [{human(sum(r['d_down'] for r in rows if r['role'] == 'lan'))} down /"
          f" {human(sum(r['d_up'] for r in rows if r['role'] == 'lan'))} up]")
    print("  totals sum per-port counters; aggregates (eth0, br0) are excluded to avoid double counting")
    if args.record:
        print(f"  recorded -> {args.record}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
