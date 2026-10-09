#!/bin/bash
# Setup 2: the two parties run in two Docker containers that act as two machines.
#   party-a (sender) and party-b (receiver) get separate CPU sets, CPUS_A and CPUS_B.
#   Defaults are for the i5-13400F: 0-5,12-13 and 6-11,14-15, i.e. each side gets
#   3 performance cores (6 threads) + 2 efficiency cores, so neither side has faster cores.
#   network: tc netem on both eth0, 1 Gbit/s, no added delay
# Optional config.sh (e.g. a copy of config.tiger-cloud.sh) overrides CPUS_A / CPUS_B, sets NICE to
# lower the benchmark's CPU priority, and with CPUS_SOLO also creates the party-solo container
# that run_setup1.sh uses.
#
# Optional settings (environment variables), used by run_grid.py (for the run mode):
#   RESULTS=results-v2   results folder (default: results)
#   SIZE=512             every set has this many items (default: random size); adds _s<SIZE> to run ids
#   PROTOS="r e i"       protocols to run (default: all three)
#   THREADS=4            worker threads per session and party (default 1); keep one value per RESULTS folder
#   TIME_LIMIT=900       seconds a run may take (default: no limit)
#   STOP_EARLY=1.25      stop runs that clearly exceed TIME_LIMIT (see run_batches.py)
#   STOP_EARLY_AFTER=120 seconds a run must last before STOP_EARLY may stop it (default 60)
#   MIN_FREE_MB=3072     stop a run when free memory falls below this
#   CTRL_PORT, BASE_PORT control and first session port (default 19999, 20000)
#
# usage: ./run_setup2.sh up                 build the image, start the containers, add netem
#        ./run_setup2.sh "n:K n:K ..."      run the given configurations (containers must be up)
#        ./run_setup2.sh down               remove the containers and the network
HERE="$(cd "$(dirname "$0")" && pwd)"
RAW="$HERE/${RESULTS:-results}/raw"
OUT="/bench/${RESULTS:-results}/raw"
USER_ID="$(id -u):$(id -g)"
DELAY="0ms"
RATE="1gbit"
[ -f "$HERE/config.sh" ] && . "$HERE/config.sh"
CPUS_A="${CPUS_A:-0-5,12-13}"
CPUS_B="${CPUS_B:-6-11,14-15}"
EXTRA=()
[ -n "$SIZE" ] && EXTRA+=(--size "$SIZE")
[ -n "$THREADS" ] && EXTRA+=(--threads "$THREADS")
[ -n "$TIME_LIMIT" ] && EXTRA+=(--time-limit "$TIME_LIMIT")
[ -n "$STOP_EARLY" ] && EXTRA+=(--stop-early "$STOP_EARLY")
[ -n "$STOP_EARLY_AFTER" ] && EXTRA+=(--stop-early-after "$STOP_EARLY_AFTER")
[ -n "$MIN_FREE_MB" ] && EXTRA+=(--min-free-mb "$MIN_FREE_MB")
[ -n "$CTRL_PORT" ] && EXTRA+=(--ctrl-port "$CTRL_PORT")
[ -n "$BASE_PORT" ] && EXTRA+=(--base-port "$BASE_PORT")
if [ -n "$SIZE" ] && [ -z "$RESULTS" ]; then
    echo "SIZE needs RESULTS (e.g. RESULTS=results-v2), to keep these runs out of results/" >&2
    exit 1
fi
status_of() { grep -o '"status": "[a-z_]*"' "$RAW/${1}_sender.json" 2>/dev/null | cut -d'"' -f4; }

up() {
    set -e
    # build only when the image is missing: a build looks up ubuntu:24.04 on Docker Hub, which fails
    # while the network is not up yet (e.g. right after boot). After changing the Dockerfile:
    # docker build -t psi-bench .
    docker image inspect psi-bench >/dev/null 2>&1 || docker build -q -t psi-bench "$HERE" >/dev/null
    docker rm -f party-a party-b party-solo >/dev/null 2>&1 || true
    docker network create psi-net >/dev/null 2>&1 || true
    # --init: PID 1 reaps orphaned processes, e.g. sessions left over by a failed run
    docker run -d --init --name party-a --network psi-net --cpuset-cpus "$CPUS_A" \
        --cap-add NET_ADMIN -v "$HERE":/bench psi-bench sleep infinity >/dev/null
    docker run -d --init --name party-b --network psi-net --cpuset-cpus "$CPUS_B" \
        --cap-add NET_ADMIN -v "$HERE":/bench psi-bench sleep infinity >/dev/null
    for c in party-a party-b; do
        docker exec -u 0 "$c" tc qdisc add dev eth0 root netem delay "$DELAY" rate "$RATE" limit 100000
    done
    echo "party-a CPUs: $(docker exec party-a nproc) | party-b CPUs: $(docker exec party-b nproc)"
    docker exec party-b ping -c 5 -q party-a | tail -1
    if [ -n "$CPUS_SOLO" ]; then
        docker run -d --init --name party-solo --cpuset-cpus "$CPUS_SOLO" \
            -v "$HERE":/bench psi-bench sleep infinity >/dev/null
        echo "party-solo CPUs: $(docker exec party-solo nproc)"
    fi
}

down() {
    docker rm -f party-a party-b party-solo >/dev/null 2>&1
    docker network rm psi-net >/dev/null 2>&1
    echo "removed"
}

run() {
    mkdir -p "$RAW"
    for cfg in $1; do
        n=${cfg%%:*}
        k=${cfg##*:}
        for p in ${PROTOS:-r e i}; do
            if [ -f "$HERE/STOP" ]; then
                echo "$(date +%T) STOP file found, not starting more runs"
                exit 0
            fi
            id="setup2_${p}_n${LABEL_N:-$n}${SIZE:+_s$SIZE}_k${k}${CHUNK:+_c$CHUNK}"
            if [ -f "$RAW/${id}_sender.json" ] && [ -f "$RAW/${id}_receiver.json" ]; then
                echo "$(date +%T) skip $id"
                continue
            fi
            docker exec -u "$USER_ID" party-b $NICE python3 /bench/run_batches.py --role receiver \
                --proto "$p" --n "$n" --k "$k" --host party-a --iface eth0 --offset "${OFFSET:-0}" --run-id "$id" --out "$OUT" "${EXTRA[@]}" &
            receiver=$!
            if docker exec -u "$USER_ID" party-a $NICE python3 /bench/run_batches.py --role sender \
                --proto "$p" --n "$n" --k "$k" --host 0.0.0.0 --iface eth0 --offset "${OFFSET:-0}" --run-id "$id" --out "$OUT" "${EXTRA[@]}"; then
                wait "$receiver"
                echo "$(date +%T) done $id ($(status_of "$id"))"
            else
                docker exec party-b pkill -f run_batches.py
                wait "$receiver"
                docker exec party-a pkill -x psi_session
                docker exec party-b pkill -x psi_session
                echo "$(date +%T) FAILED $id"
            fi
        done
    done
}

case "$1" in
    up) up ;;
    down) down ;;
    *) run "$1" ;;
esac
