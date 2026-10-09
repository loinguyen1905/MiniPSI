#!/usr/bin/env python3
"""Builds results/setup1.csv and results/setup2.csv from results/raw.

With --markdown it also prints one comparison table per metric (rows n x K, columns -r / -e / -i).
"""
import csv
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.environ.get("RAW", os.path.join(HERE, "results", "raw"))
OUT = os.environ.get("OUT", os.path.join(HERE, "results"))
PROTOS = [("r", "-r (DH)"), ("e", "-e (JL10)"), ("i", "-i (MiniPSI)")]
MB = 1024 * 1024


def load_run(run_id):
    """Totals of one run (or one chunk of a run), before averaging."""
    def read(role):
        with open(os.path.join(RAW, f"{run_id}_{role}.json")) as f:
            info = json.load(f)
        with open(os.path.join(RAW, f"{run_id}_{role}.csv")) as f:
            rows = list(csv.DictReader(f))
        return info, rows

    (s, s_rows), (r, r_rows) = read("sender"), read("receiver")
    return {
        "protocol": s["proto"],
        "K": s["k"],
        "batches": len(r_rows),
        "batch_ms_sum": sum(float(x["batch_ms"]) for x in r_rows),
        "sessions": sum(int(x["sessions"]) for x in r_rows),
        "mem_kb": sum(int(x["mem_sum_kb"]) for x in r_rows) + sum(int(x["mem_sum_kb"]) for x in s_rows),
        "correct": sum(int(x["correct"]) for x in r_rows),
        "total_s": s["total_s"],
        "loads": [s["load_start"], s["load_end"], r["load_start"], r["load_end"]],
        "tx": s["tx_bytes"] + r["tx_bytes"] if "tx_bytes" in s and "tx_bytes" in r else None,
    }


def to_row(n, parts):
    """One result line from a run, or from all chunks of a run that was split (run_id ..._cNN)."""
    t = parts[0]
    batches = sum(p["batches"] for p in parts)
    sessions = sum(p["sessions"] for p in parts)
    mem_kb = sum(p["mem_kb"] for p in parts)
    loads = [x for p in parts for x in p["loads"]]
    row = {
        "protocol": t["protocol"],
        "n": n,
        "K": t["K"],
        "batches": batches,
        "avg_batch_ms": round(sum(p["batch_ms_sum"] for p in parts) / batches, 1),
        "total_s": round(sum(p["total_s"] for p in parts), 3),
        "avg_memory_per_session_mb": round(mem_kb * 1024 / (2 * sessions) / MB, 2),
        "avg_memory_per_batch_mb": round(mem_kb * 1024 / batches / MB, 2),
        "correct_sessions": sum(p["correct"] for p in parts),
        "sessions": sessions,
        "load_avg": round(sum(loads) / len(loads), 1),
    }
    if all(p["tx"] is not None for p in parts):
        row["traffic_mb"] = round(sum(p["tx"] for p in parts) / MB, 2)
    return row


def collect(setup):
    groups = {}
    for path in glob.glob(os.path.join(RAW, f"{setup}_*_receiver.json")):
        run_id = os.path.basename(path)[: -len("_receiver.json")]
        if os.path.exists(os.path.join(RAW, f"{run_id}_sender.json")):
            base = re.sub(r"_c\d+$", "", run_id)       # chunks of one run share a base id
            groups.setdefault(base, []).append(load_run(run_id))
    rows = []
    for base, parts in groups.items():
        n = int(re.search(r"_n(\d+)_k", base).group(1))
        if sum(p["sessions"] for p in parts) < n:
            print(f"incomplete, not reported yet: {base} ({sum(p['sessions'] for p in parts):,}/{n:,} pairs)",
                  file=sys.stderr)
            continue
        rows.append(to_row(n, parts))
    order = {p: i for i, (p, _) in enumerate(PROTOS)}
    rows.sort(key=lambda x: (x["n"], x["K"], order[x["protocol"]]))
    return rows


def write_csv(setup, rows):
    if not rows:
        return
    path = os.path.join(OUT, f"{setup}.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path} ({len(rows)} runs)", file=sys.stderr)


def markdown(setup, rows, metric, title, fmt):
    if not rows or metric not in rows[0]:
        return
    table = {}
    for x in rows:
        table.setdefault((x["n"], x["K"]), {})[x["protocol"]] = x[metric]
    print(f"\n**{title}**\n")
    print("| n | K | " + " | ".join(label for _, label in PROTOS) + " |")
    print("|---:|---:|" + "---:|" * len(PROTOS))
    for (n, k), vals in sorted(table.items()):
        cells = [fmt.format(vals[p]) if p in vals else "" for p, _ in PROTOS]
        print(f"| {n:,} | {k} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    for setup in ("setup1", "setup2"):
        rows = collect(setup)
        write_csv(setup, rows)
        if "--markdown" in sys.argv and rows:
            print(f"\n### {setup}")
            markdown(setup, rows, "avg_batch_ms", "a) Average time per batch (ms)", "{:,.1f}")
            markdown(setup, rows, "total_s", "b) Total time (seconds)", "{:,.1f}")
            markdown(setup, rows, "avg_memory_per_session_mb", "c) Average memory per session process (MB)", "{:.2f}")
            markdown(setup, rows, "avg_memory_per_batch_mb", "c) Average memory per batch, sum over 2K processes (MB)", "{:,.1f}")
            markdown(setup, rows, "traffic_mb", "d) Network traffic between the two containers (MB)", "{:,.2f}")
