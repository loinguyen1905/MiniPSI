#!/usr/bin/env python3
"""Runs the benchmark grid on this machine: every n, set size and K, both setups, 3 protocols.

  n     --n      (default 100,1000,10000,20000,30000,40000,50000)
  size  --sizes  (default 256,512,1024): every set of a pair has exactly this many items
  K     16, 32, 64, ... up to --k-max (default 1024) while K < n; K = n (all pairs in one batch)
        is added when n <= --k-max
Order: for each n, size and K: Setup 1 (-r, -e, -i), then Setup 2 (-r, -e, -i), so the three
protocols of a configuration run back to back and see similar machine load.

Each run may take --time-limit seconds (default 900 = 15 minutes); a longer run is stopped
and recorded with status "timeout". Runs that would not fit are not started, and get a
<run_id>_skip.json file saying why:
  not_run_memory  its memory need, estimated from the same setup, protocol and size at a
                  smaller K (at least 128), is more than the free memory
  not_run_limit   a smaller or equal K of the same setup, protocol and size already ran out of
                  memory, threads/processes (task_limit) or ports
  not_run_timeout (with --skip-timeouts, the default) a run of the same setup, protocol and size
                  with n' <= n clearly went over the time limit (its full run would take at least
                  1.2 x the limit, scaled to this n): at K' >= 32 the CPU is already saturated, so
                  every K of this n would take as long; at K' = 16 only K = 16 is skipped
Skip files are checked again every time this script starts, so a run skipped only because
memory was short at that moment is tried again later.

Finished runs are not run again; runs that ended with status "error" or "interrupted" are.
To stop gracefully: `touch STOP` in this folder (the run in progress finishes). To stop now:
Ctrl-C or SIGTERM; the run in progress is then recorded as interrupted and redone next time.
Only one benchmark may run at a time: at start this script stops leftover launchers and
sessions of an earlier, interrupted benchmark (they would hold the ports).
"""
import argparse
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROTOS = "rei"
RERUN_STATUSES = ("error", "interrupted")
RUN_ID = re.compile(r"^setup([12])_([rei])_n(\d+)_s(\d+)_k(\d+)$")
MIN_K_FOR_ESTIMATE = 128        # below this, other programs' memory changes drown the reading


def k_values(n, k_min=16, k_max=None):
    """16, 32, 64, ... while K < n (and K <= k_max), then n itself if n <= k_max."""
    ks, k = [], k_min
    while k < n and (k_max is None or k <= k_max):
        ks.append(k)
        k *= 2
    if k_max is None or n <= k_max:
        ks.append(n)
    return ks


def run_id(setup, proto, n, size, k):
    return f"setup{setup}_{proto}_n{n}_s{size}_k{k}"


def mem_available_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run_files(raw, rid):
    return [os.path.join(raw, f"{rid}_{role}.{ext}") for role in ("sender", "receiver")
            for ext in ("json", "csv", "log")]


def finished_runs(raw, retry=()):
    """{run_id: (sender json, receiver json)} for runs that are finished (not to be run again)."""
    runs = {}
    for path in glob.glob(os.path.join(raw, "*_sender.json")):
        rid = os.path.basename(path)[: -len("_sender.json")]
        if not RUN_ID.match(rid):
            continue
        s, r = read_json(path), read_json(os.path.join(raw, f"{rid}_receiver.json"))
        if s and r and s.get("status") not in RERUN_STATUSES + tuple(retry) \
                and r.get("status") not in RERUN_STATUSES:
            runs[rid] = (s, r)
    return runs


def resource_failure(j):
    """What a run ran out of, or None: 'memory', 'tasks' or 'ports'."""
    if j.get("status") == "out_of_memory":
        return "memory"
    if j.get("status") == "start_failed":
        return {"task_limit": "tasks", "port_range": "ports"}.get(j.get("reason"))
    return None


def estimated_total(j, n):
    """Seconds a run that hit the time limit would have needed for all n pairs, or None."""
    if j.get("status") == "timeout_projected" and j.get("projected_s"):
        return j["projected_s"]
    if j.get("status") == "timeout" and j.get("sessions_done"):
        return j["total_s"] * n / j["sessions_done"]
    return None


