#!/usr/bin/env python3
"""Render a self-contained HTML dashboard from usage_history.db.

Charts are generated with matplotlib and embedded as base64 PNGs, so the file
opens offline with no external requests. Data volumes come from deltas between
samples taken by read_usage.py --record.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import io
import json
import sqlite3

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from read_usage import classify, directions, human  # noqa: E402

ACCENT = "#2f7fd1"
DOWN, UP = "#2f7fd1", "#e08b2e"
GRID = "#e6e9ee"


def load(db_path: str):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    samples = [dict(r) for r in con.execute("SELECT * FROM samples ORDER BY ts")]
    counters: dict[str, dict[str, tuple[int, int]]] = {}
    for r in con.execute("SELECT ts, iface, in_octets, out_octets FROM counters ORDER BY ts"):
        counters.setdefault(r["ts"], {})[r["iface"]] = (r["in_octets"], r["out_octets"])
    con.close()
    return samples, counters


def parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def interval_deltas(samples, counters):
    """[(start_ts, iface, role, bytes_down, bytes_up)] skipping reset/missing."""
    out = []
    for prev, cur in zip(samples, samples[1:]):
        a, b = counters.get(prev["ts"], {}), counters.get(cur["ts"], {})
        if not a or not b:
            continue
        seconds = (parse_ts(cur["ts"]) - parse_ts(prev["ts"])).total_seconds()
        rebooted = bool(json.loads(cur.get("detail") or "{}").get("reboot"))
        if seconds <= 0 or seconds > 3 * 86400 or rebooted:
            continue
        for iface, (in_b, out_b) in b.items():
            if iface not in a:
                continue
            d_in, d_out = in_b - a[iface][0], out_b - a[iface][1]
            if d_in < 0 or d_out < 0:
                continue  # counter reset on that interface
            out.append((prev["ts"], iface, classify(iface), d_in, d_out, seconds))
    return out


def roll(deltas, key):
    """Totals per bucket as [down, up]; only LAN access ports are trusted."""
    totals: dict[object, list[float]] = {}
    for ts, _iface, role, d_in, d_out, _s in deltas:
        if role != "lan":
            continue
        down, up = directions(role, d_in, d_out)
        bucket = key(parse_ts(ts))
        row = totals.setdefault(bucket, [0.0, 0.0])
        row[0] += down
        row[1] += up
    return dict(sorted(totals.items()))


def fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=115, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def bar_chart(labels, series, title, yunit="GB", stacked=True):
    fig, ax = plt.subplots(figsize=(7.6, 2.9))
    xs = range(len(labels))
    bottom = [0] * len(labels)
    for name, values, color in series:
        ax.bar(xs, values, bottom=bottom if stacked else None, label=name,
               color=color, width=0.68)
        if stacked:
            bottom = [b + v for b, v in zip(bottom, values)]
    # Long windows would otherwise produce overlapping date labels.
    step = max(1, -(-len(labels) // 14))
    ticks = list(range(0, len(labels), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([labels[i] for i in ticks], fontsize=8, rotation=45, ha="right")
    ax.set_ylabel(f"{yunit}")
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    ax.set_title(title, fontsize=11, loc="left")
    return fig_to_b64(fig)


def line_chart(labels, series, title, ylabel=""):
    fig, ax = plt.subplots(figsize=(7.6, 2.6))
    for name, values, color, dash in series:
        ax.plot(range(len(values)), values, color=color, linestyle=dash,
                marker="o" if len(values) < 40 else None, ms=3, lw=1.4, label=name)
    step = max(1, len(labels) // 8)
    ax.set_xticks(range(0, len(labels), step))
    ax.set_xticklabels([labels[i] for i in range(0, len(labels), step)], fontsize=8,
                       rotation=45, ha="right")
    ax.set_ylabel(ylabel)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.legend(fontsize=8, frameon=False, loc="best")
    ax.set_title(title, fontsize=11, loc="left")
    return fig_to_b64(fig)


def port_chart(totals) -> str:
    active = {k: v for k, v in totals.items() if v[0] or v[1]}
    if not active:
        return ""
    order = sorted(active.items(), key=lambda kv: kv[1][0] + kv[1][1])[-12:]
    names = [k for k, _ in order]
    downs = [v[0] / 2**30 for _, v in order]
    ups = [v[1] / 2**30 for _, v in order]
    fig, ax = plt.subplots(figsize=(7.6, max(2.2, 0.42 * len(names))))
    ys = range(len(names))
    ax.barh(list(ys), downs, color=DOWN, height=0.6, label="download")
    ax.barh(list(ys), ups, left=downs, color=UP, height=0.6, label="upload")
    ax.set_yticks(list(ys))
    ax.set_yticklabels(names, fontsize=9)
    ax.set_xlabel("GB")
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.legend(fontsize=8, frameon=False, loc="lower right")
    ax.set_title("Volume per interface (LAN access ports)", fontsize=11, loc="left")
    return fig_to_b64(fig)


def tile(label, value, note=""):
    return (f'<div class="tile"><div class="tl">{html.escape(label)}</div>'
            f'<div class="tv">{html.escape(str(value))}</div>'
            f'<div class="tn">{html.escape(note)}</div></div>')


def img(name, b64):
    if not b64:
        return ""
    return f'<div class="card"><img alt="{html.escape(name)}" src="data:image/png;base64,{b64}"></div>'


def build(db_path: str, days: int) -> str:
    samples, counters = load(db_path)
    if not samples:
        raise SystemExit(f"no samples in {db_path}: run read_usage.py --record {db_path} first")

    latest = samples[-1]
    detail = json.loads(latest.get("detail") or "{}")
    ident = detail.get("identity", {})
    ts_all = [parse_ts(s["ts"]) for s in samples]
    first_ts, last_ts = ts_all[0], ts_all[-1]

    deltas = interval_deltas(samples, counters)
    cutoff = last_ts - dt.timedelta(days=days)
    recent = [d for d in deltas if parse_ts(d[0]) >= cutoff]

    daily = roll(recent, lambda t: t.date())
    monthly = roll(deltas, lambda t: (t.year, t.month))
    ports: dict[str, list[float]] = {}
    for _ts, iface, role, d_in, d_out, _s in recent:
        if role != "lan":
            continue
        down, up = directions(role, d_in, d_out)
        row = ports.setdefault(iface, [0.0, 0.0])
        row[0] += down
        row[1] += up

    day_labels = [d.strftime("%m-%d") for d in daily]
    daily_gb = [[v[0] / 2**30 for v in daily.values()], [v[1] / 2**30 for v in daily.values()]]
    days_word = "day" if len(daily) == 1 else "days"
    ch_daily = (bar_chart(day_labels, [("down", daily_gb[0], DOWN), ("up", daily_gb[1], UP)],
                          f"Daily volume ({len(daily)} {days_word} recorded)") if daily else "")

    mon_labels = [f"{y}-{m:02d}" for (y, m) in monthly]
    mon_vals = [[v[0] / 2**30 for v in monthly.values()], [v[1] / 2**30 for v in monthly.values()]]
    ch_monthly = (bar_chart(mon_labels, [("down", mon_vals[0], DOWN), ("up", mon_vals[1], UP)],
                            "Volume per month") if monthly else "")

    health_labels = [s["ts"][5:16].replace("T", " ") for s in samples][-120:]
    pick = lambda k: [s[k] for s in samples][-120:]  # noqa: E731
    rx, tx = pick("rx_power_dbm"), pick("tx_power_dbm")
    ch_optics = (line_chart(health_labels,
                            [("RX power (dBm)", rx, ACCENT, "-"),
                             ("TX power (dBm)", tx, "#7b5cd6", "--")],
                            "GPON optical power", "dBm")
                 if any(v is not None for v in rx) else "")
    temp, cpu = pick("temperature_c"), pick("cpu_percent")
    ch_env = (line_chart(health_labels,
                         [("ONT temperature (C)", temp, "#c0392b", "-"),
                          ("CPU %", cpu, "#16a085", "--")],
                         "Thermals and CPU")
              if any(v is not None for v in temp) else "")

    total_down = sum(v[0] for v in daily.values()) if daily else 0
    total_up = sum(v[1] for v in daily.values()) if daily else 0
    reboots = sum(1 for s in samples if json.loads(s.get("detail") or "{}").get("reboot"))
    coverage_h = max((last_ts - first_ts).total_seconds() / 3600, 0)

    # Volumes that the device itself still reports, counted since each interface
    # counter last reset; useful as a coarse back-dated estimate only.
    since_reset = counters[latest["ts"]]
    lan_cum = {k: v for k, v in since_reset.items() if classify(k) == "lan"}
    est_down = sum(directions("lan", v[0], v[1])[0] for v in lan_cum.values())
    est_up = sum(directions("lan", v[0], v[1])[1] for v in lan_cum.values())

    up = latest["uptime_s"] or 0
    rows = "".join(
        f"<tr><td>{d}</td><td>{human(v[0])}</td><td>{human(v[1])}</td>"
        f"<td>{human(v[0] + v[1])}</td></tr>"
        for d, v in list(daily.items())[-31:][::-1])

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(str(ident.get('model', 'ONT')))} usage</title><style>
body{{margin:0;background:#f5f7fa;color:#22262b;font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}}
header{{background:#1d2733;color:#fff;padding:18px 26px}}
header h1{{margin:0;font-size:19px;font-weight:600}}
header .sub{{color:#9fb0c2;font-size:12.5px;margin-top:4px}}
main{{max-width:1080px;margin:0 auto;padding:20px 18px 44px}}
.tiles{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}}
@media (max-width:900px){{.tiles{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
@media (max-width:520px){{.tiles{{grid-template-columns:minmax(0,1fr)}}}}
.tile{{background:#fff;border:1px solid #e3e8ef;border-radius:9px;padding:12px 14px}}
.tl{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#7d8896}}
.tv{{font-size:20px;font-weight:650;margin-top:3px}}
.tn{{font-size:11.5px;color:#8b95a3}}
.note{{background:#fff8e6;border:1px solid #f0dfa8;border-radius:9px;padding:11px 14px;margin:0 0 16px;font-size:13px}}
.charts{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}
@media (max-width:900px){{.charts{{grid-template-columns:minmax(0,1fr)}}}}
.card{{background:#fff;border:1px solid #e3e8ef;border-radius:9px;padding:8px}}
.card img{{width:100%;height:auto;display:block}}
table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e3e8ef;border-radius:9px;font-size:13px}}
th,td{{padding:7px 11px;text-align:left;border-bottom:1px solid #eef1f5}}
th{{background:#f2f5f9;font-weight:600;font-size:12px}}
h2{{font-size:15px;margin:26px 0 10px}}
</style></head><body>
<header><h1>{html.escape(str(ident.get('model', 'ONT')))} &mdash; traffic &amp; health</h1>
<div class="sub">{html.escape(str(ident.get('wan_type', '')))} &middot; firmware {html.escape(str(ident.get('sw_version', '')))}
 &middot; serial {html.escape(str(ident.get('serial', '')))} &middot; generated {html.escape(latest['ts'])}</div></header>
<main>
<div class="note"><b>History depth.</b> This ONT keeps no monthly usage totals and its web API exposes no
traffic counters, so every day/month figure below is integrated from locally recorded samples
({coverage_h:.1f} h of history, {len(samples)} samples). Usage before sampling began cannot be recovered from
the device. The "since counter reset" tile is the only back-dated number available; it is a rough lower
bound because individual interface counters restart on link flaps and hardware-offloaded paths are counted
unevenly.</div>
<div class="tiles">
{tile("Recorded total", human(total_down + total_up), f"{human(total_down)} down / {human(total_up)} up")}
{tile("Devices seen", latest['devices'], "listed on the LAN")}
{tile("WAN", latest['wan_srv'] or "-", f"{latest['wan_ip'] or ''} via {detail.get('wan', {}).get('ifname', '')}")}
{tile("Uptime", f"{up // 86400}d {(up % 86400) // 3600}h", f"reboots detected: {reboots}")}
{tile("RX power", f"{latest['rx_power_dbm']} dBm", "GPON receive")}
{tile("Temperature", f"{latest['temperature_c']} C", f"CPU {latest['cpu_percent']}%")}
{tile("Since counter reset", human(est_down + est_up), f"{human(est_down)} down / {human(est_up)} up")}
</div>
<div class="charts">
{img("Daily volume", ch_daily)}
{img("Monthly volume", ch_monthly)}
{img("Per interface", port_chart(ports))}
{img("Optical power", ch_optics)}
{img("Thermals", ch_env)}
</div>
<h2>Daily detail (most recent first)</h2>
<table><thead><tr><th>Day</th><th>Downloaded</th><th>Uploaded</th><th>Total</th></tr></thead>
<tbody>{rows}</tbody></table>
</main></body></html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="usage_history.db")
    ap.add_argument("--days", type=int, default=90, help="window for daily/per-port charts")
    ap.add_argument("--out", default="dashboard.html")
    args = ap.parse_args(argv)

    page = build(args.db, args.days)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"wrote {args.out} ({len(page) / 1024:.0f} KB) from {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
