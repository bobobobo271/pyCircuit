#include "wse_parallel/model.hpp"
#include "SimModule.h"
#include <algorithm>
#include <array>
#include <chrono>
#include <limits>
#include <queue>
#include <set>
#include <stdexcept>
#include <string>

namespace wse_parallel {
namespace {
using Tick = std::uint64_t;
Tick later(Tick t, Tick delta) {
  if (delta > std::numeric_limits<Tick>::max() - t)
    throw std::overflow_error("model time overflow");
  return t + delta;
}
unsigned direction(unsigned a, unsigned b, const Config &c) {
  if (a >= c.cols && b == a - c.cols)
    return 0;
  if (a % c.cols && b == a - 1)
    return 1;
  if (a % c.cols + 1 < c.cols && b == a + 1)
    return 2;
  if (b == a + c.cols)
    return 3;
  throw std::invalid_argument("tree edge must connect mesh neighbors");
}
struct Plan {
  Flow flow;
  std::vector<std::array<int, 4>> peers;
  std::vector<bool> targets;
};
std::vector<Plan> validate(const Config &c, const std::vector<Flow> &flows) {
  if (!c.rows || !c.cols || Tick(c.rows) * c.cols > 40 || !c.hop_ticks)
    throw std::invalid_argument(
        "require 1..40 mesh nodes and positive hop_ticks");
  const unsigned n = c.rows * c.cols;
  std::set<unsigned> ids, sources;
  std::vector<Plan> plans;
  for (const auto &f : flows) {
    if (f.source >= n || !f.nbytes || f.targets.empty() ||
        !ids.insert(f.id).second || !sources.insert(f.source).second)
      throw std::invalid_argument(
          "flow requires unique id/source, bytes and targets");
    Plan p{f, std::vector<std::array<int, 4>>(n), std::vector<bool>(n)};
    for (auto &ports : p.peers)
      ports.fill(-1);
    for (auto t : f.targets) {
      if (t >= n || p.targets[t])
        throw std::invalid_argument("invalid target");
      p.targets[t] = true;
    }
    std::vector<unsigned> indegree(n);
    for (const auto &edge : f.edges) {
      unsigned a = edge.first, b = edge.second;
      if (a >= n || b >= n)
        throw std::invalid_argument("edge outside mesh");
      unsigned d = direction(a, b, c);
      if (p.peers[a][d] >= 0 || ++indegree[b] > 1 || b == f.source)
        throw std::invalid_argument("edges must form a directed tree");
      p.peers[a][d] = int(b);
    }
    std::vector<bool> reached(n);
    std::vector<unsigned> todo{f.source};
    while (!todo.empty()) {
      unsigned a = todo.back();
      todo.pop_back();
      if (reached[a])
        throw std::invalid_argument("cyclic tree");
      reached[a] = true;
      for (int b : p.peers[a])
        if (b >= 0)
          todo.push_back(unsigned(b));
    }
    for (unsigned a = 0; a < n; ++a) {
      if ((p.targets[a] || indegree[a]) && !reached[a])
        throw std::invalid_argument("disconnected tree or unreachable target");
      for (int b : p.peers[a])
        if (b >= 0 && !reached[a])
          throw std::invalid_argument("disconnected tree");
    }
    plans.push_back(std::move(p));
  }
  std::sort(plans.begin(), plans.end(), [](const Plan &a, const Plan &b) {
    return a.flow.source < b.flow.source;
  });
  return plans;
}
struct SourceState {
  Tick outstanding = 0;
  unsigned roots = 0;
  std::vector<bool> remaining;
  bool done = false;
};
struct NodeState {
  Tick ejection = 0, write = 0;
  std::array<Tick, 4> link{};
  std::vector<Tick> received;
};
struct Request {
  Event event;
  unsigned plan = 0;
  SourceState source;
};
struct Reply {
  Event event;
  unsigned plan = 0;
  SourceState source;
  std::vector<Event> generated;
  Tick read = 0, write = 0, link = 0;
};
class Node final : public gfsim::SimModule {
  gfsim::SimQueue<Request> *input_;
  gfsim::SimQueue<Reply> *output_;
  const Config &config_;
  const std::vector<Plan> &plans_;
  NodeState state_, next_;
  bool active_ = false;

public:
  Node(unsigned index, gfsim::SimQueue<Request> *in,
       gfsim::SimQueue<Reply> *out, const Config &c,
       const std::vector<Plan> &plans)
      : SimModule("node_" + std::to_string(index)), input_(BindInput(in)),
        output_(BindOutput(out)), config_(c), plans_(plans) {
    state_.received.resize(plans.size());
    next_ = state_;
  }
  void Build() override { SetObjectCapacity(0); }
  void Work() override {
    active_ = false;
    if (input_->Empty() || output_->Full())
      return;
    const auto request = input_->Front();
    auto r = std::make_shared<Reply>();
    r->event = request->event;
    r->plan = request->plan;
    r->source = request->source;
    next_ = state_;
    active_ = true;
    auto &e = r->event;
    auto &s = r->source;
    const auto &p = plans_[r->plan];
    const Tick flits = (p.flow.nbytes - 1) / 52 + 1;
    auto schedule = [&](unsigned kind, Tick time, unsigned node, Tick flit = 0,
                        unsigned d = 0) {
      r->generated.push_back(
          Event{kind, time, 0, e.id, e.source, node, flit, d, 0});
      if (kind == 2 || kind == 3 || kind == 4 || kind == 9)
        ++s.outstanding;
    };
    if (e.kind == 2 || e.kind == 3 || e.kind == 4 || e.kind == 9) {
      if (!s.outstanding)
        throw std::logic_error("outstanding underflow");
      --s.outstanding;
    }
    if (e.kind == 1) {
      e.bytes = r->read = p.flow.nbytes;
      bool edges = false;
      for (int peer : p.peers[e.node])
        edges |= peer >= 0;
      if (edges)
        schedule(2, e.time, e.node);
      if (p.targets[e.node]) {
        next_.write = std::max(next_.write, e.time);
        schedule(5, next_.write, e.node);
      }
    } else if (e.kind == 2 || e.kind == 4) {
      unsigned mask = 0;
      for (unsigned d = 0; d < 4; ++d)
        if (p.peers[e.node][d] >= 0) {
          mask |= 1u << d;
          schedule(3, e.time, e.node, e.flit, d);
        }
      if (e.kind == 2)
        s.roots = mask;
      else if (p.targets[e.node])
        schedule(9, e.time, e.node, e.flit);
    } else if (e.kind == 3) {
      Tick ready = std::max(e.time, next_.link[e.direction]);
      if (ready != e.time) {
        schedule(3, ready, e.node, e.flit, e.direction);
        e.kind = 11;
      } else {
        next_.link[e.direction] = later(e.time, 1);
        e.bytes = r->link = 64;
        if (e.node == e.source) {
          s.roots &= ~(1u << e.direction);
          if (!s.roots && e.flit + 1 < flits)
            schedule(2, later(e.time, 1), e.node, e.flit + 1);
        }
        schedule(4, later(e.time, config_.hop_ticks),
                 unsigned(p.peers[e.node][e.direction]), e.flit);
      }
    } else if (e.kind == 9) {
      if (next_.ejection > e.time) {
        schedule(9, next_.ejection, e.node, e.flit);
        e.kind = 12;
      } else {
        next_.ejection = later(e.time, 1);
        e.bytes = e.flit + 1 < flits ? 52 : p.flow.nbytes - e.flit * 52;
        if (++next_.received[r->plan] == flits) {
          next_.write = std::max(next_.write, e.time);
          schedule(5, next_.write, e.node);
        }
      }
    } else if (e.kind == 5) {
      e.bytes = r->write = p.flow.nbytes;
      s.remaining[e.node] = false;
      if (std::none_of(s.remaining.begin(), s.remaining.end(),
                       [](bool b) { return b; }))
        schedule(6, e.time, e.source);
    } else if (e.kind == 6) {
      if (s.outstanding)
        schedule(6, later(e.time, 1), e.source);
      else
        s.done = true;
    } else
      throw std::logic_error("unsupported event");
    output_->Write(std::move(r));
    input_->Pop();
    GetSim()->MarkProgress();
  }
  void Xfer() override {
    if (active_)
      state_ = next_;
  }
  void Reset() override {
    state_ = NodeState{};
    state_.received.resize(plans_.size());
    next_ = state_;
    active_ = false;
  }
  void ReportStat() override {}
};
struct Later {
  bool operator()(const Event &a, const Event &b) const {
    return a.time != b.time ? a.time > b.time : a.seq > b.seq;
  }
};
class Root final : public gfsim::SimModule {
  const Config config_;
  const std::vector<Plan> plans_;
  const bool reference_;
  std::vector<SourceState> sources_;
  std::vector<gfsim::SimQueue<Request> *> requests_;
  std::vector<gfsim::SimQueue<Reply> *> replies_;
  std::priority_queue<Event, std::vector<Event>, Later> heap_;
  std::vector<Event> batch_;
  std::vector<unsigned> by_source_;
  Tick sequence_ = 0;

public:
  Result result;
  Root(gfsim::SimSystem *sim, Config c, std::vector<Plan> p, bool reference)
      : SimModule("wse_parallel", sim), config_(c), plans_(std::move(p)),
        reference_(reference), sources_(plans_.size()),
        by_source_(c.rows * c.cols) {
    for (unsigned i = 0; i < plans_.size(); ++i)
      by_source_[plans_[i].flow.source] = i;
  }
  void push(Event e) {
    e.seq = sequence_++;
    heap_.push(e);
  }
  void Build() override {
    unsigned n = config_.rows * config_.cols;
    SetObjectCapacity(n * 3);
    for (unsigned i = 0; i < n; ++i) {
      auto *in = AddInnerQueue(std::make_shared<gfsim::SimQueue<Request>>(
          "dispatch_" + std::to_string(i)));
      auto *out = AddInnerQueue(std::make_shared<gfsim::SimQueue<Reply>>(
          "result_" + std::to_string(i)));
      in->InitMaxSize(1);
      out->InitMaxSize(1);
      requests_.push_back(BindOutput(in));
      replies_.push_back(BindInput(out));
      AddSubmodule(std::make_shared<Node>(i, in, out, config_, plans_));
    }
    BuildInnerQueues();
    BuildChildren();
  }
  void Work() override {
    // Root owns the heap and source state. Children receive value snapshots.
    // Unique source and node per batch make all proposals independent.
    if (!batch_.empty()) {
      bool ready = true;
      for (const auto &e : batch_)
        ready &= !replies_[e.node]->Empty();
      if (ready) {
        for (const auto &e : batch_) {
          const auto r = replies_[e.node]->Front();
          sources_[r->plan] = r->source;
          if (config_.record_events)
            result.events.push_back(r->event);
          result.model_time = r->event.time;
          result.read_bytes += r->read;
          result.write_bytes += r->write;
          result.link_bytes += r->link;
          for (auto generated : r->generated)
            push(generated);
          replies_[e.node]->Pop();
          GetSim()->MarkProgress();
        }
        batch_.clear();
      }
    } else if (!heap_.empty()) {
      std::set<unsigned> sources, nodes;
      Tick time = heap_.top().time;
      while (!heap_.empty() && heap_.top().time == time) {
        const Event e = heap_.top();
        if (requests_[e.node]->Full())
          break;
        bool boundary = e.kind == 5 || e.kind == 6;
        if (!batch_.empty() && (reference_ || boundary ||
                                sources.count(e.source) || nodes.count(e.node)))
          break;
        heap_.pop();
        batch_.push_back(e);
        sources.insert(e.source);
        nodes.insert(e.node);
        if (boundary || reference_)
          break;
      }
      if (batch_.empty()) {
        WorkChildren();
        return;
      }
      ++result.batches;
      result.max_batch = std::max<Tick>(result.max_batch, batch_.size());
      if (batch_.size() > 1)
        ++result.multi_event_batches;
      for (const auto &e : batch_) {
        unsigned p = by_source_[e.source];
        auto request = std::make_shared<Request>();
        request->event = e;
        request->plan = p;
        request->source = sources_[p];
        requests_[e.node]->Write(std::move(request));
        GetSim()->MarkProgress();
      }
    }
    WorkChildren();
  }
  void Xfer() override {
    AdvanceInnerQueues();
    XferChildren();
  }
  void Reset() override {
    ResetInnerQueues();
    ResetChildren();
    result = Result{};
    heap_ = decltype(heap_){};
    batch_.clear();
    sequence_ = 0;
    sources_.assign(plans_.size(), SourceState{});
    for (unsigned i = 0; i < plans_.size(); ++i) {
      const auto &p = plans_[i];
      sources_[i].remaining = p.targets;
      push(Event{1, 0, 0, p.flow.id, p.flow.source, p.flow.source, 0, 0, 0});
    }
  }
  void ReportStat() override {}
  bool done() const { return heap_.empty() && batch_.empty(); }
  void check_complete() const {
    for (const auto &s : sources_)
      if (!s.done || s.outstanding)
        throw std::logic_error("incomplete flow at empty event heap");
  }
};
} // namespace
Result simulate(const Config &config, const std::vector<Flow> &flows,
                unsigned workers, bool reference_serial) {
  auto plans = validate(config, flows);
  const auto build_start = std::chrono::steady_clock::now();
  gfsim::SimSystem system;
  auto root = std::make_shared<Root>(&system, config, std::move(plans),
                                     reference_serial);
  system.AddModule(root);
  if (workers) {
    system.SetWorkerCount(workers);
    system.SetParallelChildren(root.get());
  }
  system.Build();
  const auto run_start = std::chrono::steady_clock::now();
  root->result.build_seconds =
      std::chrono::duration<double>(run_start - build_start).count();
  while (!root->done())
    system.Step();
  root->result.run_seconds = std::chrono::duration<double>(
                                 std::chrono::steady_clock::now() - run_start)
                                 .count();
  root->check_complete();
  root->result.execution_steps = system.Cycles();
  return std::move(root->result);
}
} // namespace wse_parallel