def decide(raw, setup, proto, n, size, k, free_mb, min_free_mb, finished=None, time_limit=None,
           margin=1.2):
    """What to do with one run: ('done', None), ('run', None), or ('skip', content of the new
    <run_id>_skip.json). finished: result of finished_runs(raw), to avoid reading it again.
    time_limit: if given, skip runs that are sure to go over it (see not_run_timeout)."""
    rid = run_id(setup, proto, n, size, k)
    finished = finished_runs(raw) if finished is None else finished
    if rid in finished:
        return "done", None

    same = []                                     # finished runs of this setup, protocol and size
    too_long = []
    for other, (s, r) in finished.items():
        m = RUN_ID.match(other)
        if m.group(1) == str(setup) and m.group(2) == proto and int(m.group(4)) == size:
            same.append((int(m.group(5)), other, s, r))
            n2, k2 = int(m.group(3)), int(m.group(5))
            est = estimated_total(s, n2)
            if time_limit and est and n2 <= n and (k2 == k or k2 >= 32) \
                    and est * n / n2 >= margin * time_limit:
                too_long.append((est * n / n2, other, n2, k2, est))
    if too_long:
        scaled, other, n2, k2, est = min(too_long)
        return "skip", {"status": "not_run_timeout", "limit": "time", "based_on": other,
                        "estimated_s": round(scaled),
                        "detail": f"n = {n2:,}, K = {k2} needed about {est / 60:.0f} min, so this run would "
                                  f"need about {scaled / 60:.0f} min, over the {time_limit / 60:.0f}-minute limit"}

    limits = [(k2, other, resource_failure(s) or resource_failure(r)) for k2, other, s, r in same if k2 <= k]
    limits = [x for x in limits if x[2]]
    if limits:
        k2, other, kind = min(limits)
        what = {"memory": "free memory", "tasks": "threads/processes", "ports": "ports"}[kind]
        return "skip", {"status": "not_run_limit", "limit": kind, "based_on": other,
                        "detail": f"K = {k2} already ran out of {what}"}

    measured = [(k2, other, s, r) for k2, other, s, r in same
                if MIN_K_FOR_ESTIMATE <= k2 < k and s.get("batches_done", 0) > 0]
    if measured:
        k2, other, s, r = max(measured, key=lambda x: x[0])
        used = max((j["mem_avail_start_mb"] - j["mem_avail_min_mb"] for j in (s, r)
                    if j.get("mem_avail_start_mb") is not None and j.get("mem_avail_min_mb") is not None),
                   default=0)
        predicted = used * k / k2
        if predicted > free_mb - min_free_mb:
            return "skip", {"status": "not_run_memory", "limit": "memory", "based_on": other,
                            "predicted_mb": round(predicted), "free_mb": free_mb, "min_free_mb": min_free_mb,
                            "detail": f"needs about {predicted / 1024:.1f} GB (K = {k2} used "
                                      f"{used / 1024:.1f} GB), {free_mb / 1024:.1f} GB free"}
    return "run", None


def containers_running():
    for c in ("party-a", "party-b"):
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", c],
                             capture_output=True, text=True)
        if out.stdout.strip() != "true":
            return False
    return True


def start_containers(env, log, say, attempts=10, wait=60):
    """Starts the Setup 2 containers; retries, since Docker may not be ready right after boot."""
    for attempt in range(1, attempts + 1):
        say("starting the Setup 2 containers" + (f" (attempt {attempt})" if attempt > 1 else ""))
        done = subprocess.run([os.path.join(HERE, "run_setup2.sh"), "up"], env=env,
                              stdout=log, stderr=subprocess.STDOUT)
        if done.returncode == 0 and containers_running():
            return
        if attempt < attempts:
            time.sleep(wait)
    raise RuntimeError(f"the Setup 2 containers did not start after {attempts} attempts")


def benchmark_pids():
    """PIDs of launchers (python3 .../run_batches.py) and psi_session processes on this host."""
    pids = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if argv and os.path.basename(argv[0]) == b"psi_session":
            pids.append(int(pid))
        elif len(argv) > 1 and os.path.basename(argv[0]).startswith(b"python") \
                and argv[1].endswith(b"run_batches.py"):
            pids.append(int(pid))
    return pids


