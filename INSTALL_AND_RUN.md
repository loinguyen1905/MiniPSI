# MiniPSI: install, run PSI, run the benchmark

Use Ubuntu 24.04 or 22.04 (both tested). Ubuntu 26.04 does not work as is: its Boost has no
`boost_system` library. Needs an x86-64 CPU with AES-NI, PCLMUL and SSE4.1.

## 1. Install dependencies

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake git wget ca-certificates m4 bzip2 python3 \
    autoconf automake libtool libboost-system-dev libboost-thread-dev
# Docker, for the benchmark (skip if Docker is already installed)
sudo apt-get install -y docker.io
sudo usermod -aG docker $USER      # then log out and back in
```

## 2. Get the code and build

```bash
git clone -b experiment/batch-benchmark https://github.com/loinguyen1905/MiniPSI.git ~/MiniPSI
cd ~/MiniPSI
bash build_all.sh
```

`build_all.sh` builds GMP, gf2x, NTL, Miracl, libsodium, libOTe, libPSI, `bin/frontend.exe` and
`benchmark/bin/psi_session`. It takes about 5-10 minutes and stops at the first error.
To update the code later: `cd ~/MiniPSI && git pull`, then run `bash build_all.sh` again.

## 3. Run PSI (one session, both parties on this machine)

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

- Input: `data/input_sender.csv`, `data/input_receiver.csv` (one 128-bit item per line, 32 hex characters).
- Output: `data/output_i.csv`, `data/output_r.csv`, `data/output_e.csv`.
- Check: `diff <(sort data/output_i.csv) <(sort data/expected_intersection.csv) && echo OK` (100 shared items).

## 4. Run the benchmark

All commands run in `~/MiniPSI/benchmark`. First check the build (about 30 s, must end with `OK`):

```bash
cd ~/MiniPSI/benchmark
python3 -m unittest discover -s tests
```

**CPU split for Setup 2.** Setup 2 runs the sender and the receiver in two Docker containers with
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

**A few runs by hand.** `"n:K"` = n pairs of sets, K sessions in parallel per batch; set sizes are
random in [2, 1024]. Results go to `results/raw/`.

```bash
./run_setup1.sh "1000:16 1000:64"     # Setup 1: both parties on the host
./run_setup2.sh up                    # Setup 2: build the image, start the 2 containers (once)
./run_setup2.sh "1000:16 1000:64"
python3 summarize.py --markdown > results/tables.md
```

**The full grid** (n = 100 ... 20K, sizes 512 and 1024, K = 16 ... 1024, 15-minute limit per run).
It takes many hours, so start it in `tmux`. It starts the Setup 2 containers itself.

```bash
tmux new -s psi
cd ~/MiniPSI/benchmark
python3 run_grid.py --n 100,1000,10000,20000 --sizes 512,1024 --k-max 1024 \
    --time-limit 900 --stop-early 1.0 --stop-early-after 120
```

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

Clean up afterwards: `./run_setup2.sh down && docker rmi psi-bench`.

## Troubleshooting

- `Requested CPUs are not available`: `config.sh` lists CPUs that the server does not have.
- `permission denied` on `/var/run/docker.sock`: log out and back in after `usermod -aG docker`.
- `The legacy builder is deprecated` during `./run_setup2.sh up`: harmless, the image is still built.
