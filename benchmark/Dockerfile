# Image for Setup 2. The benchmark folder (psi_session binary, run_batches.py, results)
# is mounted at /bench, so both containers run the same build that Setup 1 used.
FROM ubuntu:24.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 iproute2 iputils-ping \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /bench
