"""Build sampled, nested timers for the actual coarse generated C++ artifact.

This creates an isolated derivative of generated source and one runtime header;
the original model, generated artifact, SDK and executable remain unchanged.
It builds only. Run separately with COARSE_PROFILE_OUTPUT=/path/prefix and
COARSE_PROFILE_PERIOD=127 (0 disables all probes), optionally OFFSET=0..126.
COARSE_PROFILE_DETAIL=0 omits fine work-part timers for low-overhead phase
attribution; the default detailed mode locates generated-code hotspots.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


PROFILER = r"""#pragma once
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <fstream>
namespace costprof {
using Clock = std::chrono::steady_clock;
inline bool execution = false, selected = false;
inline std::uint64_t ordinal = 0;
inline bool detailed = !std::getenv("COARSE_PROFILE_DETAIL") || std::strtoul(std::getenv("COARSE_PROFILE_DETAIL"), nullptr, 10) != 0;
inline unsigned period = std::getenv("COARSE_PROFILE_PERIOD") ? std::strtoul(std::getenv("COARSE_PROFILE_PERIOD"), nullptr, 10) : 127;
inline unsigned offset = std::getenv("COARSE_PROFILE_OFFSET") ? std::strtoul(std::getenv("COARSE_PROFILE_OFFSET"), nullptr, 10) : 0;
struct Stat { std::uint64_t calls=0, inclusive=0, exclusive=0; };
inline Stat stats[512], categories[64];
inline std::uint64_t category_cycles[64]={};
inline const char* names[512] = {__NAMES__};
struct Scope;
inline Scope* top = nullptr;
struct Scope {
  unsigned id=0; bool enabled=false; Scope* parent=nullptr;
  Clock::time_point begin; std::uint64_t children=0;
  Scope() = default;
  explicit Scope(unsigned i) { start(i); }
  void start(unsigned i) {
    id=i; enabled=selected;
    if(enabled) { parent=top; top=this; children=0; begin=Clock::now(); }
  }
  std::uint64_t stop() {
    if(!enabled) return 0;
    const auto elapsed=std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-begin).count();
    auto& s=stats[id]; ++s.calls; s.inclusive+=elapsed; s.exclusive+=elapsed-children;
    top=parent; if(parent) parent->children+=elapsed;
    enabled=false; return elapsed;
  }
  ~Scope() { stop(); }
};
struct PartScope : Scope { explicit PartScope(unsigned i) { if(detailed) start(i); } };
struct Cycle {
  unsigned category; Scope scope;
  explicit Cycle(unsigned c):category(c) {
    if(!execution || !period) return;
    ++category_cycles[c]; selected=(ordinal++ % period)==(offset % period);
    scope.start(0);
  }
  ~Cycle() {
    if(scope.enabled) { const auto ns=scope.stop(); ++categories[category].calls; categories[category].inclusive+=ns; }
    selected=false;
  }
};
inline void dump() {
  const char* prefix=std::getenv("COARSE_PROFILE_OUTPUT");
  if(!prefix) return;
  std::ofstream f(std::string(prefix)+".functions.csv");
  f << "id,name,calls,inclusive_ns,exclusive_ns\n";
  for(unsigned i=0;i<512;++i) if(stats[i].calls)
    f << i << ',' << names[i] << ',' << stats[i].calls << ',' << stats[i].inclusive << ',' << stats[i].exclusive << '\n';
  std::ofstream c(std::string(prefix)+".categories.csv");
  c << "category,cycles,sampled_cycles,sampled_ns\n";
  for(unsigned i=0;i<64;++i) if(category_cycles[i])
    c << i << ',' << category_cycles[i] << ',' << categories[i].calls << ',' << categories[i].inclusive << '\n';
  std::ofstream meta(std::string(prefix)+".meta.json");
  constexpr unsigned n=100000;
  const auto start=Clock::now();
  for(unsigned i=0;i<n;++i) { auto a=Clock::now(); auto b=Clock::now(); (void)a; (void)b; }
  auto ns=std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-start).count();
  meta << "{\"period\":" << period << ",\"offset\":" << offset << ",\"execution_cycles\":" << ordinal << ",\"detailed\":" << (detailed ? "true" : "false")
       << ",\"clock_pair_calibration_ns\":" << double(ns)/n << ",\"method\":\"periodic complete driver-cycle sample; each driver cycle contains two GFSIM Steps; timer overhead is retained\"}\n";
}
}
"""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError((old, source.count(old)))
    return source.replace(old, new, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--coarse-build", type=Path, required=True)
    parser.add_argument("--coarse-checkout", type=Path, required=True)
    parser.add_argument("--compiler", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    src = args.coarse_build.resolve()
    checkout = args.coarse_checkout.resolve()
    sdk = checkout / ".coarse-sdk"
    shutil.copytree(src / "cpp", out / "cpp")
    original_hashes = {
        str(p): digest(p) for p in (src / "cpp").rglob("*") if p.is_file()
    }
    labels = []
    mapping = []

    def scope(name, path=None, line=None, detail=False):
        index = len(labels)
        labels.append(name)
        mapping.append(
            {
                "id": index,
                "name": name,
                "source": str(path) if path else None,
                "line": line,
            }
        )
        kind = "PartScope" if detail else "Scope"
        return f"costprof::{kind} pyc_cost_scope({index});"

    scope("driver.cycle")
    for stem in ("machine", "event_queue"):
        p = out / f"cpp/sources/wse_coarse/{stem}.hpp"
        source = p.read_text()
        original = src / f"cpp/sources/wse_coarse/{stem}.hpp"
        pattern = r"((?:static )?pyc_work_noinline void (pyc_work_(?:part|frame_init)_\d+)\(pyc_work_frame &pyc_values\) \{)"

        def part(match):
            line = source.count("\n", 0, match.start()) + 1
            return (
                match[1]
                + " "
                + scope(stem + "." + match[2], original, line, detail=True)
            )

        transformed = re.sub(pattern, part, source)
        for signature, name in (
            ("void Work() {", "family.Work"),
            ("void Xfer() noexcept {", "family.Xfer"),
            ("void Work() override {", "scalar.Work"),
            ("void Xfer() noexcept override {", "scalar.Xfer"),
        ):
            transformed = replace_once(
                transformed,
                signature,
                signature
                + " "
                + scope(
                    stem + "." + name,
                    original,
                    source[: source.index(signature)].count("\n") + 1,
                ),
            )
        if stem == "machine":
            method = r"""
  unsigned profile_category() const {
    auto low=[](const auto& v,unsigned bits) { unsigned value=0; for(unsigned i=0;i<bits;++i) value|=unsigned(v.packed().value().bit(i))<<i; return value; };
    const auto& m=*pyc_implementation;
    const auto phase=low(m.pyc_instance_queue->pyc_instance_phase_q.element(0),3);
    if(phase) return 16+phase;
    if(low(m.pyc_instance_pending_read_q.element(0),4)<low(m.pyc_instance_pending_count_q.element(0),4)) return 32;
    return low(m.pyc_instance_stage_q.element(0),5);
  }
