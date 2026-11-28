#include "Vcoarse_rtl.h"
struct Sim {
  uint64_t permits = (uint64_t(1) << 40) - 1;
  Vcoarse_rtl dut;
  uint64_t cycles = 0;
  void drive(const std::array<uint8_t, 68> &r, unsigned clk) {
    for (unsigned i = 0; i < 19; ++i)
      dut.host[i] = 0;
    auto put = [&](unsigned o, unsigned w, uint64_t v) {
      for (unsigned i = 0; i < w; ++i)
        if ((v >> i) & 1)
          dut.host[(o + i) / 32] |= uint32_t(1) << ((o + i) % 32);
    };
    put(0, 40, permits);
    put(40, 16, uint64_t(r[4]) | (uint64_t(r[5]) << 8));
    for (unsigned i = 0; i < 64; ++i)
      put(56 + i * 8, 8, r[i + 4]);
    put(568, 16, uint64_t(r[2]) | (uint64_t(r[3]) << 8));
    put(584, 4, r[0]);
    dut.clk = clk;
  }
  Sim() {
    drive({}, 0);
    dut.rst = 1;
    dut.phase = 0;
    dut.commit = 0;
    dut.eval();
    dut.phase = 4;
    dut.eval();
    dut.commit = 1;
    dut.phase = 2;
    dut.eval();
    dut.phase = 0;
    dut.commit = 0;
    dut.rst = 0;
    dut.eval();
  }
  Snapshot cycle(const std::array<uint8_t, 68> &r = {}) {
    std::array<uint32_t, 13> b{};
    for (unsigned clk = 0; clk < 2; ++clk) {
      drive(r, clk);
      dut.eval();
      dut.commit = 0;
      dut.phase = 1;
      dut.eval();
      if (!dut.checked || dut.sampling_error || dut.local_error ||
          !dut.available)
        throw std::runtime_error("managed RTL check failed");
      for (unsigned i = 0; i < 13; ++i)
        b[i] = dut.result[i];
      dut.commit = 1;
      dut.phase = 2;
      dut.eval();
      dut.phase = 0;
      dut.commit = 0;
      dut.eval();
    }
    ++cycles;
    auto get = [&](unsigned o, unsigned w) {
      uint64_t v = 0;
      for (unsigned i = 0; i < w; ++i)
        v |= uint64_t((b[(o + i) / 32] >> ((o + i) % 32)) & 1) << i;
      return v;
    };
    return {get(341, 64), get(309, 32), get(301, 8),  get(295, 6),
            get(289, 6),  get(273, 16), get(257, 16), get(254, 3),
            get(222, 32), get(158, 64), get(94, 64),  get(30, 64),
            get(14, 16),  get(9, 5),    get(1, 8),    get(0, 1)};
  }
};
