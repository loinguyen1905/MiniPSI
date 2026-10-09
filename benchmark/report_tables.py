#!/usr/bin/env python3
"""Prints the result tables of a run_grid.py report (markdown) from <results>/setup1.csv and setup2.csv.

  python3 report_tables.py --results results-v2 > tables_for_report.md

Tables a) time per batch, b) total time, c) memory, d) traffic and e) accuracy, with rows n x K per
set size and columns Setup 1 / Setup 2 x -r / -e / -i. Runs that stopped at the time limit or were
not run are shown with the same labels as tables.md (see summarize_v2.py). Missing runs are blank.
"""
import argparse
import csv
import os
import statistics
import sys

import run_grid
import summarize_v2

HERE = os.path.dirname(os.path.abspath(__file__))
SP = [(s, p) for s in (1, 2) for p in "rei"]
HEAD = ("| n | K | S1 `-r` | S1 `-e` | S1 `-i` | S2 `-r` | S2 `-e` | S2 `-i` |\n"
        "|---:|---:|---:|---:|---:|---:|---:|---:|")


def load(folder):
    rows = {}
    for s in (1, 2):
        path = os.path.join(folder, f"setup{s}.csv")
        if not os.path.exists(path):
            continue
        for r in csv.DictReader(open(path)):
            for c in ("pairs_done", "n", "size", "K", "sessions_ok"):
                if r.get(c, "") != "":
                    r[c] = int(r[c])
            for c in ("avg_batch_ms", "total_s", "avg_memory_per_session_mb", "avg_memory_per_batch_mb",
                      "accuracy_pct", "traffic_mb", "projected_s"):
                if r.get(c, "") != "":
                    r[c] = float(r[c])
            rows[(s, r["protocol"], r["n"], r["size"], r["K"])] = r
    return rows


def done(r):
    return r is not None and r["status"] == "done"


def complete_blocks(R, k_max=1024, k_min=16):
    """Keeps only the (n, set size) blocks that have a row for every setup, protocol and K."""
    keep, dropped = {}, []
    for n, z in sorted({(k[2], k[3]) for k in R}):
        want = [(s, p, n, z, k) for k in run_grid.k_values(n, k_min, k_max) for s, p in SP]
        if all(w in R for w in want):
            keep.update({w: R[w] for w in want})
        else:
            dropped.append((n, z, sum(w in R for w in want), len(want)))
    return keep, dropped


def stats(R, out=sys.stdout):
    """The figures quoted in the report's conclusion (finished runs only)."""
    say = lambda line="": print(line, file=out)
    T = lambda s, p, n, z, k: R[(s, p, n, z, k)]["total_s"]
    cfg = sorted({(k[2], k[3], k[4]) for k in R})
    ok_cfg = [c for c in cfg if all(done(R.get((s, p) + c)) for s, p in SP)]
    say(f"runs {len(R)}, configurations {len(cfg)} ({len(ok_cfg)} with all 6 runs finished)")
    tot = lambda col: sum(int(r[col]) for r in R.values() if r.get(col, "") != "")
    say(f"sessions correct {tot('sessions_ok'):,} / {tot('pairs_done'):,}; wrong {tot('sessions_wrong')}, "
        f"failed {tot('sessions_failed')}; items correct {tot('items_correct'):,} / expected {tot('items_expected'):,} "
        f"/ found {tot('items_found'):,}; statuses {sorted({r['status'] for r in R.values()})}")
    for s in (1, 2):
        fast, slow = {}, {}
        for c in ok_cfg:
            v = {p: T(s, p, *c) for p in "rei"}
            b, w = min(v, key=v.get), max(v, key=v.get)
            fast[b] = fast.get(b, 0) + 1
            slow[w] = slow.get(w, 0) + 1
        say(f"Setup {s}: fastest {fast}, slowest {slow} (of {len(ok_cfg)})")
    blocks = sorted({(c[0], c[1]) for c in ok_cfg})
    for n, z in blocks:
        ks = [c[2] for c in ok_cfg if c[:2] == (n, z) and c[2] >= 32]
        if not ks:
            continue
        line = []
        for s in (1, 2):
            med = {p: statistics.median(T(s, p, n, z, k) for k in ks) for p in "rei"}
            line.append(f"S{s} median K>=32 {[round(med[p], 1) for p in 'rei']} s, -i vs -r {100 * (med['i'] / med['r'] - 1):+.1f}%, "
                        f"sessions/s {[round(n / med[p]) for p in 'rei']}")
            k16 = [c for c in ok_cfg if c == (n, z, 16)]
            if k16:
                line[-1] += ", K=16 vs median " + str([f"{100 * (T(s, p, n, z, 16) / med[p] - 1):+.0f}%" for p in "rei"])
        say(f"n={n:,} size {z}: " + " | ".join(line))
    sizes = sorted({c[1] for c in ok_cfg})
    for z in sizes:
        for s in (1, 2):
            mem = {p: statistics.mean(r["avg_memory_per_session_mb"] for key, r in R.items()
                                      if key[:2] == (s, p) and key[3] == z and done(r)) for p in "rei"}
            say(f"size {z} Setup {s}: memory per process {[round(mem[p], 2) for p in 'rei']} MB, -i vs -r "
                f"{100 * (mem['i'] / mem['r'] - 1):+.1f}%, -i vs -e {100 * (mem['i'] / mem['e'] - 1):+.1f}%")
        for n in sorted({c[0] for c in ok_cfg if c[1] == z}):
            k0 = min(c[2] for c in ok_cfg if c[:2] == (n, z))
            t = {p: R[(2, p, n, z, k0)]["traffic_mb"] for p in "rei"}
            say(f"size {z} n={n:,} traffic K={k0}: -i less than -r {100 * (1 - t['i'] / t['r']):.1f}%, than -e "
                f"{100 * (1 - t['i'] / t['e']):.1f}%; per session {[round(t[p] * 1024 / n, 1) for p in 'rei']} KB")
    if len(sizes) > 1:
        for n in sorted({c[0] for c in ok_cfg if all((c[0], z, c[2]) in ok_cfg for z in sizes)}):
            ks = [k for k in {c[2] for c in ok_cfg if c[0] == n} if k >= 32 and all((n, z, k) in ok_cfg for z in sizes)]
            if ks:
                ratio = [statistics.median(T(s, p, n, sizes[-1], k) / T(s, p, n, sizes[0], k) for k in ks)
                         for s in (1, 2) for p in "rei"]
                say(f"n={n:,}: time {sizes[-1]} / {sizes[0]} items, median per protocol and setup {min(ratio):.2f}-{max(ratio):.2f}")
    longest = max(R.values(), key=lambda r: r["total_s"] if done(r) else 0)
    say(f"longest finished run {longest['total_s'] / 60:.1f} min (n={longest['n']:,}, size {longest['size']}, K={longest['K']})")


