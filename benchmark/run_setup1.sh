#!/bin/bash
# Setup 1: both parties on one machine, talking over localhost.
# usage: ./run_setup1.sh "n:K n:K ..."        e.g. ./run_setup1.sh "100:16 1000:16"
# For every n:K the three protocols run back to back, so they see similar machine load.
# A run whose results already exist is skipped, so the script can be restarted.
#
# Optional config.sh (e.g. a copy of config.tiger-cloud.sh): when it sets CPUS_SOLO, both parties
# run inside the party-solo container instead of on the host, for machines whose OS cannot run
# the psi_session binary. NICE, if set, lowers the benchmark's CPU priority.
#
# Optional settings (environment variables), used by run_grid.py:
#   RESULTS=results-v2   results folder (default: results)
#   SIZE=512             every set has this many items (default: random size); adds _s<SIZE> to run ids
#   PROTOS="r e i"       protocols to run (default: all three)
#   THREADS=4            worker threads per session and party (default 1); keep one value per RESULTS folder
#   TIME_LIMIT=900       seconds a run may take (default: no limit)
#   STOP_EARLY=1.25      stop runs that clearly exceed TIME_LIMIT (see run_batches.py)
#   STOP_EARLY_AFTER=120 seconds a run must last before STOP_EARLY may stop it (default 60)
#   MIN_FREE_MB=3072     stop a run when free memory falls below this
#   CTRL_PORT, BASE_PORT control and first session port (default 19999, 20000)
HERE="$(cd "$(dirname "$0")" && pwd)"
RAW="$HERE/${RESULTS:-results}/raw"
[ -f "$HERE/config.sh" ] && . "$HERE/config.sh"
mkdir -p "$RAW"
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

if [ -n "$CPUS_SOLO" ]; then
    RUN=(docker exec -u "$(id -u):$(id -g)" party-solo $NICE python3 /bench/run_batches.py)
    OUT=/bench/${RESULTS:-results}/raw
    clean() { docker exec party-solo pkill -f run_batches.py; docker exec party-solo pkill -x psi_session; }
else
    RUN=($NICE python3 "$HERE/run_batches.py")
    OUT="$RAW"
    clean() { pkill -x psi_session; }
fi

for cfg in $1; do
    n=${cfg%%:*}
    k=${cfg##*:}
    for p in ${PROTOS:-r e i}; do
        if [ -f "$HERE/STOP" ]; then
            echo "$(date +%T) STOP file found, not starting more runs"
            exit 0
        fi
        id="setup1_${p}_n${LABEL_N:-$n}${SIZE:+_s$SIZE}_k${k}${CHUNK:+_c$CHUNK}"
        if [ -f "$RAW/${id}_sender.json" ] && [ -f "$RAW/${id}_receiver.json" ]; then
            echo "$(date +%T) skip $id"
            continue
        fi
        "${RUN[@]}" --role receiver --proto "$p" --n "$n" --k "$k" \
            --host 127.0.0.1 --offset "${OFFSET:-0}" --run-id "$id" --out "$OUT" "${EXTRA[@]}" &
        receiver=$!
        if "${RUN[@]}" --role sender --proto "$p" --n "$n" --k "$k" \
            --host 127.0.0.1 --offset "${OFFSET:-0}" --run-id "$id" --out "$OUT" "${EXTRA[@]}"; then
            wait "$receiver"
            echo "$(date +%T) done $id ($(status_of "$id"))"
        else
            kill "$receiver" 2>/dev/null
            clean
            wait "$receiver"
            echo "$(date +%T) FAILED $id"
        fi
    done
done
