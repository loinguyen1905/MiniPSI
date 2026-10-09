# MiniPSI: install, run PSI, run the benchmark

This guide takes a fresh Ubuntu server to benchmark results in four steps:

1. [Install dependencies](#1-install-dependencies): system packages and Docker.
2. [Get the code and build](#2-get-the-code-and-build): clone this repository and build everything with one script.
3. [Run PSI](#3-run-psi-one-session-both-parties-on-this-machine): one PSI session per protocol, to check the build.
4. [Run the benchmark](#4-run-the-benchmark): many PSI sessions at once, on one machine (Setup 1)
   and in two Docker containers (Setup 2).

Every command in this guide was tested on clean Ubuntu 24.04 and 22.04 machines.

## 0. Requirements

| | |
|---|---|
| OS | Ubuntu 24.04 or 22.04. Ubuntu 26.04 does not work as is: its Boost (1.90) has no `boost_system` library. |
| CPU | x86-64 with AES-NI, PCLMUL and SSE4.1 (libOTe uses these instructions; any Intel/AMD server CPU from about 2010 on has them). |
| Disk | under 1 GB for the code, the libraries, the build and the Docker image. |
| RAM | little for single PSI runs. The benchmark needs more: `-i` with 1024 items at K = 1024 needed about 6 GB. |
| Network | the build downloads GMP, gf2x, NTL and Miracl; Setup 2 downloads the `ubuntu:24.04` Docker image once. |
| User | a normal user with `sudo` (only `apt-get` and `usermod` need it). |

## 1. Install dependencies

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake git wget ca-certificates m4 bzip2 python3 \
    autoconf automake libtool libboost-system-dev libboost-thread-dev
# Docker, for the benchmark (skip if Docker is already installed)
sudo apt-get install -y docker.io
sudo usermod -aG docker $USER      # then log out and back in
```

What each package is for:

| Package | Used for |
|---|---|
| `build-essential` | gcc, g++ and make, to compile the C/C++ code |
| `cmake` | generates the Makefiles of libOTe and libPSI |
| `git` | clones this repository, and Miracl during the build |
| `wget`, `ca-certificates` | download GMP, gf2x and NTL over https |
| `m4`, `bzip2` | GMP's `configure` needs `m4`; the GMP archive is a `.tar.bz2` |
| `python3` | runs the benchmark scripts (standard library only, nothing to `pip install`) |
| `autoconf`, `automake`, `libtool` | create libsodium's `configure` script, which is not stored in git |
| `libboost-system-dev`, `libboost-thread-dev` | Boost, used by the network layer of cryptoTools / libOTe |
| `docker.io` | Docker. Needed for the benchmark only: Setup 2 runs in containers, and `run_grid.py` and the tests call `docker`. Running PSI (section 3) does not need it. |

`usermod -aG docker $USER` lets your user run `docker` without `sudo`. It takes effect only in a
new login: log out and log in again (or open a new SSH connection). Check with `docker ps`, which
must not say `permission denied`.

## 2. Get the code and build

```bash
git clone -b experiment/batch-benchmark https://github.com/loinguyen1905/MiniPSI.git ~/MiniPSI
cd ~/MiniPSI
bash build_all.sh
```

`-b experiment/batch-benchmark` selects the branch that has the benchmark, the build fixes and this
guide; `master` has none of them.

`build_all.sh` builds everything in this order. It takes about 5-10 minutes and, because it uses
`set -e`, stops at the first error.

| Step | What | Notes |
|---|---|---|
| 1 | **Miracl** | elliptic-curve library; cloned from GitHub and compiled (`libOTe/cryptoTools/thirdparty/linux/miracl.get`) |
| 2 | **libOTe + cryptoTools** | oblivious transfer and networking; `cmake` gets `-DBoost_NO_BOOST_CMAKE=ON`, without it CMake does not find Ubuntu's static Boost libraries |
| 3 | **GMP 6.1.2** | big-number arithmetic, from ftp.gnu.org, into `thirdparty/linux/gmp` |
| 4 | **gf2x 1.3.0** | polynomials over GF(2), into `thirdparty/linux/gf2x` (the repository's own `gf2x.get` points to a dead link for 1.2) |
| 5 | **NTL 10.4.0** | polynomial and number arithmetic, used heavily by MiniPSI (`-i`); patched with `thirdparty/linux/ntl_patch` (`ntl.get`) |
| 6 | **libsodium** | Ristretto255 curve, used by all three protocols; `autogen.sh` first creates `configure` |
| 7 | **libPSI + frontend** | the PSI protocols and `bin/frontend.exe`; `cmake` gets the path of GMP, because libPSI's CMake only looks in `gmp/src/` |
| 8 | **`benchmark/build.sh`** | builds `benchmark/bin/psi_session`; Boost is linked statically so the binary also runs inside the Docker containers |

The build prints many compiler **warnings** (`warning: ...`, e.g. `-Wpessimizing-move`,
`-Wcatch-value`, `-Wmaybe-uninitialized`). They come from the old library code and are harmless.
Only a line with `error:` that stops the build is a real problem. The build has succeeded when
the last line is `build_all.sh: done` and these two files exist:

- `~/MiniPSI/bin/frontend.exe`: runs one PSI session for one party (section 3).
- `~/MiniPSI/benchmark/bin/psi_session`: the same for the benchmark, one process per session and party (section 4).

To find the error when the build stops: `bash build_all.sh 2>&1 | tee build.log`, then
`grep -n -m5 "error" build.log`.

To update the code later: `cd ~/MiniPSI && git pull`, then run `bash build_all.sh` again.
Running `build_all.sh` again is safe: it downloads and rebuilds everything from scratch.

## 3. Run PSI (one session, both parties on this machine)

**Private Set Intersection (PSI):** two parties each hold a set of items. They compute the items
the sets have in common, and neither party learns anything else about the other's set. Here the
**receiver** gets the intersection; the **sender** learns nothing. This repository has three
protocols:

| Flag | Protocol |
|---|---|
| `-i` | **MiniPSI**, the protocol of the paper (polynomial-based, elligator encoding) |
| `-r` | **DH-based PSI** (Diffie-Hellman on the Ristretto curve) |
| `-e` | **JL10** PSI, an older protocol, for comparison |

Run from the repo root (the input and output paths are relative). `0` = sender, `1` = receiver,
`-n 10` = 2^10 = 1024 items per set (the size of the sample input files). Start the sender first:
a receiver that starts before the sender is listening hangs (then stop both with `pkill frontend.exe`).

```bash
cd ~/MiniPSI
rm -f data/output_*.csv                                                    # old results
./bin/frontend.exe -i 0 -n 10 & sleep 1; ./bin/frontend.exe -i 1 -n 10    # MiniPSI (the paper's protocol)
./bin/frontend.exe -r 0 -n 10 & sleep 1; ./bin/frontend.exe -r 1 -n 10    # DH-based PSI
./bin/frontend.exe -e 0 -n 10 & sleep 1; ./bin/frontend.exe -e 1 -n 10    # JL10 PSI
```

How each line works: `&` runs the sender in the background; `sleep 1` gives it time to open its
port; then the receiver runs in the foreground, connects to it over `localhost`, and prints the
result. Without the `sleep`, the receiver sometimes connects too early and both programs wait
forever (about 1 run in 10 in our tests). `rm -f data/output_*.csv` removes the output of an earlier
run, so the check below cannot pass on old files.

- Input: `data/input_sender.csv`, `data/input_receiver.csv` (one 128-bit item per line, 32 hex characters).
- Output: `data/output_i.csv`, `data/output_r.csv`, `data/output_e.csv`.
- Check: `diff <(sort data/output_i.csv) <(sort data/expected_intersection.csv) && echo OK` (100 shared items).

The receiver prints a line `Intersection: 100 items -> data/output_i.csv` and the data sent
(`Total Comm`, about 64 KB for `-i` and 72 KB for `-r` and `-e`). To use your own sets, replace the
input files (same format); `-n k` reads the first 2^k lines of each file.

## 4. Run the benchmark

All commands run in `~/MiniPSI/benchmark`. First check the build (about 30 s, must end with `OK`):

```bash
cd ~/MiniPSI/benchmark
python3 -m unittest discover -s tests
```

The tests check the benchmark scripts and also run a few real sessions of all three protocols.

### 4.1 What the benchmark measures

The benchmark compares `-i`, `-r` and `-e` when a server has to run **many PSI sessions on small sets**:

- There are **n pairs of sets**. Pair i is the sender's set A_i and the receiver's set B_i; both have
  the same number of items, about 10% of them shared. The sets are generated from a fixed seed,
  so all three protocols get exactly the same pairs.
- The pairs run in **batches of K sessions in parallel**. Each session is two separate
  `psi_session` processes (sender and receiver) on their own TCP port. When all K sessions of a
  batch have finished, the next batch starts, until all n pairs are done.
- **Setup 1:** both parties on the same machine, talking over `localhost`.
- **Setup 2:** two Docker containers act as two machines: `party-a` (sender) and `party-b`
  (receiver), each with its own CPUs, connected by a network limited to 1 Gbit/s with `tc netem`.
- Measured per run: **a)** average time per batch, **b)** total time, **c)** peak memory per
  session process, **d)** network traffic between the two containers (Setup 2 only), and the
  **accuracy**: how many sessions returned exactly the shared items.

The scripts:

| Script | Role |
|---|---|
| `bin/psi_session` | one PSI session for one party (built from `psi_session.cpp`) |
| `run_batches.py` | runs the n sessions, K at a time, for one party; the sender and receiver launchers keep each other in step |
| `run_setup1.sh` | runs a configuration in Setup 1 |
| `run_setup2.sh` | creates the two containers (`up`), runs a configuration in Setup 2, removes them (`down`) |
| `run_grid.py` | runs the whole grid of n, set size and K, both setups, all three protocols |
| `summarize.py`, `summarize_v2.py`, `report_tables.py` | turn the raw results into CSV files and markdown tables |

### 4.2 CPU split for Setup 2 (`config.sh`)

Setup 2 runs the sender and the receiver in two Docker containers with
separate CPUs. The defaults only fit the 16-thread PC the benchmark was written on, so on the
server create `config.sh` with two sets of CPUs of the same size. Run `lscpu -e=CPU,CORE`: CPUs
with the same CORE number share a physical core and must go in the same set. Example for 6 cores /
12 threads where CPU i and i+6 share a core:

```bash
cat > config.sh <<'EOF'
CPUS_A="0-2,6-8"          # sender container
CPUS_B="3-5,9-11"         # receiver container
# NICE="nice -n 10"       # uncomment if the server runs other services
EOF
```

Why: if one physical core had a thread in each container, the sender and the receiver would
compete for it, and Setup 2 would not behave like two machines. The numbering differs between
machines: on some, CPU i and i+N/2 share a core (as above); on others, CPUs 0 and 1, 2 and 3, ...
do. Always check `lscpu`. To leave CPUs free for other work on the server, simply do not list
them. `NICE` lowers the CPU priority of the benchmark, so the server's other services come first
(this can make the measured times slower when the server is busy).

`run_setup1.sh` and `run_setup2.sh` read `config.sh` if it exists; `config.sh` is not in git, so
`git pull` never changes it.

### 4.3 A few runs by hand

`"n:K"` = n pairs of sets, K sessions in parallel per batch; set sizes are
random in [2, 1024]. Results go to `results/raw/`.

```bash
./run_setup1.sh "1000:16 1000:64"     # Setup 1: both parties on the host
./run_setup2.sh up                    # Setup 2: build the image, start the 2 containers (once)
./run_setup2.sh "1000:16 1000:64"
python3 summarize.py --markdown > results/tables.md
```

- Each configuration runs the three protocols one after the other, so they see the same machine load.
  The example (2 configurations x 3 protocols x 2 setups) takes about 6 minutes.
- `./run_setup2.sh up` builds the Docker image `psi-bench` (the first time; it downloads
  `ubuntu:24.04`), creates the network `psi-net`, starts `party-a` and `party-b` on the CPUs of
  `config.sh` and sets up the 1 Gbit/s link. It prints the CPUs of each container and a ping
  between them. Run it again after a reboot (containers do not restart by themselves).
- Each run writes `results/raw/<run id>_sender.json`, `_receiver.json` (summary), `.csv` (one row
  per batch) and `.log`. A run id looks like `setup2_i_n1000_k16`: setup, protocol, n and K.
- A run whose results already exist is skipped, so a command can simply be started again.
- `summarize.py` writes `results/setup1.csv` and `results/setup2.csv` (one row per run) and prints
  one table per metric: rows n x K, columns `-r` / `-e` / `-i`.

### 4.4 The full grid

n = 100 ... 20K, sizes 512 and 1024, K = 16 ... 1024, 15-minute limit per run.
It takes many hours, so start it in `tmux`. It starts the Setup 2 containers itself.

```bash
tmux new -s psi
cd ~/MiniPSI/benchmark
python3 run_grid.py --n 100,1000,10000,20000 --sizes 512,1024 --k-max 1024 \
    --time-limit 900 --stop-early 1.0 --stop-early-after 120
```

What the options mean:

| Option | Meaning |
|---|---|
| `--n 100,1000,10000,20000` | numbers of set pairs |
| `--sizes 512,1024` | every set has exactly this many items (unlike the runs by hand, where sizes are random) |
| `--k-max 1024` | K = 16, 32, 64, ... up to 1024 (and K = n when n is smaller) |
| `--time-limit 900` | a run may take at most 15 minutes; a longer one is stopped and recorded as `timeout` |
| `--stop-early 1.0 --stop-early-after 120` | after 2 minutes, a run whose finished batches show it would take more than 15 minutes is stopped early |
| `--results` | results folder inside `benchmark/` (default `results-v2`) |
| `--threads T` | worker threads per session and party (default 1) |
| `--dry-run` | only lists the runs |

For every n, set size and K the grid runs Setup 1 (`-r`, `-e`, `-i`), then Setup 2 (`-r`, `-e`,
`-i`): 300 runs in all. Larger configurations that clearly cannot finish in time (because a smaller
one of the same setup, protocol and size already went over the limit) are skipped and shown as
`timeout (skipped)`; runs that would not fit in the free memory are skipped as `not run (RAM)`.

`tmux` keeps the grid running when the SSH connection closes: detach with `Ctrl-b d`, come back
with `tmux attach -t psi`.

- Add `--dry-run` to only list the runs.
- `--threads T` gives every session T worker threads (default 1). A results folder holds one T only,
  so use a new one per T, e.g. `--threads 4 --results results-t4`.
- Progress: `results-v2/raw/progress.log`. Tables are updated after each (n, size) in
  `results-v2/tables.md`, `setup1.csv` and `setup2.csv`.
- Stop after the current run: `touch STOP`. Stop now: Ctrl-C. Run the same command again to
  continue; finished runs are skipped.
- Report tables: `python3 report_tables.py --results results-v2` (add `--stats` for summary figures).
- Copy the results to your PC under a new name, so they do not mix with a `results-v2` there
  (same run ids). On your PC, in `MiniPSI/`:
  `scp -r user@server:MiniPSI/benchmark/results-v2 benchmark/results-server`.
- Run one benchmark at a time (they share ports 19999 and 20000+), and not together with the tests.
- Memory: `-i` with 1024 items at K = 1024 needed about 6 GB.

More on stopping and continuing: after Ctrl-C the run in progress is recorded as `interrupted` and
is run again next time; runs that ended with `error` are also run again. `touch STOP` (in
`benchmark/`) lets the current run finish first; the file is removed when the grid starts again.

**Do not run two benchmarks, or the tests, at the same time.** Besides the shared ports: at every
start, `run_grid.py` stops all `psi_session` and `run_batches.py` processes it can see on the
machine, including those inside containers, so it would kill the other benchmark.

What the tables contain (`tables.md`, `report_tables.py`): a) time per batch, b) total time, c)
memory per process, d) traffic between the containers, e) accuracy, with rows n x K per set size
and columns Setup 1 / Setup 2 x `-r` / `-e` / `-i`. Runs that hit the time limit or were not run
are shown as `timeout`, `timeout (skipped)`, `not run (RAM)` and similar.

Clean up afterwards: `./run_setup2.sh down && docker rmi psi-bench`.

(`down` removes the two containers and the `psi-net` network; `docker rmi` removes the image.
The results folders are kept.)

## Troubleshooting

- `Requested CPUs are not available`: `config.sh` lists CPUs that the server does not have.
- `permission denied` on `/var/run/docker.sock`: log out and back in after `usermod -aG docker`.
- `The legacy builder is deprecated` during `./run_setup2.sh up`: harmless, the image is still built.
- Many `warning:` lines during the build: harmless (see section 2). Only `error:` stops the build.
- `frontend.exe` prints nothing and does not end: the receiver started before the sender was
  listening. Stop both with `pkill frontend.exe` and run the line again (with `sleep 1`).
- A run shows status `start_failed (task_limit)`: the user may not start enough threads for that K.
  Check `ulimit -u` (and `TasksMax` if the grid runs as a systemd service), or use a smaller K.
- A run shows `out of RAM` / `not run (RAM)`: free memory fell below 3 GB (`--min-free-mb`, default
  3072). Use a smaller K or a machine with more memory.
