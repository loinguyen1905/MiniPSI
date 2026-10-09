#!/usr/bin/env python3
"""Runs n PSI sessions, K at a time, for one party (sender or receiver).

Every session is a separate psi_session process (the library leaks memory and shares a
global timer, so sessions cannot safely run inside one long-lived process).

The two launchers talk over a small control connection:
  sender:   start K psi_session senders, wait until all are listening, send "GO <b> <seconds left>"
  receiver: start K psi_session receivers
  both:     once all own K sessions have exited, send "FIN <b> <state>" to the other side;
            after the other side's FIN, own sessions still running get a grace period and are
            then killed (they count as failed sessions)
  sender:   next "GO", or "STOP <status>" when the run is over

A run stops early, and its JSON "status" says why, when
  timeout            it runs longer than --time-limit seconds
  timeout_projected  (only with --stop-early) the finished batches show it would clearly
                     take longer than --time-limit
  out_of_memory      free memory (MemAvailable) falls below --min-free-mb
  start_failed       sessions cannot be started; "reason" says why: task_limit (not enough free
                     processes/threads, checked before every batch and reported by psi_session
                     with exit code 75), port_range (K larger than the free port range) or
                     session_start (a session died before listening)
  interrupted        the launcher was stopped (Ctrl-C, SIGTERM); run_grid.py runs it again
  error              anything unexpected, e.g. the other launcher disappeared
A batch cut short by one of these is not counted; the batches before it are.

Measured per run:
  batch time  = receiver side, from starting the K receivers until all K have exited
  total time  = sender side, from the first batch until the end of the last one
  memory      = peak resident memory (VmHWM) that every psi_session process reports for itself
  accuracy    = receiver side: sessions that returned exactly the shared items (ok), returned
                something else (wrong), or crashed/were killed (failed)
  traffic     = bytes sent/received on --iface during the run (Setup 2)
"""
import argparse
import csv
import json
import os
import resource
import selectors
import signal
import socket
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
POLL = 0.1              # longest wait for an event (output, exit, control message)
REAP_POLL = 0.002       # wait between checks for processes whose output is closed but not reaped yet
MEM_CHECK_EVERY = 32    # started processes between memory checks while a batch starts
CONNECT_TIMEOUT = 120   # seconds the two launchers wait for each other
TASKS_EXTRA = 3         # psi_session tasks besides its worker threads: main + IO thread (+1 margin)
# batch states (sent in FIN messages) -> run status and reason
STATUS = {"cut": "timeout", "oom": "out_of_memory", "tasks": "start_failed", "not_ready": "start_failed"}
REASON = {"tasks": "task_limit", "not_ready": "session_start"}


def _interrupt(signum, frame):
    # A shutdown often sends SIGTERM twice (systemd and run_grid.py); a second one must not
    # interrupt the cleanup that kills the sessions and writes the JSON.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    raise KeyboardInterrupt(f"signal {signum}")


def load_average():
    with open("/proc/loadavg") as f:
        return float(f.read().split()[0])


def mem_available_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return None


def iface_bytes(iface):
    if not iface:
        return None
    base = f"/sys/class/net/{iface}/statistics/"
    with open(base + "tx_bytes") as tx, open(base + "rx_bytes") as rx:
        return int(tx.read()), int(rx.read())


def user_tasks():
    """Tasks (threads) of this user that this process can see."""
    uid, count = os.getuid(), 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/status") as f:
                fields = dict(line.split(":", 1) for line in f if ":" in line)
            if int(fields["Uid"].split()[0]) == uid:
                count += int(fields["Threads"])
        except (OSError, KeyError, ValueError):
            pass
    return count


def task_headroom():
    """How many more tasks may be created: the tightest of pids.max - pids.current over this
    process's cgroup and its parents, and RLIMIT_NPROC minus this user's tasks. None = no limit."""
    room = []
    try:
        with open("/proc/self/cgroup") as f:
            rel = next(line.split("::", 1)[1].strip() for line in f if line.startswith("0::"))
        d = os.path.normpath("/sys/fs/cgroup" + rel)
        while d.startswith("/sys/fs/cgroup"):
            try:
                with open(os.path.join(d, "pids.max")) as f:
                    limit = f.read().strip()
                with open(os.path.join(d, "pids.current")) as f:
                    current = int(f.read())
                if limit != "max":
                    room.append(int(limit) - current)
            except (OSError, ValueError):
                pass
            d = os.path.dirname(d)
    except (OSError, StopIteration):
        pass
    soft = resource.getrlimit(resource.RLIMIT_NPROC)[0]
    if soft != resource.RLIM_INFINITY:
        room.append(soft - user_tasks())
    return min(room) if room else None


