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
    std::uint64_t visited_in = 0;
    bool listed = false, write = false;
  };
  std::vector<Lane> lanes_;
  // Work samples each lane's independent clock. A complete batch publishes
  // all sampled clocks with a buffer swap, including edges without a write.
  std::vector<std::uint8_t> clocks_, next_clocks_;
  std::uint64_t batch_ = 1;
  std::vector<Wire> next_;
  std::vector<std::size_t> writes_;
  std::vector<std::size_t> changed_;
  std::vector<std::uint8_t> reported_;
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
  explicit wse_sparse_registers(std::size_t size) : lanes_(size), clocks_(size), next_clocks_(size), next_(size) {
    if (!size) throw std::invalid_argument("empty WSE register bank");
    writes_.reserve(size);
  }

  // One consumer per owned Q bank. Accumulate committed writes across Xfers
  // until the evaluator consumes them. Failed/discarded work never publishes.
  void take_changed_rows(std::vector<std::size_t> &rows, bool refresh_all) {
    rows.clear();
    if (refresh_all || reported_.empty()) {
      changed_.reserve(lanes_.size());
      reported_.assign(lanes_.size(), 0);
      for (std::size_t i = 0; i < lanes_.size(); ++i) rows.push_back(i);
    } else {
      rows.assign(changed_.begin(), changed_.end());
      for (auto i : changed_) reported_[i] = 0;
    }
    changed_.clear();
  }

  void work(std::size_t i, typename Kernel::Inputs in, typename Kernel::Outputs out) {
    try {
      auto &lane = lanes_.at(i);
      if (lane.visited_in != batch_) {
        lane.visited_in = batch_;
        ++visited_;
      }
      const auto edge = gfsim::evaluateSampledClock(clocks_[i], in.clk, in.rst);
      // Use the stock kernel for its exact exception and reset/enable rules
      // whenever a control is unknown. Fault-free data writes use the same
      // borrowed D and init pins as dffe_kernel.
      if (edge.fault != gfsim::SampledClockFault::None ||
          (edge.rising && !edge.resetActive && !in.en.isFullyKnown())) {
        typename Kernel::Current current{out.q, static_cast<bool>(clocks_[i])};
        typename Kernel::Pending next;
        Kernel::work(current, in, next, out);
      }
      next_clocks_[i] = edge.nextClock;
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
          if (!reported_.empty() && !reported_[i]) {
            reported_[i] = 1;
            changed_.push_back(i);
          }
        }
      }
      clocks_.swap(next_clocks_);
    }
    discard();
  }

  void discard() noexcept {
    for (auto i : writes_) lanes_[i].listed = false;
    if (++batch_ == 0) {
      for (auto &lane : lanes_) lane.visited_in = 0;
      batch_ = 1;
    }
    writes_.clear();
    visited_ = 0;
  }

  template<class InputAccessor> void reset(InputAccessor inputs) noexcept {
    discard();
    for (std::size_t i = 0; i < lanes_.size(); ++i) {
      next_clocks_[i] = false;
      lanes_[i].visited_in = batch_;
      stage(i, inputs(i).init);
    }
    visited_ = lanes_.size();
  }
};
