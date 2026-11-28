#pragma once
#include <cstdint>
#include <utility>
#include <vector>

namespace wse_parallel {
struct Flow {
  unsigned id = 0, source = 0;
  std::uint64_t nbytes = 0;
  std::vector<unsigned> targets;
  std::vector<std::pair<unsigned, unsigned>> edges;
  Flow() = default;
  Flow(unsigned identity, unsigned origin, std::uint64_t bytes,
       std::vector<unsigned> destinations,
       std::vector<std::pair<unsigned, unsigned>> tree)
      : id(identity), source(origin), nbytes(bytes),
        targets(std::move(destinations)), edges(std::move(tree)) {}
};
struct Config {
  unsigned rows = 5, cols = 8;
  std::uint64_t hop_ticks = 1;
  bool record_events = true;
  Config() = default;
  Config(unsigned r, unsigned c, std::uint64_t hop, bool record = true)
      : rows(r), cols(c), hop_ticks(hop), record_events(record) {}
};
struct Event {
  unsigned kind = 0;
  std::uint64_t time = 0, seq = 0;
  unsigned id = 0, source = 0, node = 0;
  std::uint64_t flit = 0;
  unsigned direction = 0;
  std::uint64_t bytes = 0;
  Event() = default;
  Event(unsigned k, std::uint64_t t, std::uint64_t sequence, unsigned identity,
        unsigned origin, unsigned at, std::uint64_t fragment, unsigned port,
        std::uint64_t size)
      : kind(k), time(t), seq(sequence), id(identity), source(origin), node(at),
        flit(fragment), direction(port), bytes(size) {}
};
struct Result {
  double build_seconds = 0, run_seconds = 0;
  std::vector<Event> events;
  std::uint64_t model_time = 0, read_bytes = 0, write_bytes = 0, link_bytes = 0;
  std::uint64_t execution_steps = 0, max_batch = 0, batches = 0,
                multi_event_batches = 0;
};
// Static, initially dispatched flows; open Calendar; zero endpoint/guard
// latency. Queue capacity is dynamic: coarse's 1024-entry fault is outside this
// prototype.
Result simulate(const Config &, const std::vector<Flow> &, unsigned workers,
                bool reference_serial = false);
} // namespace wse_parallel