def ephemeral_port_low():
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range") as f:
            return int(f.read().split()[0])
    except OSError:
        return 32768


class Control:
    """Line-based control connection that can be read with a timeout."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def send(self, line):
        self.sock.settimeout(None)
        self.sock.sendall((line + "\n").encode())

    def recv(self, timeout):
        """Next line, or None if none arrived within timeout seconds (timeout None: wait)."""
        end = None if timeout is None else time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = None if end is None else max(0.001, end - time.monotonic())
            self.sock.settimeout(left)
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not data:
                raise ConnectionError("the other launcher closed the control connection")
            self.buf += data
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode()


class Watch:
    """Deadline and free-memory checks, and the lowest free memory seen."""

    def __init__(self, min_free_mb):
        self.min_free_mb = min_free_mb
        self.deadline = None
        self.mem_start = mem_available_mb()
        self.mem_min = self.mem_start

    def check(self):
        """'cut' after the deadline, 'oom' when memory is low, otherwise None."""
        mem = mem_available_mb()
        if mem is not None:
            self.mem_min = mem if self.mem_min is None else min(self.mem_min, mem)
            if self.min_free_mb and mem < self.min_free_mb:
                return "oom"
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return "cut"
        return None


class Batch:
    """The K psi_session processes of one batch on this side, and their output."""

    def __init__(self, cmds, log):
        self.cmds = cmds
        self.log = log
        self.procs = []
        self.out = []
        self.eof = set()
        self.reaped = set()
        self.sel = selectors.DefaultSelector()
        self.why = ""

    def start(self, watch, need_tasks, reserve):
        """Starts the sessions; returns None, or why not ('tasks', 'not_ready', 'cut', 'oom')."""
        room = task_headroom()
        if room is not None and room - reserve < need_tasks:
            self.why = f"task limit: need {need_tasks} tasks, {room} free, {reserve} kept for other programs"
            return "tasks"
        for i, c in enumerate(self.cmds):
            if i % MEM_CHECK_EVERY == 0:
                state = watch.check()
                if state:
                    return state
            try:
                p = subprocess.Popen(c, stdout=subprocess.PIPE, stderr=self.log)
            except OSError as e:
                self.log.write(f"cannot start {' '.join(c)}: {e}\n")
                self.log.flush()
                self.why = f"cannot start a session: {e}"
                return "tasks" if e.errno in (11, 12) else "not_ready"      # EAGAIN, ENOMEM
            self.procs.append(p)
            self.out.append(b"")
            self.sel.register(p.stdout, selectors.EVENT_READ, i)
        return None

    def pump(self, timeout):
        """Collects the output that is available, waiting at most timeout seconds.

        Returns as soon as there is output or a process closes its output (exits). When every
        output is closed already, it only waits REAP_POLL, for the exit to be seen."""
        if len(self.eof) == len(self.procs):
            time.sleep(min(timeout, REAP_POLL))
            return
        for key, _ in self.sel.select(timeout):
            data = os.read(key.fd, 65536)
            if data:
                self.out[key.data] += data
            else:
                self.sel.unregister(key.fileobj)
                key.fileobj.close()
                self.eof.add(key.data)

    def wait_ready(self, watch):
        """Sender side: returns None once every session printed READY, else why not."""
        while True:
            state = watch.check()
            if state:
                return state
            self.pump(POLL)
            ready = 0
            for i in range(len(self.procs)):
                if self.out[i].startswith(b"READY\n"):
                    ready += 1
                elif i in self.eof or len(self.out[i]) >= 6:
                    try:
                        code = self.procs[i].wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        code = None
                    self.why = f"session {i} did not become ready (exit code {code})"
                    self.log.write(f"{self.why}: {' '.join(self.cmds[i])}\n")
                    self.log.flush()
                    return "tasks" if code == 75 else "not_ready"
            if ready == len(self.procs):
                return None

    def alive(self):
        """Indices of the sessions still running. A process can only have exited after its output
        closed, so only those are checked (this keeps the check cheap for thousands of sessions)."""
        for i in self.eof - self.reaped:
            if self.procs[i].poll() is not None:
                self.reaped.add(i)
        return [i for i in range(len(self.procs)) if i not in self.reaped]

    def kill(self, which=None):
        for i in range(len(self.procs)) if which is None else which:
            if self.procs[i].poll() is None:
                self.procs[i].kill()
        for p in self.procs:
            p.wait()

    def results(self):
        """(exit code, RESULT fields) per session, in command order. Call once all have exited."""
        end = time.monotonic() + 5
        while len(self.eof) < len(self.procs) and time.monotonic() < end:
            self.pump(POLL)
        self.sel.close()
        res = []
        for p, data in zip(self.procs, self.out):
            if p.stdout and not p.stdout.closed:
                p.stdout.close()
            text = data.decode(errors="replace")
            line = next((l for l in text.splitlines() if l.startswith("RESULT")), "")
            res.append((p.returncode, line.split()[1:]))
        return res


def combine(own, peer):
    """State of a batch from both sides: 'ok' or the reason it was cut short."""
    for s in (peer, own):
        if s not in ("ok", "aborted"):
            return s
    return "ok"


def run_batch(batch, ctrl, b, watch, own, grace_min):
    """Waits until both sides are done with batch b; returns (own state, peer state, end time).

    own is None while own sessions run, or already a state if they never started properly.
    """
    peer = None
    grace_until = None
    t_end = time.monotonic() if own else None
    fin_sent = False
    t0 = time.monotonic()
    while True:
        if own is None:
            batch.pump(POLL)                    # wakes up on output or exit of a session
        now = time.monotonic()
        if own is None:
            alive = batch.alive()
            state = watch.check()
            if any(p.returncode == 75 for p in batch.procs):
                state = "tasks"                 # a session ran out of threads/processes/memory
                batch.why = "a session ran out of system resources (exit code 75)"
            if state:
                batch.kill()
                own = state
            elif not alive:
                own = "ok"
            elif grace_until is not None and now >= grace_until:
                batch.kill(alive)              # stragglers count as failed sessions
                own = "ok"
            if own is not None:
                t_end = time.monotonic()
        if own is not None and not fin_sent:
            ctrl.send(f"FIN {b} {own}")
            fin_sent = True
        if peer is None:
            # own sessions done: wait for the other side's FIN, waking up as soon as it arrives
            line = ctrl.recv(0 if own is None else POLL)
            if line is not None:
                kind, pb, pstate = line.split()
                if kind != "FIN" or int(pb) != b:
                    raise RuntimeError(f"unexpected control message: {line}")
                peer = pstate
                if own is None and peer != "ok":
                    batch.kill()                # the other side gave up on this batch
                    own = "aborted"
                    t_end = time.monotonic()
                elif own is None:
                    grace_until = now + max(grace_min, now - t0)
        if fin_sent and peer is not None:
            return own, peer, t_end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["sender", "receiver"], required=True)
    ap.add_argument("--proto", choices=["r", "e", "i"], required=True)
    ap.add_argument("--n", type=int, required=True, help="number of item-set pairs")
    ap.add_argument("--k", type=int, required=True, help="sessions per batch")
    ap.add_argument("--size", type=int, default=0,
                    help="items per set; 0 = random in [2, 1024]")
    ap.add_argument("--threads", type=int, default=1,
                    help="worker threads per session and party, like -t of frontend/main.cpp")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--offset", type=int, default=0,
                    help="index of the first pair; lets a large n be run as several chunks")
    ap.add_argument("--host", required=True,
                    help="sender: address to listen on; receiver: address of the sender")
    ap.add_argument("--base-port", type=int, default=20000)
    ap.add_argument("--ctrl-port", type=int, default=19999)
    ap.add_argument("--iface", default="", help="network interface to count traffic on (Setup 2)")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", default=os.path.join(HERE, "results", "raw"))
    ap.add_argument("--bin", default=os.path.join(HERE, "bin", "psi_session"))
    ap.add_argument("--time-limit", type=float, default=0,
                    help="sender: seconds the whole run may take; 0 = no limit")
    ap.add_argument("--stop-early", type=float, default=0,
                    help="sender: stop once the finished batches project a total above "
                         "this many times --time-limit; 0 = off")
    ap.add_argument("--stop-early-after", type=float, default=60,
                    help="seconds the run must have lasted before --stop-early may stop it")
    ap.add_argument("--min-free-mb", type=int, default=0,
                    help="stop when MemAvailable falls below this; 0 = never")
    ap.add_argument("--task-reserve", type=int, default=2000,
                    help="free tasks (threads) left for other programs when starting a batch")
    ap.add_argument("--grace-min", type=float, default=15.0,
                    help="seconds own sessions may still run after the other side finished "
                         "(at least; it is also at least the time the batch took so far)")
    a = ap.parse_args()

    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGHUP, _interrupt)
    # One pipe per session is kept open; Docker containers default to a soft limit of 1024 files.
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))

    os.makedirs(a.out, exist_ok=True)
    prefix = os.path.join(a.out, f"{a.run_id}_{a.role}")
    log = open(prefix + ".log", "w")
    role_flag = "s" if a.role == "sender" else "r"
    size_arg = [str(a.size)] if a.size else []
    if a.threads != 1:
        size_arg = [str(a.size), str(a.threads)]
    tasks_per_session = a.threads + TASKS_EXTRA

    def cmd(slot, index):
        return [a.bin, a.proto, role_flag, a.host, str(a.base_port + slot), str(a.offset + index),
                str(a.seed)] + size_arg

    rows = []
    batches = (a.n + a.k - 1) // a.k
    status, detail, reason = None, "", ""       # status is None while the run is going on
    projected = None
    same_host = a.host in ("127.0.0.1", "localhost")   # Setup 1: both parties' sessions on this host
    watch = Watch(a.min_free_mb)
    load0 = load_average()
    net0 = iface_bytes(a.iface)
    t_start = time.monotonic()
    batch = None
    conn = None
    try:
        # ---- control connection ----
        if a.role == "sender":
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((a.host, a.ctrl_port))
            srv.listen(1)
            srv.settimeout(CONNECT_TIMEOUT)
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                raise ConnectionError(f"the receiver launcher did not connect within {CONNECT_TIMEOUT} s")
            finally:
                srv.close()
        else:
            end = time.monotonic() + CONNECT_TIMEOUT
            while True:
                try:
                    conn = socket.create_connection((a.host, a.ctrl_port), timeout=5)
                    break
                except OSError:
                    if time.monotonic() > end:
                        raise ConnectionError(f"could not reach the sender launcher within {CONNECT_TIMEOUT} s")
                    time.sleep(0.05)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ctrl = Control(conn)
        t_start = time.monotonic()
        net0 = iface_bytes(a.iface)
        load0 = load_average()

        if a.role == "sender":
            if a.time_limit > 0:
                watch.deadline = t_start + a.time_limit
            if a.base_port + min(a.k, a.n) > ephemeral_port_low():
                status, reason, detail = "start_failed", "port_range", "K is larger than the free port range"
            b = 0
            while status is None and b < batches:
                now = time.monotonic()
                if a.stop_early and a.time_limit > 0 and b >= 3 and now - t_start >= a.stop_early_after:
                    projected = (now - t_start) / b * batches
                    if projected > a.stop_early * a.time_limit:
                        status, detail = "timeout_projected", f"projected total {projected:.0f} s"
                        break
                first, last = b * a.k, min(a.n, (b + 1) * a.k)
                batch = Batch([cmd(slot, i) for slot, i in enumerate(range(first, last))], log)
                need = (last - first) * tasks_per_session * (2 if same_host else 1)
                state = watch.check() or batch.start(watch, need, a.task_reserve) or batch.wait_ready(watch)
                if state:
                    batch.kill()
                    batch.results()
                    status, reason, detail = STATUS[state], REASON.get(state, ""), batch.why
                    break
                # "-" = no time limit; otherwise the seconds left, at least 1 ms
                left = f"{max(0.001, watch.deadline - time.monotonic()):.3f}" if watch.deadline else "-"
                ctrl.send(f"GO {b} {left}")
                own, peer, _ = run_batch(batch, ctrl, b, watch, None, a.grace_min)
                res, why = batch.results(), batch.why
                batch = None
                state = combine(own, peer)
                if state != "ok":
                    status, reason = STATUS.get(state, "error"), REASON.get(state, "")
                    detail = f"batch {b} not counted" + (f"; {why}" if why else "")
                    break
                mem = [int(f[2]) for code, f in res if code == 0 and len(f) >= 3]
                fails = sum(1 for code, f in res if code != 0 or len(f) < 3)
                rows.append([b, len(res), sum(mem), max(mem, default=0), fails])
                b += 1
            if status is None:
                status = "done"
            ctrl.send(f"STOP {status} {reason}".strip())
        else:
            while True:
                line = ctrl.recv(None)
                parts = line.split()
                if parts[0] == "STOP":
                    status = parts[1]
                    reason = parts[2] if len(parts) > 2 else ""
                    break
                if parts[0] != "GO":
                    raise RuntimeError(f"unexpected control message: {line}")
                b = int(parts[1])
                watch.deadline = None if parts[2] == "-" else time.monotonic() + float(parts[2])
                first, last = b * a.k, min(a.n, (b + 1) * a.k)
                batch = Batch([cmd(slot, i) for slot, i in enumerate(range(first, last))], log)
                t0 = time.monotonic()
                own = watch.check() or batch.start(watch, (last - first) * tasks_per_session, a.task_reserve)
                if own:
                    batch.kill()
                    detail = batch.why
                own, peer, t1 = run_batch(batch, ctrl, b, watch, own, a.grace_min)
                res = batch.results()
                batch = None
                if combine(own, peer) != "ok":
                    continue                     # the sender ends the run with STOP
                full = [f for code, f in res if len(f) >= 6]
                ok = sum(1 for f in full if f[5] == "1")          # printed exactly the shared items
                wrong = len(full) - ok
                mem = [int(f[2]) for f in full]
                rows.append([b, len(res), round((t1 - t0) * 1000, 3), sum(mem), max(mem, default=0),
                             ok, wrong, len(res) - ok - wrong,
                             sum(int(f[3]) for f in full), sum(int(f[4]) for f in full),
                             sum(int(f[6]) for f in full if len(f) >= 7)])
    except KeyboardInterrupt as e:
        status, detail = "interrupted", f"stopped ({e or 'Ctrl-C'})"
        log.write(f"launcher {detail}\n")
    except Exception as e:                       # noqa: BLE001  - recorded in the JSON
        status, detail = "error", f"{type(e).__name__}: {e}"
        log.write(f"launcher error: {detail}\n")
    finally:
        if batch is not None:
            batch.kill()
        status = status or "error"
        total_s = time.monotonic() - t_start
        net1 = iface_bytes(a.iface)
        with open(prefix + ".csv", "w", newline="") as f:
            w = csv.writer(f)
            if a.role == "sender":
                w.writerow(["batch", "sessions", "mem_sum_kb", "mem_max_kb", "failed"])
            else:
                w.writerow(["batch", "sessions", "batch_ms", "mem_sum_kb", "mem_max_kb", "correct",
                            "wrong", "failed", "items_expected", "items_found", "items_correct"])
            w.writerows(rows)
        summary = {"run_id": a.run_id, "role": a.role, "proto": a.proto, "n": a.n, "k": a.k,
                   "size": a.size, "threads": a.threads, "seed": a.seed, "offset": a.offset, "batches": batches,
                   "batches_done": len(rows), "sessions_done": sum(r[1] for r in rows),
                   "status": status, "reason": reason, "detail": detail, "time_limit": a.time_limit,
                   "total_s": round(total_s, 3), "load_start": load0, "load_end": load_average(),
                   "mem_avail_start_mb": watch.mem_start, "mem_avail_min_mb": watch.mem_min,
                   "min_free_mb": a.min_free_mb, "stop_early": a.stop_early,
                   "stop_early_after": a.stop_early_after}
        if status == "timeout_projected" and projected:
            summary["projected_s"] = round(projected, 1)
        if net0 and net1:
            summary["tx_bytes"] = net1[0] - net0[0]
            summary["rx_bytes"] = net1[1] - net0[1]
        with open(prefix + ".json", "w") as f:
            json.dump(summary, f, indent=1)
        if conn is not None:
            conn.close()
        log.close()
    return 0 if status != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
