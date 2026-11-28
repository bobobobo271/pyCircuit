#include "wse_parallel/model.hpp"
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>

int main(int argc, char **argv) {
  try {
    if (argc < 3 || argc > 5)
      throw std::invalid_argument("usage: wse_parallel_runner input.txt "
                                  "workers [reference|batch] [no-trace]");
    std::ifstream input(argv[1]);
    wse_parallel::Config config;
    unsigned count = 0;
    if (!(input >> config.rows >> config.cols >> config.hop_ticks >> count) ||
        count > 40)
      throw std::invalid_argument("invalid header or more than 40 flows");
    std::vector<wse_parallel::Flow> flows;
    for (unsigned i = 0; i < count; ++i) {
      wse_parallel::Flow flow;
      unsigned targets = 0, edges = 0;
      if (!(input >> flow.id >> flow.source >> flow.nbytes >> targets >>
            edges) ||
          targets > 40 || edges > 39)
        throw std::invalid_argument("invalid flow header");
      for (unsigned j = 0, node; j < targets; ++j) {
        if (!(input >> node))
          throw std::invalid_argument("missing target");
        flow.targets.push_back(node);
      }
      for (unsigned j = 0, a, b; j < edges; ++j) {
        if (!(input >> a >> b))
          throw std::invalid_argument("missing edge");
        flow.edges.push_back(std::make_pair(a, b));
      }
      flows.push_back(flow);
    }
    std::string trailing;
    if (input >> trailing)
      throw std::invalid_argument("unexpected trailing input");
    const std::string mode = argc >= 4 ? argv[3] : "batch";
    if (mode != "batch" && mode != "reference")
      throw std::invalid_argument("invalid mode");
    if (argc == 5 && std::string(argv[4]) != "no-trace")
      throw std::invalid_argument("invalid trace option");
    config.record_events = argc < 5;
    const auto result = wse_parallel::simulate(
        config, flows, std::stoul(argv[2]), mode == "reference");
    std::cout << std::setprecision(17)
              << "{\"model_time\":" << result.model_time
              << ",\"read_bytes\":" << result.read_bytes
              << ",\"write_bytes\":" << result.write_bytes
              << ",\"link_bytes\":" << result.link_bytes
              << ",\"execution_steps\":" << result.execution_steps
              << ",\"max_batch\":" << result.max_batch
              << ",\"batches\":" << result.batches
              << ",\"multi_event_batches\":" << result.multi_event_batches
              << ",\"run_seconds\":" << result.run_seconds
              << ",\"build_seconds\":" << result.build_seconds
              << ",\"events\":[";
    bool first = true;
    for (const auto &e : result.events) {
      if (!first)
        std::cout << ',';
      first = false;
      std::cout << "{\"kind\":" << e.kind << ",\"noc_cycle\":" << e.time
                << ",\"seq\":" << e.seq << ",\"id\":" << e.id
                << ",\"source\":" << e.source << ",\"node\":" << e.node
                << ",\"flit\":" << e.flit << ",\"direction\":" << e.direction
                << ",\"bytes\":" << e.bytes << '}';
    }
    std::cout << "]}\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
