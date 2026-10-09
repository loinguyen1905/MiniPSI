#!/bin/bash
# Builds the dependencies, libPSI, bin/frontend.exe and benchmark/bin/psi_session (see INSTALL_AND_RUN.md).
# Usage: bash build_all.sh      (about 5-10 minutes; stops at the first error)
set -e
R="$(cd "$(dirname "$0")" && pwd)"
J=$(nproc)
CM="-DCMAKE_BUILD_TYPE=Release -DBoost_NO_BOOST_CMAKE=ON"

# Miracl, then libOTe / cryptoTools
cd $R/libOTe/cryptoTools/thirdparty/linux
bash miracl.get
cd $R/libOTe
cmake . $CM
make -j$J

# GMP, gf2x, NTL (in thirdparty/linux; gf2x.get there has a dead link, so gf2x 1.3.0 is used)
cd $R/thirdparty/linux
rm -rf gmp gf2x ntl *.tar.gz *.tar.bz2
wget -q https://ftp.gnu.org/gnu/gmp/gmp-6.1.2.tar.bz2
tar xjf gmp-6.1.2.tar.bz2
mv gmp-6.1.2 gmp
(cd gmp && ./configure --prefix=$PWD --enable-static --disable-shared && make -j$J && make install)
wget -q https://gitlab.inria.fr/gf2x/gf2x/uploads/c46b1047ba841c20d1225ae73ad6e4cd/gf2x-1.3.0.tar.gz
tar xzf gf2x-1.3.0.tar.gz
mv gf2x-1.3.0 gf2x
(cd gf2x && ./configure --prefix=$PWD ABI=64 CFLAGS="-m64 -O2" --enable-static --disable-shared \
    && make -j$J && make install)
bash ntl.get

# libsodium (configure is not in git: autogen.sh creates it)
cd $R/libsodium-stable
DO_NOT_UPDATE_CONFIG_SCRIPTS=1 sh autogen.sh
./configure --prefix=$PWD/bin --disable-shared --enable-static
make -j$J
make install

# libPSI + frontend (bin/frontend.exe), then the benchmark binary (benchmark/bin/psi_session)
# (libPSI looks for GMP only in gmp/src/, so its path is given)
cd $R
cmake . $CM -DGMP_LIB=$R/thirdparty/linux/gmp/lib/libgmp.a
make -j$J
cd $R/benchmark
./build.sh
echo "build_all.sh: done"
