"""Tests for the benchmark tools. Run from benchmark/:  python3 -m unittest discover -s tests -v

The launcher tests start real launchers on 127.0.0.1 (ports from 26000 up); the real-protocol
tests need bin/psi_session (./build.sh). Do not run them while a benchmark is running: the
end-to-end grid test uses the benchmark's default ports 19999/20000.
"""
import csv
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import report_tables   # noqa: E402
import run_batches     # noqa: E402
import run_grid        # noqa: E402
import summarize_v2    # noqa: E402

LAUNCHER = os.path.join(HERE, "run_batches.py")
BIN = os.path.join(HERE, "bin", "psi_session")
FAKE = os.path.join(HERE, "tests", "fake_session.py")
_next_port = [26000]


def run_pair(out, n, k, proto="r", size=256, binary=BIN, time_limit=0, min_free=0, grace=15.0,
             stop_early=0, stop_early_after=60, fake_mode="", fake_sleep=None, run_id="t",
             task_reserve=2000, stop_receiver_after=None, term_twice=False, threads=1):
    """Runs one receiver + sender launcher pair; returns (json, csv rows) per role and seconds taken.

    stop_receiver_after: send SIGTERM to the receiver launcher after this many seconds.
    """
    ctrl = _next_port[0]
    _next_port[0] += k + 2
    common = ["--proto", proto, "--n", str(n), "--k", str(k), "--size", str(size), "--host", "127.0.0.1",
              "--ctrl-port", str(ctrl), "--base-port", str(ctrl + 1), "--run-id", run_id, "--out", out,
              "--bin", binary, "--min-free-mb", str(min_free), "--grace-min", str(grace),
              "--task-reserve", str(task_reserve), "--threads", str(threads)]
    env = dict(os.environ, FAKE_MODE=fake_mode)
    if fake_sleep is not None:
        env["FAKE_SLEEP"] = str(fake_sleep)
    t0 = time.monotonic()
    receiver = subprocess.Popen([sys.executable, LAUNCHER, "--role", "receiver"] + common, env=env)
    sender = subprocess.Popen([sys.executable, LAUNCHER, "--role", "sender", "--time-limit", str(time_limit),
                               "--stop-early", str(stop_early), "--stop-early-after", str(stop_early_after)]
                              + common, env=env)
    try:
        if stop_receiver_after is not None:
            time.sleep(stop_receiver_after)
            receiver.terminate()
            if term_twice:
                time.sleep(0.05)
                receiver.terminate()
        sender.wait(timeout=300)
        receiver.wait(timeout=120)
    finally:                                   # never leave launchers behind, even when a test fails
        for p in (sender, receiver):
            if p.poll() is None:
                p.kill()
                p.wait()
    took = time.monotonic() - t0
    res = {}
    for role in ("sender", "receiver"):
        with open(os.path.join(out, f"{run_id}_{role}.json")) as f:
            js = json.load(f)
        with open(os.path.join(out, f"{run_id}_{role}.csv")) as f:
            res[role] = (js, list(csv.DictReader(f)))
    return res, took


def total(rows, col):
    return sum(int(r[col]) for r in rows)


class KValues(unittest.TestCase):
    def test_powers_of_two_then_n(self):
        self.assertEqual(run_grid.k_values(100), [16, 32, 64, 100])
        self.assertEqual(run_grid.k_values(1000), [16, 32, 64, 128, 256, 512, 1000])
        self.assertEqual(run_grid.k_values(16), [16])
        self.assertEqual(run_grid.k_values(10), [10])
        self.assertEqual(run_grid.k_values(50000)[-3:], [16384, 32768, 50000])

    def test_k_max(self):
        self.assertEqual(run_grid.k_values(100, 16, 1024), [16, 32, 64, 100])
        self.assertEqual(run_grid.k_values(1000, 16, 1024), [16, 32, 64, 128, 256, 512, 1000])
        self.assertEqual(run_grid.k_values(1024, 16, 1024), [16, 32, 64, 128, 256, 512, 1024])
        self.assertEqual(run_grid.k_values(50000, 16, 1024), [16, 32, 64, 128, 256, 512, 1024])

    def test_run_id(self):
        self.assertEqual(run_grid.run_id(2, "e", 20000, 512, 64), "setup2_e_n20000_s512_k64")


