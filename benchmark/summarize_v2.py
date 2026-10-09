#!/usr/bin/env python3
"""Builds the tables of the fixed-set-size benchmark (run_grid.py) from <results>/raw.

The CSV files keep extra decimals, so the tables (and reports built from the CSV) round only once.

Writes <results>/setup1.csv and <results>/setup2.csv (one row per run, including runs that hit
the time limit or were not started for lack of memory) and <results>/tables.md (one table per
metric and set size; rows n x K, columns Setup 1 / Setup 2 x -r / -e / -i).
"""
import argparse
import csv
import glob
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROTOS = ["r", "e", "i"]
RUN_ID = re.compile(r"^setup([12])_([rei])_n(\d+)_s(\d+)_k(\d+)$")
MB = 1024 * 1024
LABEL = {"timeout": "timeout", "timeout_projected": "timeout", "out_of_memory": "out of RAM",
         "not_run_memory": "not run (RAM)", "start_failed": "start failed", "error": "error",
         "interrupted": "interrupted", "not_run_timeout": "timeout (skipped)"}
LIMIT_LABEL = {"memory": "not run (RAM)", "tasks": "not run (threads)", "ports": "not run (ports)",
               "time": "timeout (skipped)"}
REASON_LABEL = {"task_limit": "start failed (threads)", "port_range": "start failed (ports)",
                "session_start": "start failed"}
TIMEOUTS = ("timeout", "timeout_projected")


def read_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def read_json(path):
    with open(path) as f:
        return json.load(f)


def load_run(raw, rid):
    """One CSV row for a run that both launchers finished."""
    setup, proto, n, size, k = RUN_ID.match(rid).groups()
    s, r = (read_json(os.path.join(raw, f"{rid}_{x}.json")) for x in ("sender", "receiver"))
    s_rows, r_rows = (read_csv(os.path.join(raw, f"{rid}_{x}.csv")) for x in ("sender", "receiver"))
    col = lambda rows, name: sum(int(x.get(name) or 0) for x in rows)
    pairs, batches = col(r_rows, "sessions"), len(r_rows)
    mem_kb = col(r_rows, "mem_sum_kb") + col(s_rows, "mem_sum_kb")
    ok = col(r_rows, "correct")
    # per-batch averages use the full batches (K sessions); the smaller last batch is left out
    full = [i for i, x in enumerate(r_rows) if int(x["sessions"]) == int(k)] or list(range(batches))
    full_mem_kb = sum(int(r_rows[i]["mem_sum_kb"]) + int(s_rows[i]["mem_sum_kb"]) for i in full
                      if i < len(s_rows))
    status = s.get("status", "done")
    if r.get("status") in ("error", "interrupted"):
        status = r["status"]
    row = {
        "protocol": proto, "n": int(n), "size": int(size), "K": int(k), "threads": s.get("threads", 1),
        "status": status, "pairs_done": pairs, "batches": batches,
        "avg_batch_ms": round(sum(float(r_rows[i]["batch_ms"]) for i in full) / len(full), 3) if full else "",
        "total_s": round(s["total_s"], 3),
        "avg_memory_per_session_mb": round(mem_kb / (2 * pairs) / 1024, 4) if pairs else "",
        "avg_memory_per_batch_mb": round(full_mem_kb / len(full) / 1024, 3) if full else "",
        "accuracy_pct": round(100 * ok / pairs, 4) if pairs else "",
        "sessions_ok": ok, "sessions_wrong": col(r_rows, "wrong"), "sessions_failed": col(r_rows, "failed"),
        "sender_failed": col(s_rows, "failed"),
        "items_expected": col(r_rows, "items_expected"), "items_found": col(r_rows, "items_found"),
        "items_correct": col(r_rows, "items_correct"),
        "traffic_mb": round((s["tx_bytes"] + r["tx_bytes"]) / MB, 6) if "tx_bytes" in s and "tx_bytes" in r else "",
        "load_avg": round(sum((s["load_start"], s["load_end"], r["load_start"], r["load_end"])) / 4, 1),
        "min_free_mem_mb": min(x for x in (s.get("mem_avail_min_mb"), r.get("mem_avail_min_mb")) if x is not None)
        if s.get("mem_avail_min_mb") is not None or r.get("mem_avail_min_mb") is not None else "",
        "reason": s.get("reason") or r.get("reason") or "",
        "projected_s": s.get("projected_s", ""),
        "detail": s.get("detail") or r.get("detail") or "",
    }
    return int(setup), row


def skipped_run(raw, rid):
    setup, proto, n, size, k = RUN_ID.match(rid).groups()
    m = read_json(os.path.join(raw, f"{rid}_skip.json"))
    row = {"protocol": proto, "n": int(n), "size": int(size), "K": int(k), "status": m["status"],
           "reason": m.get("limit", ""), "pairs_done": 0, "batches": 0, "detail": m.get("detail", "")}
    return int(setup), row


