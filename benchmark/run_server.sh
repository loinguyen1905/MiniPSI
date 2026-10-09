#!/bin/bash
# Runs the large cases on a server (e.g. tiger-cloud, with config.sh = config.tiger-cloud.sh):
#   1. n = 100000, K = 16, Setup 1 and Setup 2
#   2. n = 500000, K = 16, as 5 chunks of 100000 pairs: every chunk of Setup 1, then of Setup 2
#      (chunk 00 is a copy of the n = 100000 run, which covers exactly the same pairs)
# Tables of all finished runs are written to results/tables.md after each of the two steps.
# Finished runs and chunks are skipped, so running this again continues where it stopped.
# To stop gracefully: `touch STOP` in this folder (the run in progress finishes, nothing new starts).
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
mkdir -p "$HERE/results/raw"
LOG="$HERE/results/raw/progress.log"
rm -f "$HERE/STOP"

# the containers stop when the machine or Docker restarts
for c in party-a party-b party-solo; do
    docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null | grep -q true || { ./run_setup2.sh up; break; }
done

stop_if_asked() {
    if [ -f "$HERE/STOP" ]; then
        echo "$(date '+%d/%m %T') STOPPED (STOP file); run ./run_server.sh to continue"
        exit 0
    fi
}

# chunked <setup 1|2> <n> <K> <chunk size>
chunked() {
    for ((c = 0; c * $4 < $2; c++)); do
        stop_if_asked
        OFFSET=$((c * $4)) LABEL_N=$2 CHUNK=$(printf %02d $c) ./run_setup$1.sh "$4:$3"
    done
}

{
    echo "$(date '+%d/%m %T') --- run_server: 100K K=16 -> 500K K=16 (5 chunks) ---"
    ./run_setup1.sh "100000:16"; ./run_setup2.sh "100000:16"; stop_if_asked
    echo "$(date '+%d/%m %T') === done n=100000 K=16 ==="
    python3 summarize.py --markdown > results/tables.md

    # chunk 00 of n=500000 is pairs 0..99999 with the same seed, i.e. exactly the n=100000 run: reuse it
    for s in 1 2; do
        for p in r e i; do
            from="results/raw/setup${s}_${p}_n100000_k16"
            to="results/raw/setup${s}_${p}_n500000_k16_c00"
            if [ -f "${from}_sender.json" ] && [ -f "${from}_receiver.json" ] && [ ! -f "${to}_receiver.json" ]; then
                for f in "${from}"_*; do cp "$f" "${to}${f#$from}"; done
                echo "$(date +%T) reused ${from##*/} as ${to##*/}"
            fi
        done
    done

    chunked 1 500000 16 100000; stop_if_asked
    echo "$(date '+%d/%m %T') === done n=500000 K=16 Setup 1 ==="
    chunked 2 500000 16 100000; stop_if_asked
    echo "$(date '+%d/%m %T') === done n=500000 K=16 Setup 2 ==="
    python3 summarize.py --markdown > results/tables.md
    echo "$(date '+%d/%m %T') ALL DONE"
} >> "$LOG" 2>&1