@unittest.skipUnless(os.path.exists(BIN), "needs bin/psi_session (./build.sh)")
class RealProtocols(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.out)

    def test_all_protocols_exact_intersection(self):
        for proto in "rei":
            with self.subTest(proto=proto):
                res, _ = run_pair(self.out, n=5, k=2, proto=proto, size=256, run_id=f"all_{proto}")
                (sj, srows), (rj, rrows) = res["sender"], res["receiver"]
                self.assertEqual((sj["status"], rj["status"]), ("done", "done"))
                self.assertEqual((len(srows), len(rrows)), (3, 3))
                self.assertEqual(total(rrows, "sessions"), 5)
                self.assertEqual(total(rrows, "correct"), 5)
                self.assertEqual(total(rrows, "wrong") + total(rrows, "failed"), 0)
                self.assertEqual(total(rrows, "items_expected"), 5 * 25)
                self.assertEqual(total(rrows, "items_correct"), 5 * 25)
                self.assertEqual(total(rrows, "items_found"), 5 * 25)
                self.assertEqual(total(srows, "failed"), 0)

    def test_multi_thread_sessions_are_exact(self):
        """threads = worker threads per session (like frontend -t); -e needed a library fix for > 1."""
        for proto in "rei":
            for threads, size in ((2, 512), (4, 1024)):
                with self.subTest(proto=proto, threads=threads, size=size):
                    res, _ = run_pair(self.out, n=4, k=2, proto=proto, size=size, threads=threads,
                                      run_id=f"mt_{proto}_{threads}")
                    (sj, srows), (rj, rrows) = res["sender"], res["receiver"]
                    self.assertEqual((sj["status"], rj["status"], sj["threads"]), ("done", "done", threads))
                    self.assertEqual(total(rrows, "correct"), 4)
                    self.assertEqual(total(rrows, "items_correct"), 4 * (size // 10))
                    self.assertEqual(total(rrows, "items_found"), 4 * (size // 10))

    def test_random_size_still_works(self):
        res, _ = run_pair(self.out, n=3, k=3, proto="i", size=0)
        self.assertEqual(res["receiver"][0]["status"], "done")
        self.assertEqual(total(res["receiver"][1], "correct"), 3)

    def test_time_limit_stops_the_run(self):
        res, took = run_pair(self.out, n=400, k=2, proto="r", size=1024, time_limit=2)
        (sj, srows), (rj, rrows) = res["sender"], res["receiver"]
        self.assertEqual((sj["status"], rj["status"]), ("timeout", "timeout"))
        self.assertGreater(total(rrows, "sessions"), 0)
        self.assertLess(total(rrows, "sessions"), 400)
        self.assertEqual(len(srows), len(rrows))
        self.assertEqual(total(rrows, "correct"), total(rrows, "sessions"))
        self.assertLess(took, 20)

    def test_memory_guard_stops_the_run(self):
        res, _ = run_pair(self.out, n=4, k=2, min_free=10 ** 9)
        self.assertEqual(res["sender"][0]["status"], "out_of_memory")
        self.assertEqual(res["receiver"][0]["status"], "out_of_memory")
        self.assertEqual(res["receiver"][0]["sessions_done"], 0)


class FakeSessions(unittest.TestCase):
    """Failure handling, with tests/fake_session.py instead of psi_session."""

    def setUp(self):
        self.out = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.out)

    def pair(self, **kw):
        return run_pair(self.out, binary=FAKE, **kw)

    def test_hanging_receiver_session_counts_as_failed(self):
        res, took = self.pair(n=6, k=6, grace=1, fake_mode="hang_r:3")
        rj, rrows = res["receiver"]
        self.assertEqual(rj["status"], "done")
        self.assertEqual((total(rrows, "correct"), total(rrows, "failed")), (5, 1))
        self.assertLess(took, 15)

    def test_hanging_sender_session_counts_as_failed(self):
        res, took = self.pair(n=4, k=4, grace=1, fake_mode="hang_s:2")
        self.assertEqual(res["sender"][0]["status"], "done")
        self.assertEqual(total(res["sender"][1], "failed"), 1)
        self.assertEqual(total(res["receiver"][1], "correct"), 4)
        self.assertLess(took, 15)

    def test_wrong_and_crashed_results(self):
        res, _ = self.pair(n=6, k=3, fake_mode="wrong_r:1,crash_r:4")
        rrows = res["receiver"][1]
        self.assertEqual([total(rrows, c) for c in ("correct", "wrong", "failed")], [4, 1, 1])
        self.assertEqual(total(rrows, "items_expected"), 5 * 25)     # 5 sessions printed a RESULT
        self.assertEqual(total(rrows, "items_correct"), 5 * 25 - 1)

    def test_sender_not_ready_stops_the_run(self):
        res, took = self.pair(n=6, k=3, fake_mode="noready_s:4")
        for role in ("sender", "receiver"):
            self.assertEqual(res[role][0]["status"], "start_failed")
            self.assertEqual(res[role][0]["batches_done"], 1)
        self.assertLess(took, 15)

    def test_deadline_during_a_batch_discards_it(self):
        res, took = self.pair(n=4, k=4, time_limit=1, fake_sleep=5)
        for role in ("sender", "receiver"):
            self.assertEqual(res[role][0]["status"], "timeout")
            self.assertEqual(res[role][0]["sessions_done"], 0)
        self.assertLess(took, 10)

    def test_stop_early_when_clearly_too_long(self):
        res, took = self.pair(n=40, k=2, time_limit=2, fake_sleep=0.3, stop_early=1.25, stop_early_after=0.5)
        self.assertEqual(res["sender"][0]["status"], "timeout_projected")
        self.assertEqual(res["receiver"][0]["status"], "timeout_projected")
        self.assertLess(took, 5)


def write_run(raw, rid, status="done", pairs=(4, 4), correct=None, batch_ms=100.0, traffic=False,
              mem=(20000, 15000), total_s=10.0):
    """Synthetic launcher output: one batch per entry of pairs."""
    correct = pairs if correct is None else correct
    base = {"status": status, "total_s": total_s, "load_start": 1.0, "load_end": 3.0,
            "mem_avail_start_mb": mem[0], "mem_avail_min_mb": mem[1], "batches_done": len(pairs),
            "sessions_done": sum(pairs), "detail": ""}
    if traffic:
        base.update(tx_bytes=1024 * 1024, rx_bytes=2048)
    for role in ("sender", "receiver"):
        with open(os.path.join(raw, f"{rid}_{role}.json"), "w") as f:
            json.dump(dict(base, role=role), f)
    with open(os.path.join(raw, f"{rid}_sender.csv"), "w") as f:
        f.write("batch,sessions,mem_sum_kb,mem_max_kb,failed\n")
        for b, p in enumerate(pairs):
            f.write(f"{b},{p},{p * 5000},5000,0\n")
    with open(os.path.join(raw, f"{rid}_receiver.csv"), "w") as f:
        f.write("batch,sessions,batch_ms,mem_sum_kb,mem_max_kb,correct,wrong,failed,"
                "items_expected,items_found,items_correct\n")
        for b, (p, c) in enumerate(zip(pairs, correct)):
            f.write(f"{b},{p},{batch_ms},{p * 7000},7000,{c},{p - c},0,{p * 25},{p * 25},{c * 25}\n")


class GridDecisions(unittest.TestCase):
    def setUp(self):
        self.raw = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.raw)

    def test_done_runs_are_not_repeated_and_skips_are_rechecked(self):
        write_run(self.raw, "setup1_r_n100_s256_k16", mem=(20000, 19800))
        self.assertEqual(run_grid.decide(self.raw, 1, "r", 100, 256, 16, 20000, 3072)[0], "done")
        with open(os.path.join(self.raw, "setup1_r_n100_s256_k32_skip.json"), "w") as f:
            json.dump({"status": "not_run_memory"}, f)     # an old skip no longer blocks the run
        self.assertEqual(run_grid.decide(self.raw, 1, "r", 100, 256, 32, 20000, 3072)[0], "run")

    def test_interrupted_and_failed_runs_are_run_again(self):
        write_run(self.raw, "setup1_r_n100_s256_k16", status="interrupted")
        write_run(self.raw, "setup1_e_n100_s256_k16", status="error")
        write_run(self.raw, "setup1_i_n100_s256_k16", status="out_of_memory", pairs=())
        self.assertEqual(run_grid.decide(self.raw, 1, "r", 100, 256, 16, 20000, 3072)[0], "run")
        self.assertEqual(run_grid.decide(self.raw, 1, "e", 100, 256, 16, 20000, 3072)[0], "run")
        self.assertEqual(run_grid.decide(self.raw, 1, "i", 100, 256, 16, 20000, 3072)[0], "done")
        retry = run_grid.finished_runs(self.raw, retry=("out_of_memory",))
        self.assertEqual(run_grid.decide(self.raw, 1, "i", 100, 256, 16, 20000, 3072, retry)[0], "run")

    def test_only_resource_failures_block_larger_k(self):
        write_run(self.raw, "setup1_r_n1000_s256_k512", status="start_failed", pairs=())   # no reason
        self.assertEqual(run_grid.decide(self.raw, 1, "r", 1000, 256, 1000, 20000, 3072)[0], "run")
        for role in ("sender", "receiver"):
            path = os.path.join(self.raw, f"setup1_r_n1000_s256_k512_{role}.json")
            with open(path) as f:
                j = json.load(f)
            j["reason"] = "task_limit"
            with open(path, "w") as f:
                json.dump(j, f)
        action, marker = run_grid.decide(self.raw, 1, "r", 1000, 256, 1000, 20000, 3072)
        self.assertEqual((action, marker["status"], marker["limit"]), ("skip", "not_run_limit", "tasks"))

    def test_larger_k_after_out_of_memory_is_skipped_for_every_n(self):
        write_run(self.raw, "setup1_i_n1000_s1024_k1000", status="out_of_memory", pairs=())
        action, marker = run_grid.decide(self.raw, 1, "i", 10000, 1024, 2048, 20000, 3072)
        self.assertEqual((action, marker["status"]), ("skip", "not_run_limit"))
        self.assertEqual(run_grid.decide(self.raw, 1, "i", 10000, 1024, 512, 20000, 3072)[0], "run")
        self.assertEqual(run_grid.decide(self.raw, 1, "r", 10000, 1024, 2048, 20000, 3072)[0], "run")
        self.assertEqual(run_grid.decide(self.raw, 2, "i", 10000, 1024, 2048, 20000, 3072)[0], "run")
        self.assertEqual(run_grid.decide(self.raw, 1, "i", 10000, 512, 2048, 20000, 3072)[0], "run")

    def test_memory_estimate_from_smaller_k(self):
        write_run(self.raw, "setup1_i_n10000_s1024_k1024", mem=(20000, 8000))      # used 12 GB
        action, marker = run_grid.decide(self.raw, 1, "i", 10000, 1024, 2048, 20000, 3072)
        self.assertEqual((action, marker["status"], marker["predicted_mb"]), ("skip", "not_run_memory", 24000))
        self.assertEqual(run_grid.decide(self.raw, 1, "i", 10000, 1024, 2048, 40000, 3072)[0], "run")

    def test_runs_sure_to_exceed_the_time_limit_are_skipped(self):
        def timed_out(rid, done, total=900.0, projected=None):
            write_run(self.raw, rid, status="timeout_projected" if projected else "timeout",
                      pairs=(done,), total_s=total)
            if projected:
                for role in ("sender", "receiver"):
                    path = os.path.join(self.raw, f"{rid}_{role}.json")
                    with open(path) as f:
                        j = json.load(f)
                    j["projected_s"] = projected
                    with open(path, "w") as f:
                        json.dump(j, f)
        timed_out("setup1_i_n40000_s1024_k32", 0, total=70, projected=1300)      # clearly over (1300 s)
        d = lambda n, k, st=1, p="i", size=1024: run_grid.decide(self.raw, st, p, n, size, k, 20000, 3072,
                                                                   time_limit=900)
        action, marker = d(40000, 64)
        self.assertEqual((action, marker["status"]), ("skip", "not_run_timeout"))
        self.assertEqual(d(40000, 16)[0], "skip")                   # K = 16 is slower than K = 32
        self.assertEqual(d(50000, 1024)[0], "skip")                 # larger n takes longer
        self.assertEqual(d(30000, 64)[0], "run")                    # smaller n is not implied
        self.assertEqual(d(40000, 64, st=2)[0], "run")              # other setup, protocol, size
        self.assertEqual(d(40000, 64, p="r")[0], "run")
        self.assertEqual(d(40000, 64, size=512)[0], "run")
        self.assertEqual(run_grid.decide(self.raw, 1, "i", 40000, 1024, 64, 20000, 3072)[0], "run")  # off
        timed_out("setup1_r_n30000_s1024_k16", 28000)               # 900 s for 93%: only 7% over
        self.assertEqual(d(30000, 32, p="r")[0], "run")             # borderline: really run it
        timed_out("setup1_e_n30000_s1024_k16", 10000)               # 900 s for a third: 45 min
        self.assertEqual(d(40000, 16, p="e")[0], "skip")             # same K, larger n
        self.assertEqual(d(30000, 32, p="e")[0], "run")             # K = 16 says nothing about K = 32

    def test_one_threads_value_per_results_folder(self):
        name = f"tests_tmp_threads_{os.getpid()}"
        folder = os.path.join(HERE, name)
        try:
            os.makedirs(os.path.join(folder, "raw"))
            write_run(os.path.join(folder, "raw"), "setup1_r_n100_s256_k16")      # threads missing = 1
            out = subprocess.run([sys.executable, os.path.join(HERE, "run_grid.py"), "--dry-run", "--n", "100",
                                  "--sizes", "256", "--results", name, "--threads", "4"],
                                 capture_output=True, text=True, timeout=60)
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("threads = [1]", out.stderr)
            ok = subprocess.run([sys.executable, os.path.join(HERE, "run_grid.py"), "--dry-run", "--n", "100",
                                 "--sizes", "256", "--results", name], capture_output=True, text=True, timeout=60)
            self.assertEqual(ok.returncode, 0)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_small_measurements_do_not_block(self):
        write_run(self.raw, "setup1_r_n100_s256_k16", mem=(20000, 19900))
        self.assertEqual(run_grid.decide(self.raw, 1, "r", 100, 256, 100, 3000, 3072)[0], "run")


class ReportTables(unittest.TestCase):
    def test_tables_from_csv(self):
        folder = tempfile.mkdtemp()
        try:
            raw = os.path.join(folder, "raw")
            os.makedirs(raw)
            for st in (1, 2):
                for p in "rei":
                    write_run(raw, f"setup{st}_{p}_n8_s256_k4", pairs=(4, 4), traffic=st == 2, batch_ms=1473.46)
            write_run(raw, "setup1_r_n16_s256_k4", status="timeout", pairs=(4,), total_s=900)
            with open(os.path.join(raw, "setup1_e_n16_s256_k4_skip.json"), "w") as f:
                json.dump({"status": "not_run_timeout", "limit": "time", "detail": "x"}, f)
            rows = summarize_v2.collect(raw)
            for st in (1, 2):
                summarize_v2.write_csv(os.path.join(folder, f"setup{st}.csv"), rows[st], traffic=st == 2)
            out = io.StringIO()
            report_tables.tables(report_tables.load(folder), out)
            text = out.getvalue()
            self.assertIn("| 8 | 4 | 1,473 | 1,473 | 1,473 | 1,473 | 1,473 | 1,473 |", text)
            self.assertIn("| 16 | 4 | timeout (25%) | timeout (skipped) |", text)
            self.assertIn("| 256 | 8 | 2.00 MB | 2.00 MB | 2.00 MB | 256.0 KB | 256.0 KB | 256.0 KB |", text)
            self.assertIn("| 8 | 4 | 100% | 100% | 100% | 100% | 100% | 100% |", text)
            self.assertIn("Per batch (sum over the 2K processes of a batch, MB), n = 8,", text)
            R = report_tables.load(folder)
            kept, dropped = report_tables.complete_blocks(R, k_max=4, k_min=4)
            self.assertEqual({k[2] for k in kept}, {8})                 # n = 16 has only 2 of 6 runs
            self.assertEqual(dropped, [(16, 256, 2, 6)])
            out = io.StringIO()
            report_tables.stats(kept, out)
            self.assertIn("sessions correct 48 / 48", out.getvalue())
        finally:
            shutil.rmtree(folder)


class BatchWait(unittest.TestCase):
    def test_peer_fin_is_seen_at_once(self):
        """Once its own sessions are done, a launcher must react to the other side's FIN at once,
        not at the next 0.1 s tick (that added up to 0.1 s between batches)."""
        a, b = socket.socketpair()
        try:
            got = []

            def peer():
                got.append(b.recv(100))           # our FIN
                time.sleep(0.03)
                b.sendall(b"FIN 7 ok\n")

            t = threading.Thread(target=peer)
            t.start()
            batch = run_batches.Batch([], None)
            t0 = time.monotonic()
            own, theirs, _ = run_batches.run_batch(batch, run_batches.Control(a), 7, run_batches.Watch(0), "ok", 15)
            took = time.monotonic() - t0
            t.join()
            self.assertEqual((own, theirs, got), ("ok", "ok", [b"FIN 7 ok\n"]))
            self.assertLess(took, 0.08)
        finally:
            a.close()
            b.close()

    def test_exit_seen_without_waiting_a_tick(self):
        batch = run_batches.Batch([[sys.executable, "-c", "print('RESULT 1')"]], None)
        self.assertIsNone(batch.start(run_batches.Watch(0), 0, 0))
        t0 = time.monotonic()
        while batch.alive():
            batch.pump(run_batches.POLL)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(batch.results(), [(0, ["1"])])


class ContainerStart(unittest.TestCase):
    def test_retries_until_the_containers_run(self):
        calls, said = [], []
        results = iter([1, 1, 0])                       # two failed attempts, then success
        orig = (run_grid.subprocess.run, run_grid.containers_running, run_grid.time.sleep)
        try:
            run_grid.subprocess.run = lambda *a, **kw: calls.append(a) or subprocess.CompletedProcess(a, next(results))
            run_grid.containers_running = lambda: len(calls) == 3
            run_grid.time.sleep = lambda s: None
            run_grid.start_containers({}, None, said.append, attempts=5, wait=0)
            self.assertEqual(len(calls), 3)
            self.assertIn("(attempt 3)", said[-1])
            calls.clear()
            results = iter([1] * 5)
            run_grid.containers_running = lambda: False
            with self.assertRaises(RuntimeError):
                run_grid.start_containers({}, None, said.append, attempts=2, wait=0)
            self.assertEqual(len(calls), 2)
        finally:
            run_grid.subprocess.run, run_grid.containers_running, run_grid.time.sleep = orig


class Summary(unittest.TestCase):
    def setUp(self):
        self.raw = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.raw)

    def test_rows_and_cells(self):
        write_run(self.raw, "setup1_r_n8_s256_k4", pairs=(4, 4), correct=(4, 3))
        write_run(self.raw, "setup1_e_n8_s256_k4", status="timeout", pairs=(4,), total_s=900)
        write_run(self.raw, "setup2_r_n8_s256_k4", traffic=True)
        with open(os.path.join(self.raw, "setup1_i_n8_s256_k8_skip.json"), "w") as f:
            json.dump({"status": "not_run_memory", "detail": "needs 30 GB"}, f)
        with open(os.path.join(self.raw, "setup1_i_n8_s256_k4_sender.json"), "w") as f:
            json.dump({"status": "done"}, f)                          # receiver missing: incomplete
        rows = summarize_v2.collect(self.raw)
        s1 = {(x["protocol"], x["K"]): x for x in rows[1]}
        self.assertEqual(set(s1), {("r", 4), ("e", 4), ("i", 8)})
        r = s1[("r", 4)]
        self.assertEqual((r["pairs_done"], r["batches"], r["accuracy_pct"], r["sessions_wrong"]), (8, 2, 87.5, 1))
        self.assertEqual(r["avg_memory_per_session_mb"], round((8 * 5000 + 8 * 7000) / 16 / 1024, 4))
        self.assertEqual(r["avg_batch_ms"], 100.0)
        self.assertEqual(r["threads"], 1)
        self.assertEqual(summarize_v2.cell(r, "accuracy_pct"), "87.50% (-1)")
        e = s1[("e", 4)]
        self.assertEqual(summarize_v2.cell(e, "total_s"), "timeout (50%)")
        self.assertEqual(summarize_v2.cell(e, "avg_batch_ms"), "100 t")
        self.assertEqual(summarize_v2.cell(e, "accuracy_pct"), "100% t")
        self.assertEqual(summarize_v2.cell(s1[("i", 8)], "avg_batch_ms"), "not run (RAM)")
        self.assertEqual(rows[2][0]["traffic_mb"], round((2 * 1024 * 1024) / (1024 * 1024), 6))
        md = summarize_v2.markdown(rows)
        self.assertIn("## Set size 256", md)
        self.assertIn("| 8 | 4 | 100 | 100 t |", md)


class SummaryDetails(unittest.TestCase):
    def setUp(self):
        self.raw = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.raw)

    def test_accuracy_and_timeout_never_round_up(self):
        write_run(self.raw, "setup1_r_n50000_s256_k1000", pairs=(1000,) * 50, correct=(1000,) * 49 + (999,))
        write_run(self.raw, "setup1_e_n50000_s256_k1000", status="timeout", pairs=(1000,) * 49 + (999,))
        rows = {x["protocol"]: x for x in summarize_v2.collect(self.raw)[1]}
        self.assertEqual(summarize_v2.cell(rows["r"], "accuracy_pct"), "99.99% (-1)")
        self.assertEqual(summarize_v2.cell(rows["e"], "total_s"), "timeout (99%)")

    def test_values_are_rounded_only_once(self):
        write_run(self.raw, "setup1_r_n8_s256_k4", pairs=(4, 4), batch_ms=1473.46)
        row = summarize_v2.collect(self.raw)[1][0]
        # 1473.46 rounds to 1,473; rounding to 1473.5 first would give 1,474
        self.assertEqual(summarize_v2.cell(row, "avg_batch_ms"), "1,473")

    def test_per_batch_values_use_full_batches(self):
        write_run(self.raw, "setup1_r_n6_s256_k4", pairs=(4, 2), batch_ms=100.0)
        with open(os.path.join(self.raw, "setup1_r_n6_s256_k4_receiver.csv")) as f:
            text = f.read().replace("\n1,2,100.0,", "\n1,2,10.0,")
        with open(os.path.join(self.raw, "setup1_r_n6_s256_k4_receiver.csv"), "w") as f:
            f.write(text)
        row = summarize_v2.collect(self.raw)[1][0]
        self.assertEqual(row["avg_batch_ms"], 100.0)                    # the 2-session batch is left out
        self.assertEqual(row["avg_memory_per_batch_mb"], round(4 * 12000 / 1024, 3))

    def test_projected_and_skipped_timeouts(self):
        write_run(self.raw, "setup1_r_n40000_s1024_k32", status="timeout_projected", pairs=(32,) * 90, total_s=75)
        path = os.path.join(self.raw, "setup1_r_n40000_s1024_k32_sender.json")
        with open(path) as f:
            j = json.load(f)
        j["projected_s"] = 1250.0
        with open(path, "w") as f:
            json.dump(j, f)
        with open(os.path.join(self.raw, "setup1_r_n40000_s1024_k64_skip.json"), "w") as f:
            json.dump({"status": "not_run_timeout", "limit": "time", "detail": "x"}, f)
        rows = {x["K"]: x for x in summarize_v2.collect(self.raw)[1]}
        self.assertEqual(summarize_v2.cell(rows[32], "total_s"), "timeout (~21 min)")
        self.assertEqual(summarize_v2.cell(rows[64], "total_s"), "timeout (skipped)")
        self.assertEqual(summarize_v2.cell(rows[64], "accuracy_pct"), "timeout (skipped)")

    def test_labels_traffic_and_bad_files(self):
        write_run(self.raw, "setup2_r_n8_s256_k4", status="timeout", pairs=(4,), traffic=True)
        with open(os.path.join(self.raw, "setup2_e_n8_s256_k8_skip.json"), "w") as f:
            json.dump({"status": "not_run_limit", "limit": "tasks", "detail": "x"}, f)
        write_run(self.raw, "setup2_i_n8_s256_k4")
        with open(os.path.join(self.raw, "setup2_i_n8_s256_k4_sender.json"), "w") as f:
            f.write("{truncated")
        rows = {x["protocol"]: x for x in summarize_v2.collect(self.raw)[2]}
        self.assertNotIn("i", rows)                                     # unreadable: left out, no crash
        self.assertEqual(summarize_v2.cell(rows["r"], "traffic_mb"), "timeout")
        self.assertEqual(summarize_v2.cell(rows["e"], "avg_batch_ms"), "not run (threads)")