def collect(raw):
    rows = {1: [], 2: []}
    ids = {os.path.basename(p).rsplit("_", 1)[0] for p in glob.glob(os.path.join(raw, "setup*_*.json"))}
    for rid in sorted(ids):
        if not RUN_ID.match(rid):
            continue
        done = all(os.path.exists(os.path.join(raw, f"{rid}_{x}.{e}"))
                   for x in ("sender", "receiver") for e in ("json", "csv"))
        try:
            if done:
                setup, row = load_run(raw, rid)
            elif os.path.exists(os.path.join(raw, f"{rid}_skip.json")):
                setup, row = skipped_run(raw, rid)
            else:
                print(f"incomplete, not reported: {rid}", file=sys.stderr)
                continue
        except (OSError, ValueError, KeyError, IndexError) as e:
            print(f"unreadable, not reported: {rid} ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        rows[setup].append(row)
    for setup in rows:
        rows[setup].sort(key=lambda x: (x["n"], x["size"], x["K"], PROTOS.index(x["protocol"])))
    return rows


def write_csv(path, rows, traffic):
    fields = ["protocol", "n", "size", "K", "threads", "status", "pairs_done", "batches", "avg_batch_ms", "total_s",
              "avg_memory_per_session_mb", "avg_memory_per_batch_mb", "accuracy_pct", "sessions_ok",
              "sessions_wrong", "sessions_failed", "sender_failed", "items_expected", "items_found",
              "items_correct"] + (["traffic_mb"] if traffic else []) + ["load_avg", "min_free_mem_mb", "reason", "projected_s",
                                                                         "detail"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path} ({len(rows)} runs)", file=sys.stderr)


def label(row):
    status = row["status"]
    if status == "not_run_limit":
        return LIMIT_LABEL.get(row.get("reason"), "not run")
    if status == "start_failed":
        return REASON_LABEL.get(row.get("reason"), "start failed")
    return LABEL.get(status, status)


def cell(row, metric):
    """Text of one table cell."""
    if row is None:
        return ""
    status = row["status"]
    value = row.get(metric, "")
    if status not in ("done",) + TIMEOUTS or value == "" or (metric == "traffic_mb" and status != "done"):
        return label(row)
    t = " t" if status in TIMEOUTS else ""
    if metric == "total_s":
        # rounded down, so a run that stopped short never shows 100%
        if status == "timeout_projected" and row.get("projected_s"):
            return f"timeout (~{row['projected_s'] / 60:.0f} min)"
        return f"timeout ({100 * row['pairs_done'] // row['n']}%)" if t else f"{value:,.1f}"
    if metric == "accuracy_pct":
        bad = row["pairs_done"] - row["sessions_ok"]
        if not bad:
            return "100%" + t
        return f"{math.floor(100 * row['sessions_ok'] / row['pairs_done'] * 100) / 100:.2f}% (-{bad:,}){t}"
    if metric == "avg_memory_per_session_mb":
        return f"{value:.2f}{t}"
    if metric == "traffic_mb":
        return f"{value:,.2f}{t}"
    return f"{value:,.0f}{t}"


def markdown(rows, time_limit_min=15):
    out = ["# Result tables (generated by summarize_v2.py)", "",
           "S1 = Setup 1, S2 = Setup 2. Rows: n (item-set pairs) and K (sessions per batch).", "",
           f"- `t`: the run hit the {time_limit_min}-minute limit; the value is over the pairs finished before it.",
           "- `timeout (x%)`: stopped at the time limit after x% of the n pairs.",
           "- `timeout (~y min)`: stopped early, because its first batches showed the full run would take "
           "about y minutes.",
           "- `timeout (skipped)`: not run, because the same setup, protocol and set size already went clearly "
           "over the limit at a smaller n, or at this n with K >= 32 (CPU already full, so K does not help).",
           "- `(-x)` after an accuracy: x sessions failed or returned a wrong intersection.",
           "- Per-batch values (a, and memory per batch) are averaged over full batches of K sessions.",
           "- `not run (RAM)`: not started, because it would need more memory than is free, or a smaller K "
           "already ran out of memory.",
           "- `not run (threads)` / `not run (ports)`: a smaller K already hit the thread/process or port limit.",
           "- `out of RAM`: stopped when free memory fell below the safety margin.",
           "- `start failed (threads)`: not enough free threads/processes to start the batch.",
           "- `interrupted` / `error`: the run was stopped or failed; run_grid.py runs it again.", ""]
    table = {}
    for setup in (1, 2):
        for x in rows[setup]:
            table.setdefault(x["size"], {}).setdefault((x["n"], x["K"]), {})[(setup, x["protocol"])] = x
    metrics = [("avg_batch_ms", "a) Average time per batch (ms)", (1, 2)),
               ("total_s", "b) Total time (seconds)", (1, 2)),
               ("avg_memory_per_session_mb", "c) Average memory per session process (MB)", (1, 2)),
               ("avg_memory_per_batch_mb", "c) Average memory per batch, all 2K processes (MB)", (1, 2)),
               ("traffic_mb", "d) Network traffic between the two containers (MB)", (2,)),
               ("accuracy_pct", "e) Accuracy: sessions that returned exactly the shared items", (1, 2))]
    for size in sorted(table):
        out.append(f"## Set size {size}")
        for metric, title, setups in metrics:
            cols = [(s, p) for s in setups for p in PROTOS]
            out += ["", f"### {title}", "",
                    "| n | K | " + " | ".join(f"S{s} `-{p}`" for s, p in cols) + " |",
                    "|---:|---:|" + "---:|" * len(cols)]
            for (n, k), runs in sorted(table[size].items()):
                out.append(f"| {n:,} | {k:,} | " + " | ".join(cell(runs.get(c), metric) for c in cols) + " |")
        out.append("")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results-v2", help="results folder inside benchmark/")
    ap.add_argument("--raw", help="raw folder (default <results>/raw)")
    ap.add_argument("--out", help="output folder (default <results>)")
    ap.add_argument("--time-limit-min", type=int, default=15)
    a = ap.parse_args()
    out = a.out or os.path.join(HERE, a.results)
    raw = a.raw or os.path.join(out, "raw")
    rows = collect(raw)
    for setup in (1, 2):
        if rows[setup]:
            write_csv(os.path.join(out, f"setup{setup}.csv"), rows[setup], traffic=setup == 2)
    with open(os.path.join(out, "tables.md"), "w") as f:
        f.write(markdown(rows, a.time_limit_min))
    print(f"wrote {os.path.join(out, 'tables.md')}", file=sys.stderr)


if __name__ == "__main__":
    main()
