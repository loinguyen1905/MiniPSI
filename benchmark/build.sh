#!/bin/bash
# Builds benchmark/bin/psi_session against the libraries already built in this repo
# (run `make frontend.exe` in the repo root first). Boost is linked statically so the
# binary also runs inside a plain ubuntu:24.04 container.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
R="$(cd "$HERE/.." && pwd)"

INC="-I$R/libOTe/cryptoTools -I$R -I$R/libPSI -I$R/libOTe
 -I$R/libOTe/cryptoTools/thirdparty/linux/miracl
 -I$R/thirdparty/linux/ntl/include -I$R/thirdparty/linux/gmp/include
 -I$R/thirdparty/linux/gf2x/include -I$R/libsodium-stable/bin/include"
LIBS="-Wl,--start-group $R/lib/liblibPSI.a $R/libOTe/lib/liblibOTe.a $R/libOTe/lib/libcryptoTools.a
 $R/libOTe/cryptoTools/thirdparty/linux/miracl/miracl/source/libmiracl.a
 $R/thirdparty/linux/ntl/src/libntl.a $R/thirdparty/linux/gmp/lib/libgmp.a
 $R/thirdparty/linux/gf2x/lib/libgf2x.a $R/libsodium-stable/bin/lib/libsodium.a -Wl,--end-group
 /usr/lib/x86_64-linux-gnu/libboost_thread.a /usr/lib/x86_64-linux-gnu/libboost_system.a -pthread"

mkdir -p "$HERE/bin"
g++ -std=c++14 -O3 -DNDEBUG -maes -msse2 -msse4.1 -mpclmul -pthread -w $INC \
    "$HERE/psi_session.cpp" $LIBS -o "$HERE/bin/psi_session"
echo "built $HERE/bin/psi_session"
