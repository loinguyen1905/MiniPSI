// Runs ONE PSI session for ONE party, using the same library calls as frontend/main.cpp.
//
// usage: psi_session <proto r|e|i> <role s|r> <host> <port> <index> <seed> [size [threads]]
//   r = DH-based (EcdhPsi, Ristretto curve), e = JL10, i = MiniPSI (MiniSender_Ris / MiniReceiver_Ris)
//   s = sender: listens on host:port, prints "READY" once listening
//   r = receiver: connects to host:port
//
// Pair <index> is generated from <seed>, identically on both sides:
//   size   = <size> items in each set; if <size> is 0 or omitted, random in [2, 1024]
//            (a set of 1 item crashes the -i protocol)
//   common = size / 10 items shared by A and B
// threads: worker threads of the session on each side, like -t of frontend/main.cpp (default 1).
//   The library creates one worker thread per channel and splits the set between them, so the
//   session opens <threads> channels; both parties must use the same value.
// Last line printed: "RESULT <index> <size> <peak_mem_kb> <common> <found> <ok> <tp>" (receiver)
//                 or "RESULT <index> <size> <peak_mem_kb>" (sender).
//   found = items the protocol returned, tp = how many of them are really shared,
//   ok = 1 if the result is exactly the shared items
// Exit code: 0 ok, 2 wrong result, 75 out of system resources (threads, processes or memory).
//
// The IOService gets 1 thread (it only moves bytes between the sockets and the workers): the
// library's default of one per CPU (17 tasks per process) hits the task limit when thousands of
// sessions run at once. A process has 2 + threads tasks (main, IOService, workers).
#include <cryptoTools/Network/IOService.h>
#include <cryptoTools/Network/Endpoint.h>
#include <cryptoTools/Network/Channel.h>
#include <cryptoTools/Crypto/PRNG.h>
#include "ECDH/EcdhPsiSender.h"
#include "ECDH/EcdhPsiReceiver.h"
#include "ECDH/JL10PsiSender.h"
#include "ECDH/JL10PsiReceiver.h"
#include "MiniPSI_ristretto/MiniSender_Ris.h"
#include "MiniPSI_ristretto/MiniReceiver_Ris.h"
#include <algorithm>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <new>
#include <string>
#include <system_error>
#include <vector>

using namespace osuCrypto;

namespace
{
    block seedBlock(u64 seed, u64 index, u64 tag)
    {
        return _mm_set_epi64x((long long)(seed ^ (tag << 56)), (long long)index);
    }

    // Discards the library's own console output.
    struct NullBuf : std::streambuf
    {
        int overflow(int c) override { return c; }
    };

    // Silences std::cout while it exists, also when an exception leaves the scope.
    struct Quiet
    {
        NullBuf buf;
        std::streambuf* old;
        Quiet() : old(std::cout.rdbuf(&buf)) {}
        ~Quiet() { std::cout.rdbuf(old); }
    };

    // Peak resident memory of this program in KB. VmHWM belongs to the address space created
    // by exec, so unlike the parent's wait4() it does not include the launcher it was forked from.
    long peakMemoryKb()
    {
        std::ifstream f("/proc/self/status");
        std::string line;
        while (std::getline(f, line))
            if (line.rfind("VmHWM:", 0) == 0)
                return std::atol(line.c_str() + 6);
        return -1;
    }
}

int run(int argc, char** argv)
{
    const u64 fixedSize = argc >= 8 ? std::strtoull(argv[7], nullptr, 10) : 0;
    const u64 numThreads = argc >= 9 ? std::strtoull(argv[8], nullptr, 10) : 1;
    if (argc < 7 || argc > 9 || fixedSize == 1 || numThreads < 1 || numThreads > 64)
    {
        std::cerr << "usage: psi_session <r|e|i> <s|r> <host> <port> <index> <seed> "
                     "[size, 0 or >= 2 [threads, 1 to 64]]" << std::endl;
        return 1;
    }
    const char proto = argv[1][0];
    const bool isSender = argv[2][0] == 's';
    const std::string host = argv[3];
    const u32 port = (u32)std::atoi(argv[4]);
    const u64 index = std::strtoull(argv[5], nullptr, 10);
    const u64 seed = std::strtoull(argv[6], nullptr, 10);

    // ---- data of pair <index> ----
    PRNG meta(seedBlock(seed, index, 0));
    const u64 randomSize = 2 + meta.get<u64>() % 1023;
    const u64 size = fixedSize ? fixedSize : randomSize;
    const u64 common = size / 10;

    PRNG pa(seedBlock(seed, index, 1)), pb(seedBlock(seed, index, 2));
    std::vector<block> A(size), B(size);
    for (auto& x : A) x = pa.get<block>();
    for (auto& x : B) x = pb.get<block>();
    for (u64 j = 0; j < common; ++j) B[j] = A[j];     // B[0..common-1] are the shared items

    // ---- network ----
    IOService ios(1);
    Endpoint ep(ios, host, port, isSender ? EpMode::Server : EpMode::Client, "psi");
    if (isSender)
        std::cout << "READY" << std::endl;            // the port is listening once Endpoint is constructed
    std::vector<Channel> chls;
    for (u64 t = 0; t < numThreads; ++t)
        chls.push_back(ep.addChannel("c" + std::to_string(t), "c" + std::to_string(t)));

    PRNG prng(seedBlock(seed, index, isSender ? 3 : 4));
    std::vector<u64> found;

    {
    Quiet quiet;
    if (proto == 'r')
    {
        if (isSender) { EcdhPsiSender s; s.sendInput(size, size, 40, prng.get<block>(), A, chls, 2); }
        else { EcdhPsiReceiver r; r.sendInput(size, size, 40, prng.get<block>(), B, chls, 2); found = r.mIntersection; }
    }
    else if (proto == 'e')
    {
        if (isSender) { JL10PsiSender s; s.startPsi_ristretoo(size, size, 40, prng.get<block>(), A, chls); }
        else { JL10PsiReceiver r; r.startPsi_ristretoo(size, size, 40, prng.get<block>(), B, chls); found = r.mIntersection; }
    }
    else
    {
        if (isSender) { MiniSender_Ris s; s.outputBigPoly_elligator(size, size, 40, prng, A, chls); }
        else { MiniReceiver_Ris r; r.outputBigPoly_elligator(size, size, 40, prng, B, chls); found = r.mIntersection; }
    }
    }

    for (auto& chl : chls)
        chl.close();
    ep.stop();
    ios.stop();

    if (isSender)
    {
        std::cout << "RESULT " << index << " " << size << " " << peakMemoryKb() << std::endl;
        return 0;
    }

    std::sort(found.begin(), found.end());
    bool ok = found.size() == common;
    for (u64 j = 0; ok && j < common; ++j)
        ok = found[j] == j;
    u64 tp = 0;                                        // distinct found items that are really shared
    for (u64 j = 0; j < found.size(); ++j)
        if (found[j] < common && (j == 0 || found[j] != found[j - 1]))
            ++tp;

    std::cout << "RESULT " << index << " " << size << " " << peakMemoryKb() << " "
              << common << " " << found.size() << " " << (ok ? 1 : 0) << " " << tp << std::endl;
    return ok ? 0 : 2;
}

int main(int argc, char** argv)
{
    try
    {
        return run(argc, argv);
    }
    catch (const std::system_error& e)                  // e.g. a thread could not be created
    {
        std::cerr << "psi_session: out of system resources: " << e.what() << std::endl;
        return 75;
    }
    catch (const std::bad_alloc&)
    {
        std::cerr << "psi_session: out of memory" << std::endl;
        return 75;
    }
}
