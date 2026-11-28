#ifndef COARSE_VERILATOR
#include "pycircuit_system.hpp"
#endif
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <vector>
using Clock = std::chrono::steady_clock;
#ifndef COARSE_VERILATOR
template <unsigned W> auto known(uint64_t v) {
  return gfsim::wire<gfsim::Bits<W>>::known(gfsim::Bits<W>{v});
}
#endif
struct Snapshot {
  uint64_t time, events, kind, source, node, id, flit, direction, bytes, reads,
      writes, links, pc, stage, fault, done;
};
#ifndef COARSE_VERILATOR
struct Sim {
  uint64_t permits = (uint64_t(1) << 40) - 1;
  pyc_dut dut{1};
  pyc_dut::Inputs inputs{};
  uint64_t cycles = 0;
  void drive(const std::array<uint8_t, 68> &r, unsigned clk) {
    gfsim::Bits<588> b{};
    auto put = [&](unsigned o, unsigned w, uint64_t v) {
      for (unsigned i = 0; i < w; ++i)
        if ((v >> i) & 1)
          b = gfsim::detail::setBit(b, o + i);
    };
    put(0, 40, permits);
    put(40, 16, uint64_t(r[4]) | (uint64_t(r[5]) << 8));
    for (unsigned i = 0; i < 64; ++i)
      put(56 + i * 8, 8, r[4 + i]);
    put(568, 16, uint64_t(r[2]) | (uint64_t(r[3]) << 8));
    put(584, 4, r[0]);
    inputs.host =
        decltype(inputs.host)::fromPacked(gfsim::FourState<588>::known(b));
    inputs.pyc_7079635f636c6b = known<1>(clk);
    inputs.pyc_7079635f727374 = known<1>(0);
    dut.drive(inputs);
  }
  Sim() {
    drive({}, 0);
    dut.system().Build();
    dut.system().Reset();
  }
  Snapshot cycle(const std::array<uint8_t, 68> &r = {}) {
    for (unsigned clk = 0; clk < 2; ++clk) {
      drive(r, clk);
      if (dut.system().Step() != gfsim::SimStepResult::Running) {
        auto e = dut.system().failureInfo();
        throw std::runtime_error(std::string(e.code) + " " +
                                 std::string(e.message));
      }
    }
    ++cycles;
    auto b = dut.sample().result.packed();
    auto get = [&](unsigned o, unsigned w) {
      uint64_t v = 0;
      for (unsigned i = 0; i < w; ++i) {
        if (!b.knownMask().bit(o + i) || b.zMask().bit(o + i))
          throw std::runtime_error("unknown output");
        v |= uint64_t(b.value().bit(o + i)) << i;
      }
      return v;
    };
    return {get(341, 64), get(309, 32), get(301, 8),  get(295, 6),
            get(289, 6),  get(273, 16), get(257, 16), get(254, 3),
            get(222, 32), get(158, 64), get(94, 64),  get(30, 64),
            get(14, 16),  get(9, 5),    get(1, 8),    get(0, 1)};
  }
};
#else
#include "rtl_backend.hpp"
#endif
int main(int argc, char **argv) {
  try {
    if (argc < 3 || argc > 4)
      throw std::runtime_error(
          "usage: coarse_runner loader.bin receipt.json [events.jsonl]");
    auto begin = Clock::now();
    std::ifstream input(argv[1], std::ios::binary);
    if (!input)
      throw std::runtime_error("input open");
    Sim sim;
    auto initialized = Clock::now();
    Snapshot s{};
    std::array<uint8_t, 68> r{};
    bool started = false;
    while (input.read(reinterpret_cast<char *>(r.data()), 68)) {
      if (started)
        throw std::runtime_error("records after START");
      s = sim.cycle(r);
      if (s.fault)
        throw std::runtime_error("loader fault");
      started = r[0] == 5;
    }
    if (input.gcount() != 0 || !started)
      throw std::runtime_error("truncated loader or missing START");
    auto envnum = [](const char *key, uint64_t fallback) {
      const char *v = std::getenv(key);
      return v ? std::stoull(v) : fallback;
    };
    const auto stall_node = envnum("WSE_STALL_NODE", 40),
               stall_at = envnum("WSE_STALL_AT", 0),
               stall_until = envnum("WSE_STALL_UNTIL", 0);
    auto loaded = Clock::now();
    auto start_cycles = sim.cycles;
    uint64_t last = 0;
    std::ofstream trace;
    if (argc == 4)
      trace.open(argv[3]);
    while (!s.done && !s.fault && sim.cycles - start_cycles < 100000000) {
      sim.permits = (uint64_t(1) << 40) - 1;
      if (stall_node < 40 && s.time >= stall_at && s.time < stall_until)
        sim.permits &= ~(uint64_t(1) << stall_node);
      s = sim.cycle();
      if (s.events != last) {
        last = s.events;
        if (trace)
          trace << "{\"kind\":" << s.kind << ",\"noc_cycle\":" << s.time
                << ",\"source\":" << s.source << ",\"node\":" << s.node
                << ",\"id\":" << s.id << ",\"flit\":" << s.flit
                << ",\"direction\":" << s.direction << ",\"bytes\":" << s.bytes
                << "}\n";
      }
    }
    auto finished = Clock::now();
    if (!s.done && !s.fault)
      throw std::runtime_error("execution bound");
    std::ofstream out(argv[2]);
    out << std::setprecision(17) << "{\"backend\":\"" <<
#ifdef COARSE_VERILATOR
        "verilator"
#else
        "gfsim"
#endif
        << "\",\"status\":\"" << (s.fault ? "FAULT" : "EXECUTED")
        << "\",\"fault\":" << s.fault << ",\"model_time\":" << s.time
        << ",\"events\":" << s.events << ",\"read_bytes\":" << s.reads
        << ",\"write_bytes\":" << s.writes << ",\"link_bytes\":" << s.links
        << ",\"execution_cycles\":" << sim.cycles - start_cycles
        << ",\"initialize_seconds\":"
        << std::chrono::duration<double>(initialized - begin).count()
        << ",\"load_seconds\":"
        << std::chrono::duration<double>(loaded - initialized).count()
        << ",\"execution_seconds\":"
        << std::chrono::duration<double>(finished - loaded).count() << "}\n";
    return s.fault ? 2 : 0;
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