"""
            transformed = replace_once(
                transformed,
                "  CoarseWseMachine(const CoarseWseMachine &) = delete;",
                method + "  CoarseWseMachine(const CoarseWseMachine &) = delete;",
            )
        p.write_text('#include "cost_profile.hpp"\n' + transformed)
    p = out / "cpp/pycircuit_system.hpp"
    source = p.read_text()
    source = replace_once(
        source,
        "bool Precheck() noexcept override {",
        "bool Precheck() noexcept override { "
        + scope("system.Precheck", src / "cpp/pycircuit_system.hpp"),
    )
    source = replace_once(
        source,
        "  gfsim::SimSystem &system()",
        "  unsigned profile_category() const { return root_->profile_category(); }\n  gfsim::SimSystem &system()",
    )
    p.write_text(source)
    runtime = sdk / "include/gfsim/SimSystem.h"
    original_hashes[str(runtime)] = digest(runtime)
    target = out / "cpp/gfsim/SimSystem.h"
    target.parent.mkdir()
    source = replace_once(
        runtime.read_text(),
        "  SimStepResult Step() {",
        "  SimStepResult Step() { " + scope("system.Step", runtime),
    )
    target.write_text('#include "cost_profile.hpp"\n' + source)
    driver = checkout / "designs/wse_model/models/coarse/driver.cpp"
    original_hashes[str(driver)] = digest(driver)
    source = driver.read_text()
    source = replace_once(
        source,
        "  Snapshot cycle(const std::array<uint8_t, 68> &r = {}) {",
        "  Snapshot cycle(const std::array<uint8_t, 68> &r = {}) {\n    costprof::Cycle pyc_cost_cycle(costprof::execution && costprof::period ? dut.profile_category() : 0);",
    )
    source = replace_once(
        source,
        "  void drive(const std::array<uint8_t, 68> &r, unsigned clk) {",
        "  void drive(const std::array<uint8_t, 68> &r, unsigned clk) { "
        + scope("driver.drive", driver),
    )
    source = replace_once(
        source,
        "    while (!s.done",
        "    costprof::execution=true;\n    while (!s.done",
    )
    source = replace_once(
        source,
        "    auto finished = Clock::now();",
        "    auto finished = Clock::now();\n    costprof::execution=false; costprof::dump();",
    )
    (out / "driver.cpp").write_text(source)
    (out / "cpp/cost_profile.hpp").write_text(
        PROFILER.replace("__NAMES__", ",".join(json.dumps(x) for x in labels))
    )
    assert len(labels) < 512
    (out / "mapping.json").write_text(
        json.dumps(
            {
                "functions": mapping,
                "categories": {
                    "1": "ingest",
                    "3": "reserve",
                    "8": "scan_sources including start_pop",
                    "9": "process_event",
                    "10": "extract",
                    "11": "dispatch_source",
                    "12": "retire",
                    "17": "heap push bubble",
                    "18": "heap pop root",
                    "19": "heap load tail",
                    "20": "heap sift down",
                    "32": "pending queue push",
                },
            },
            indent=2,
        )
        + "\n"
    )
    command = [
        str(args.compiler),
        "-O1",
        "-DNDEBUG",
        "-std=gnu++20",
        "-isysroot",
        str(args.sysroot),
        "-mmacosx-version-min=26.0",
        "-I" + str(out / "cpp"),
        "-isystem",
        str(sdk / "include"),
        str(out / "driver.cpp"),
        str(sdk / "lib/libpyc6_runtime.a"),
        "-fuse-ld=lld",
        "-o",
        str(out / "runner"),
    ]
    receipt = {
        "command": command,
        "original_source_sha256": original_hashes,
        "function_count": len(labels),
        "status": "INCOMPLETE",
    }
    with (out / "build.log").open("w") as log:
        completed = subprocess.run(command, stdout=log, stderr=log)
    receipt["returncode"] = completed.returncode
    receipt["original_sources_unchanged"] = all(
        digest(Path(path)) == value for path, value in original_hashes.items()
    )
    if completed.returncode == 0:
        receipt["runner_sha256"] = digest(out / "runner")
    receipt["status"] = (
        "PASS"
        if completed.returncode == 0 and receipt["original_sources_unchanged"]
        else "FAIL"
    )
    (out / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if receipt["status"] != "PASS":
        raise RuntimeError(str(out / "build.log"))
    print(out / "runner", flush=True)


if __name__ == "__main__":
    main()
