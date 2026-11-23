// SPDX-License-Identifier: BSD-3-Clause
#pragma once
#include <gfsim/dff.h>
#include <cstdint>
#include <vector>

// Backend for the emitted WSE table registers. Each Q pin has one driver.
// Clocks and error checks still advance on every lane. Retain the existing
// data when the edge does not write, or the write reproduces all three planes.
template<class Kernel> class wse_sparse_registers {
  using Wire = decltype(typename Kernel::Current{}.q);
  struct Lane {
    bool clock = false, next_clock = false, visited = false;
    bool listed = false, write = false;
  };
  std::vector<Lane> lanes_;
  std::vector<Wire> next_;
  std::vector<std::size_t> writes_;
  std::size_t visited_ = 0;

  static bool equal(const Wire &left, const Wire &right) {
    const auto &a = left.packed();
    const auto &b = right.packed();
    return a.value() == b.value() && a.knownMask() == b.knownMask() &&
           a.zMask() == b.zMask();
  }

  void stage(std::size_t i, const Wire &wire) {
    auto &lane = lanes_[i];
    if (!lane.listed) {
      writes_.push_back(i);
      lane.listed = true;
    }
    lane.write = true;
    next_[i] = wire;
  }

public:
  explicit wse_sparse_registers(std::size_t size) : lanes_(size), next_(size) {
    if (!size) throw std::invalid_argument("empty WSE register bank");
    writes_.reserve(size);
  }

  void work(std::size_t i, typename Kernel::Inputs in, typename Kernel::Outputs out) {
    try {
      auto &lane = lanes_.at(i);
      if (!lane.visited) {
        lane.visited = true;
        ++visited_;
      }
      const auto edge = gfsim::evaluateSampledClock(lane.clock, in.clk, in.rst);
      // Use the stock kernel for its exact exception and reset/enable rules
      // whenever a control is unknown. Fault-free data writes use the same
      // borrowed D and init pins as dffe_kernel.
      if (edge.fault != gfsim::SampledClockFault::None ||
          (edge.rising && !edge.resetActive && !in.en.isFullyKnown())) {
        typename Kernel::Current current{out.q, lane.clock};
        typename Kernel::Pending next;
        Kernel::work(current, in, next, out);
      }
      lane.next_clock = edge.nextClock;
      lane.write = false;
      if (edge.rising && (edge.resetActive || in.en.value().toBool())) {
        const auto &value = edge.resetActive ? in.init : in.d;
        if (!equal(value, out.q)) stage(i, value);
      }
    } catch (...) {
      discard();
      throw;
    }
  }

  template<class OutputAccessor> void xfer(OutputAccessor outputs) noexcept {
    if (visited_ == lanes_.size()) {
      for (auto i : writes_) {
        if (lanes_[i].write) {
          outputs(i).q = next_[i];
        }
      }
      for (auto &lane : lanes_) lane.clock = lane.next_clock;
    }
    discard();
  }

  void discard() noexcept {
    for (auto &lane : lanes_) {
      lane.visited = false;
      lane.listed = false;
      lane.write = false;
    }
    writes_.clear();
    visited_ = 0;
  }

  template<class InputAccessor> void reset(InputAccessor inputs) noexcept {
    discard();
    for (std::size_t i = 0; i < lanes_.size(); ++i) {
      lanes_[i].next_clock = false;
      lanes_[i].visited = true;
      stage(i, inputs(i).init);
    }
    visited_ = lanes_.size();
  }
};