def stop_leftovers(say):
    """Stops launchers and sessions of an earlier benchmark, on this host and in the containers."""
    pids = benchmark_pids()
    if containers_running():
        for c in ("party-a", "party-b"):
            subprocess.run(["docker", "exec", c, "pkill", "-TERM", "-f", "^python3 /bench/run_batches.py"],
                           capture_output=True)
    if pids:
        say(f"stopping {len(pids)} leftover benchmark processes")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    end = time.monotonic() + 30
    while benchmark_pids() and time.monotonic() < end:
        time.sleep(0.5)
    for pid in benchmark_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if containers_running():
        for c in ("party-a", "party-b"):
            subprocess.run(["docker", "exec", c, "pkill", "-KILL", "-x", "psi_session"], capture_output=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", default="100,1000,10000,20000,30000,40000,50000")
    ap.add_argument("--sizes", default="256,512,1024")
    ap.add_argument("--setups", default="1,2")
    ap.add_argument("--protos", default="r,e,i")
    ap.add_argument("--k-min", type=int, default=16)
    ap.add_argument("--threads", type=int, default=1,
                    help="worker threads per session and party (like -t of frontend/main.cpp); "
                         "a results folder holds runs of one value only")
    ap.add_argument("--k-max", type=int, default=1024, help="largest K; 0 = up to n")
    ap.add_argument("--time-limit", type=float, default=900)
    ap.add_argument("--stop-early", type=float, default=0,
                    help="stop a run once its finished batches project more than this many times "
                         "the time limit (e.g. 1.25); 0 = always run up to the time limit")
    ap.add_argument("--stop-early-after", type=float, default=60,
                    help="seconds a run must last before --stop-early may stop it")
    ap.add_argument("--min-free-mb", type=int, default=3072)
    ap.add_argument("--no-skip-timeouts", action="store_true",
                    help="run every configuration even when a smaller one already went over the limit")
    ap.add_argument("--results", default="results-v2", help="results folder inside benchmark/")
    ap.add_argument("--retry", default="",
                    help="comma-separated statuses to run again, e.g. out_of_memory,start_failed")
    ap.add_argument("--dry-run", action="store_true", help="only print what would run")
    a = ap.parse_args()

    ns = [int(x) for x in a.n.split(",")]
    sizes = [int(x) for x in a.sizes.split(",")]
    setups = [int(x) for x in a.setups.split(",")]
    protos = [p for p in a.protos.split(",") if p in PROTOS]
    retry = tuple(x for x in a.retry.split(",") if x)
    raw = os.path.join(HERE, a.results, "raw")
    os.makedirs(raw, exist_ok=True)
    log = None if a.dry_run else open(os.path.join(raw, "progress.log"), "a")

    def say(msg):
        line = f"{time.strftime('%d/%m %H:%M:%S')} {msg}"
        print(line, flush=True)
        if log:
            log.write(line + "\n")
            log.flush()

    env = dict(os.environ, RESULTS=a.results, TIME_LIMIT=str(a.time_limit), MIN_FREE_MB=str(a.min_free_mb),
               THREADS=str(a.threads))
    other = sorted({j.get("threads", 1) for s, r in finished_runs(raw).values() for j in (s,)} - {a.threads})
    if other:
        sys.exit(f"{a.results} holds runs with threads = {other}; use another --results folder for threads = {a.threads}")
    for var in ("OFFSET", "LABEL_N", "CHUNK", "STOP_EARLY", "STOP_EARLY_AFTER"):
        env.pop(var, None)
    if a.stop_early > 0:
        env["STOP_EARLY"] = str(a.stop_early)
        env["STOP_EARLY_AFTER"] = str(a.stop_early_after)

    current = {"proc": None}

    def on_signal(signum, frame):
        say(f"got signal {signum}: stopping the run in progress (it will be run again next time)")
        p = current["proc"]
        if p is not None and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
        stop_leftovers(say)
        raise SystemExit(130)

    if not a.dry_run:
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGHUP, on_signal)
        stop_file = os.path.join(HERE, "STOP")
        if os.path.exists(stop_file):
            os.remove(stop_file)
        stop_leftovers(say)

    say(f"--- run_grid: n={ns} sizes={sizes} K {a.k_min}..{a.k_max or 'n'} threads={a.threads} setups={setups} protos={protos} "
        f"time limit {a.time_limit:.0f} s, min free {a.min_free_mb} MB{' (dry run)' if a.dry_run else ''}")
    for n in ns:
        for size in sizes:
            for k in k_values(n, a.k_min, a.k_max or None):
                for setup in setups:
                    for p in protos:
                        if os.path.exists(os.path.join(HERE, "STOP")):
                            say("STOP file found, stopping; run ./run_grid.py again to continue")
                            return 0
                        rid = run_id(setup, p, n, size, k)
                        finished = finished_runs(raw, retry)
                        action, marker = decide(raw, setup, p, n, size, k, mem_available_mb(),
                                                a.min_free_mb, finished,
                                                None if a.no_skip_timeouts else a.time_limit)
                        skip_file = os.path.join(raw, f"{rid}_skip.json")
                        if action == "done":
                            continue
                        if action == "skip":
                            if read_json(skip_file) != marker:
                                say(f"skip {rid}: {marker['status']}, {marker['detail']}")
                            if not a.dry_run:
                                with open(skip_file, "w") as f:
                                    json.dump(marker, f, indent=1)
                            continue
                        if a.dry_run:
                            say(f"would run {rid}")
                            continue
                        for path in run_files(raw, rid) + [skip_file]:      # earlier attempt, if any
                            if os.path.exists(path):
                                os.remove(path)
                        if setup == 2 and not containers_running():
                            start_containers(env, log, say)
                        t0 = time.monotonic()
                        current["proc"] = subprocess.Popen(
                            [os.path.join(HERE, f"run_setup{setup}.sh"), f"{n}:{k}"],
                            env=dict(env, SIZE=str(size), PROTOS=p), stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
                        current["proc"].wait()
                        current["proc"] = None
                        s = read_json(os.path.join(raw, f"{rid}_sender.json")) or {}
                        extra = f" ({s['reason']})" if s.get("reason") else ""
                        say(f"{rid}: {s.get('status', 'FAILED')}{extra}, {s.get('sessions_done', 0):,}/{n:,} pairs, "
                            f"{time.monotonic() - t0:.0f} s")
            if not a.dry_run:
                subprocess.run([sys.executable, os.path.join(HERE, "summarize_v2.py"), "--results", a.results],
                               stdout=log, stderr=subprocess.STDOUT)
                say(f"=== n={n:,} size={size}: tables updated in {a.results}/tables.md")
    say("ALL DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