class ResourceHandling(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.out)

    def test_exit_75_is_a_resource_failure(self):
        for mode in ("resource_s:4", "resource_r:4"):
            with self.subTest(mode=mode):
                res, took = run_pair(self.out, n=6, k=3, binary=FAKE, fake_mode=mode, run_id=mode.replace(":", "_"))
                for role in ("sender", "receiver"):
                    self.assertEqual((res[role][0]["status"], res[role][0]["reason"]), ("start_failed", "task_limit"))
                    self.assertEqual(res[role][0]["batches_done"], 1)
                self.assertLess(took, 20)

    def test_not_enough_free_tasks(self):
        res, _ = run_pair(self.out, n=4, k=2, binary=FAKE, task_reserve=10 ** 9)
        self.assertEqual((res["sender"][0]["status"], res["sender"][0]["reason"]), ("start_failed", "task_limit"))
        self.assertEqual(res["receiver"][0]["sessions_done"], 0)

    def test_second_sigterm_does_not_break_the_cleanup(self):
        res, took = run_pair(self.out, n=40, k=40, binary=FAKE, fake_sleep=30,
                             stop_receiver_after=1.5, term_twice=True, run_id="term2")
        self.assertEqual(res["receiver"][0]["status"], "interrupted")
        self.assertLess(took, 30)

    def test_sigterm_records_interrupted_and_kills_sessions(self):
        res, took = run_pair(self.out, n=4, k=4, binary=FAKE, fake_mode="hang_r:0,hang_s:0",
                             stop_receiver_after=1.5, run_id="term")
        self.assertEqual(res["receiver"][0]["status"], "interrupted")
        self.assertIn(res["sender"][0]["status"], ("error", "interrupted"))
        self.assertLess(took, 30)
        time.sleep(0.5)
        left = subprocess.run(["pgrep", "-f", f"fake_session.py [rei] [sr] 127.0.0.1 {_next_port[0] - 5}"],
                              capture_output=True, text=True).stdout.split()
        self.assertEqual(left, [])


