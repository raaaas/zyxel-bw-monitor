#!/usr/bin/env python3
"""Write a synthetic history database for previews, docs and screenshots.

Nothing here comes from a real device: identities use RFC 5737 documentation
addresses and the numbers follow a generated diurnal traffic model. Useful for
seeing what the dashboard looks like before pointing it at an ONT.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sqlite3

from read_usage import SCHEMA

# ifIndex -> name, using the interface set this ONT family reports.
INTERFACES = {
    8: "eth0", 9: "eth0.1", 10: "eth0.2", 11: "eth0.3", 12: "eth0.4",
    19: "pon", 21: "ra0", 22: "br0", 26: "nas0_3", 2: "rai0",
}
# Average throughput per port in Mbit/s (before the diurnal factor), and the
# share of it that is a download. Sized to land near 6 GB/day in total.
PORT_PROFILE = {
    "eth0.1": (0.75, 0.86), "eth0.3": (0.40, 0.78), "eth0.2": (0.03, 0.6),
    "eth0.4": (0.02, 0.5), "ra0": (0.22, 0.82), "rai0": (0.0, 0.5),
}
IDENTITY = {
    "model": "PMG5617T20B2", "serial": "DEMO000000000", "sw_version": "V5.41(DEMO.1)G0",
    "first_use": "2026-01-05T09:00:00", "wan_type": "GPON", "login_level": "high",
}


def diurnal(hour: float) -> float:
    """Busy in the evening, near-silent at 04:00."""
    if 1 <= hour < 7:
        return 0.08
    if 7 <= hour < 18:
        return 0.45
    if 18 <= hour < 24:
        return 1.0
    return 0.6


def build(days: int, step_min: int, seed: int) -> list[tuple]:
    rng = random.Random(seed)
    start = (dt.datetime.now() - dt.timedelta(days=days)).replace(
        minute=0, second=0, microsecond=0, tzinfo=dt.datetime.now().astimezone().tzinfo)
    step = dt.timedelta(minutes=step_min)
    counters = {name: [0, 0] for name in INTERFACES.values()}
    uptime = 0
    rows_samples, rows_counters = [], []
    reboot_at = start + dt.timedelta(days=days // 2)
    t = start
    while t < start + dt.timedelta(days=days):
        load = diurnal(t.hour + t.minute / 60) * rng.uniform(0.55, 1.5)
        seconds = step.total_seconds()
        for name in INTERFACES.values():
            avg, down_share = PORT_PROFILE.get(name, (0.0, 0.5))
            total = avg * 1e6 / 8 * load * seconds * rng.uniform(0.3, 1.7)
            counters[name][0] += int(total * (1 - down_share))  # in  = upload on a LAN port
            counters[name][1] += int(total * down_share)        # out = download on a LAN port
        reboot = False
        if abs(t - reboot_at) < step / 2:
            uptime, reboot = 0, True
            for c in counters.values():
                c[0] = c[1] = 0
        uptime += int(seconds)
        rx = round(-16.4 - 1.6 * load / 1.5 + rng.uniform(-0.25, 0.25), 2)
        rows_samples.append((
            t.isoformat(), uptime, min(78, int(4 + load * 26)), 71000 - int(load * 900), 232052,
            rx, round(2.6 + rng.uniform(-0.3, 0.4), 2), round(44 + load * 9 + rng.uniform(-1, 1), 2),
            round(3.24 + rng.uniform(-0.03, 0.03), 2), "WAN4", "192.0.2.10",
            rng.choice([4, 5, 5, 6, 7]),
            json.dumps({"identity": IDENTITY,
                        "wan": {"service": "WAN4", "ifname": "nas0_3", "ip": "192.0.2.10"},
                        "reboot": reboot})))
        for name in INTERFACES.values():
            rows_counters.append((t.isoformat(), name, counters[name][0], counters[name][1]))
        t += step
    return rows_samples, rows_counters


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="demo_history.db")
    ap.add_argument("--days", type=int, default=70)
    ap.add_argument("--step-min", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    samples, counters = build(args.days, args.step_min, args.seed)
    con = sqlite3.connect(args.out)
    con.executescript(SCHEMA)
    con.executemany("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", samples)
    con.executemany("INSERT OR REPLACE INTO counters VALUES (?,?,?,?)", counters)
    con.commit()
    con.close()
    print(f"wrote {len(samples)} synthetic samples ({args.days} days) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