def tables(R, out=sys.stdout):
    say = lambda line="": print(line, file=out)
    sizes = sorted({k[3] for k in R})

    def grid(metric, title):
        say(f"\n### {title}")
        for z in sizes:
            say(f"\n**Set size {z}**\n\n{HEAD}")
            for n, k in sorted({(key[2], key[4]) for key in R if key[3] == z}):
                cells = [summarize_v2.cell(R.get((s, p, n, z, k)), metric) for s, p in SP]
                say(f"| {n:,} | {k:,} | " + " | ".join(cells) + " |")

    grid("avg_batch_ms", "a) Average time per batch (ms)")
    grid("total_s", "b) Total time (seconds)")

    say("\n### c) Average memory\n")
    say("Per session process (MB), average over the finished runs of a set size; it hardly changes with n and K:\n")
    say("| | `-r` | `-e` | `-i` |\n|---|---:|---:|---:|")
    for z in sizes:
        for s in (1, 2):
            vals = []
            for p in "rei":
                v = [r["avg_memory_per_session_mb"] for key, r in R.items()
                     if key[:2] == (s, p) and key[3] == z and r.get("avg_memory_per_session_mb", "") != ""]
                vals.append(f"{sum(v) / len(v):.2f}" if v else "")
            say(f"| Set size {z}, Setup {s} | " + " | ".join(vals) + " |")
    # per batch: the largest n with all K finished in Setup 1 for every size
    full_n = [n for n in sorted({k[2] for k in R}, reverse=True)
              if all(done(R.get((1, p, n, z, k))) for z in sizes for p in "rei"
                     for k in {key[4] for key in R if key[2] == n and key[3] == z})
              and all(any(key[2] == n and key[3] == z for key in R) for z in sizes)]
    if full_n:
        n = full_n[0]
        ks = sorted({k[4] for k in R if k[2] == n})
        say(f"\nPer batch (sum over the 2K processes of a batch, MB), n = {n:,}, Setup 1; Setup 2 is within 2%:\n")
        say("| K | " + " | ".join(f"{z} `-{p}`" for z in sizes for p in "rei") + " |\n|---:|" + "---:|" * (3 * len(sizes)))
        for k in ks:
            say(f"| {k:,} | " + " | ".join(f"{R[(1, p, n, z, k)]['avg_memory_per_batch_mb']:,.0f}"
                                         for z in sizes for p in "rei") + " |")

    say("\n### d) Network traffic between the two containers (Setup 2)\n")
    say("Total of the run at the smallest K (16), and per session:\n")
    say("| Set size | n | `-r` | `-e` | `-i` | per session `-r` | per session `-e` | per session `-i` |\n"
        "|---:|---:|---:|---:|---:|---:|---:|---:|")
    for z in sizes:
        for n in sorted({k[2] for k in R if k[3] == z}):
            ks = sorted(key[4] for key in R if key[0] == 2 and key[2] == n and key[3] == z)
            runs = [R.get((2, p, n, z, ks[0])) for p in "rei"] if ks else []
            if not runs or not all(done(r) for r in runs):
                continue
            say(f"| {z} | {n:,} | " + " | ".join(f"{r['traffic_mb']:,.2f} MB" for r in runs) + " | "
                + " | ".join(f"{r['traffic_mb'] * 1024 / n:.1f} KB" for r in runs) + " |")

    grid("accuracy_pct", "e) Accuracy: sessions that returned exactly the shared items")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default="results-v2", help="results folder inside benchmark/")
    ap.add_argument("--k-max", type=int, default=1024, help="largest K of the grid (to find complete blocks)")
    ap.add_argument("--all", action="store_true", help="also show (n, set size) blocks that are not finished")
    ap.add_argument("--stats", action="store_true", help="print the figures for the conclusion instead of tables")
    a = ap.parse_args()
    R = load(os.path.join(HERE, a.results))
    if not R:
        sys.exit(f"no setup1.csv / setup2.csv in {a.results}; run summarize_v2.py first")
    if not a.all:
        R, dropped = complete_blocks(R, a.k_max)
        for n, z, have, want in dropped:
            print(f"left out, not finished: n={n:,} set size {z} ({have}/{want} runs)", file=sys.stderr)
    stats(R) if a.stats else tables(R)


if __name__ == "__main__":
    main()