@unittest.skipUnless(os.path.exists(BIN), "needs bin/psi_session (./build.sh)")
class EndToEndGrid(unittest.TestCase):
    def test_tiny_grid(self):
        # run_grid.py stops leftover benchmark processes and removes the STOP file when it starts,
        # so never run this next to a real benchmark
        busy = subprocess.run(["pgrep", "-x", "psi_session"], capture_output=True).returncode == 0
        if busy or os.path.exists(os.path.join(HERE, "STOP")):
            self.skipTest("a benchmark is running (or STOP exists)")
        name = f"tests_tmp_results_{os.getpid()}"
        folder = os.path.join(HERE, name)
        env = dict(os.environ, CTRL_PORT="25000", BASE_PORT="25001")
        try:
            subprocess.run([sys.executable, os.path.join(HERE, "run_grid.py"), "--n", "20", "--sizes", "256",
                            "--setups", "1", "--protos", "r,i", "--results", name, "--time-limit", "120",
                            "--stop-early", "1.5", "--stop-early-after", "30"],
                           check=True, timeout=300, stdout=subprocess.DEVNULL, env=env)
            with open(os.path.join(folder, "raw", "setup1_i_n20_s256_k20_sender.json")) as f:
                j = json.load(f)
            self.assertEqual((j["time_limit"], j["stop_early"], j["stop_early_after"]), (120.0, 1.5, 30.0))
            with open(os.path.join(folder, "setup1.csv")) as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([(x["protocol"], x["K"]) for x in rows], [("r", "16"), ("i", "16"), ("r", "20"), ("i", "20")])
            for x in rows:
                self.assertEqual((x["status"], x["pairs_done"], x["accuracy_pct"]), ("done", "20", "100.0"))
            self.assertTrue(os.path.exists(os.path.join(folder, "tables.md")))
            before = os.path.getmtime(os.path.join(folder, "raw", "setup1_r_n20_s256_k16_sender.json"))
            subprocess.run([sys.executable, os.path.join(HERE, "run_grid.py"), "--n", "20", "--sizes", "256",
                            "--setups", "1", "--protos", "r,i", "--results", name], check=True, timeout=60,
                           stdout=subprocess.DEVNULL, env=env)
            after = os.path.getmtime(os.path.join(folder, "raw", "setup1_r_n20_s256_k16_sender.json"))
            self.assertEqual(before, after)                           # finished runs are not repeated
        finally:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
