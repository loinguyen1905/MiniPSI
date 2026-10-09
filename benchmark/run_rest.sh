#!/bin/bash
# Runs what is left of the plan, in this order:
#   1. n = 100000,  K = 16, Setup 1 and Setup 2
#   2. n = 500000,  K = 16, as 5 chunks of 100000 pairs: every chunk of Setup 1, then of Setup 2
#   3. n = 100000,  K = 32, 64, ..., 1024
# Finished runs and chunks are skipped, so running this again continues where it stopped.
# An interrupted run restarts from its beginning; thanks to the chunks that costs at most ~25 min.
# To stop gracefully: `touch STOP` in this folder (the run in progress finishes, nothing new starts).
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
LOG="$HERE/results/raw/progress.log"
rm -f "$HERE/STOP"

# a run started before (e.g. by an earlier invocation) must finish first: it holds the ports
while pgrep -f "run_batche[s].py" >/dev/null; do sleep 10; done

# the containers stop when the machine or Docker restarts
docker inspect -f '{{.State.Running}}' party-a 2>/dev/null | grep -q true || ./run_setup2.sh up

stop_if_asked() {
    if [ -f "$HERE/STOP" ]; then
        echo "$(date '+%d/%m %T') STOPPED (STOP file); run ./run_rest.sh to continue"
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
    echo "$(date '+%d/%m %T') --- run_rest: 100K K=16 -> 500K K=16 (5 chunks) -> 100K K=32..1024 ---"
    ./run_setup1.sh "100000:16"; ./run_setup2.sh "100000:16"; stop_if_asked
    echo "$(date '+%d/%m %T') === done n=100000 K=16 ==="

    chunked 1 500000 16 100000; stop_if_asked
    echo "$(date '+%d/%m %T') === done n=500000 K=16 Setup 1 ==="
    chunked 2 500000 16 100000; stop_if_asked
    echo "$(date '+%d/%m %T') === done n=500000 K=16 Setup 2 ==="

    for k in 32 64 128 256 512 1024; do
        stop_if_asked
        if [ "$k" -ge 1024 ] && [ "$(free -g | awk '/Mem/{print $7}')" -lt 18 ]; then
            echo "$(date +%T) SKIP K=$k: less than 18 GB of free RAM"
            continue
        fi
        ./run_setup1.sh "100000:$k"; ./run_setup2.sh "100000:$k"; stop_if_asked
        echo "$(date '+%d/%m %T') === done n=100000 K=$k ==="
    done
    echo "$(date '+%d/%m %T') ALL DONE"
} >> "$LOG" 2>&1
