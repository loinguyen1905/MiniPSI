# Settings for the tiger-cloud server (Xeon E5-2680 v3: 12 cores / 24 threads, Ubuntu 22.04).
# Copy to config.sh on the server; run_setup1.sh and run_setup2.sh read it if present.
#
# CPU i and i+12 are the two threads of physical core i. Cores 10-11 (CPUs 10,11,22,23) are left
# free for the other services running on the server.
CPUS_A="0-4,12-16"        # Setup 2 sender:   cores 0-4, 10 threads
CPUS_B="5-9,17-21"        # Setup 2 receiver: cores 5-9, 10 threads
CPUS_SOLO="0-9,12-21"     # Setup 1, both parties in one container: cores 0-9, 20 threads
# The server's glibc (2.35) is older than the one psi_session was built with (2.39), so Setup 1
# also runs in an ubuntu:24.04 container (CPUS_SOLO above) instead of on the host.
NICE="nice -n 10"         # the server's own services get the CPU first
