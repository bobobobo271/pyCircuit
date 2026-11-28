"""Build a sampled, serial-only native-model cost profiler in an isolated tree.

Production and dependency sources are copied and hashed, never edited in place.
Run the generated executable with its normal CLI. Environment controls:
WSE_PROFILE_PERIOD=64 (0 disables all timers), WSE_PROFILE_OFFSET=0,
WSE_PROFILE_OUTPUT=/absolute/path/profile.json. Only workers=0 is admitted.
No simulator workloads are run by this build tool.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


HEADER = r"""#pragma once
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <stdexcept>

namespace wse_profile {
enum Category { Step, RootControl, RootSelectDispatch, RootMerge,
                NodeTraversal, QueueAdvance, NodeXferTraversal, NodeCommit,
                HeapPush, HeapPop, EventBase, CategoryCount = EventBase + 14 };
inline const char* label(unsigned c) {
  static const char* names[] = {
    "system_step_other", "root_control_wait", "root_select_dispatch", "root_merge",
    "node_work_traversal_idle", "queue_advance", "node_xfer_traversal_idle", "node_state_commit",
    "heap_push", "heap_pop", "event_kind_0", "event_read_1", "event_inject_2",
    "event_link_3", "event_arrival_4", "event_complete_5", "event_release_6",
    "event_kind_7", "event_kind_8", "event_ejection_9", "event_kind_10",
    "event_kind_11", "event_kind_12", "event_kind_13"};
  return names[c];
}
struct Record { std::uint64_t calls=0, inclusive_ns=0, exclusive_ns=0; };
struct Scope;
struct State {
  unsigned period=64, offset=0;
  bool active=false;
  std::uint64_t steps=0, sampled_steps=0, useful_events=0, heap_pushes=0, heap_pops=0;
  std::array<std::uint64_t,14> event_counts{};
  std::array<Record,CategoryCount> records{};
  Scope* top=nullptr;
};
inline State& state() { static State value; return value; }
inline unsigned setting(const char* name, unsigned fallback) {
  const char* value=std::getenv(name);
  return value ? static_cast<unsigned>(std::stoul(value)) : fallback;
}
inline void reset() {
  state()=State{};
  state().period=setting("WSE_PROFILE_PERIOD",64);
  state().offset=setting("WSE_PROFILE_OFFSET",0);
}
inline void begin_step() {
  State& s=state();
  s.active=s.period && s.steps%s.period == s.offset%s.period;
  ++s.steps;
  if(s.active) ++s.sampled_steps;
}
inline void end_step() { state().active=false; }
struct Scope {
  typedef std::chrono::steady_clock Clock;
  unsigned category;
  bool enabled;
  Scope* parent;
  Clock::time_point start;
  std::uint64_t child_ns=0;
  explicit Scope(unsigned c): category(c),enabled(state().active),parent(nullptr) {
    if(enabled) { parent=state().top; state().top=this; start=Clock::now(); }
  }
  ~Scope() {
    if(!enabled) return;
    const auto elapsed=std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-start).count();
    const std::uint64_t ns=static_cast<std::uint64_t>(elapsed);
    Record& r=state().records[category]; ++r.calls;
    r.inclusive_ns+=ns; r.exclusive_ns+=ns-child_ns;
    state().top=parent; if(parent) parent->child_ns+=ns;
  }
};
inline void event(unsigned kind) {
  ++state().useful_events;
  if(kind<14) ++state().event_counts[kind];
}
inline void write() {
  const char* path=std::getenv("WSE_PROFILE_OUTPUT");
  if(!path) return;
  std::ofstream f(path);
  if(!f) throw std::runtime_error("cannot open WSE_PROFILE_OUTPUT");
  const State& s=state();
  f << "{\"period\":" << s.period << ",\"offset\":" << s.offset
    << ",\"steps\":" << s.steps << ",\"sampled_steps\":" << s.sampled_steps
    << ",\"useful_events\":" << s.useful_events << ",\"heap_pushes\":" << s.heap_pushes
    << ",\"heap_pops\":" << s.heap_pops << ",\"event_counts\":[";
  for(unsigned i=0;i<14;++i) { if(i)f<<',';f<<s.event_counts[i]; }
  f << "],\"categories\":[";
  for(unsigned i=0;i<CategoryCount;++i) {
    if(i)f<<',';
    const Record& r=s.records[i];
    f << "{\"name\":\"" << label(i) << "\",\"sampled_calls\":" << r.calls
      << ",\"inclusive_ns\":" << r.inclusive_ns << ",\"exclusive_ns\":" << r.exclusive_ns << '}';
  }
  f << "]}\n";
}
}
"""


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError(f"expected exactly one instrumentation site: {before!r}")
    return text.replace(before, after)


def instrument(text):
    text = '#include "native_profile.hpp"\n' + text
    text = replace_once(
        text,
        "    const auto request = input_->Front();",
        "    const auto request = input_->Front();\n    wse_profile::event(request->event.kind);\n    wse_profile::Scope node_cost(wse_profile::EventBase + request->event.kind);",
    )
    text = replace_once(
        text,
        "    if (active_)\n      state_ = next_;",
        "    if (active_) {\n      wse_profile::Scope commit_cost(wse_profile::NodeCommit);\n      state_ = next_;\n    }",
    )
    text = replace_once(
        text,
        "  void push(Event e) {",
        "  void push(Event e) {\n    ++wse_profile::state().heap_pushes;\n    wse_profile::Scope heap_cost(wse_profile::HeapPush);",
    )
    text = replace_once(
        text,
        "    // Root owns the heap",
        "    wse_profile::Scope root_cost(wse_profile::RootControl);\n    // Root owns the heap",
    )
    text = replace_once(
        text,
        "      if (ready) {",
        "      if (ready) {\n        wse_profile::Scope merge_cost(wse_profile::RootMerge);",
    )
    text = replace_once(
        text,
        "    } else if (!heap_.empty()) {",
        "    } else if (!heap_.empty()) {\n      wse_profile::Scope select_cost(wse_profile::RootSelectDispatch);",
    )
    text = replace_once(
        text,
        "        heap_.pop();",
        "        {\n          ++wse_profile::state().heap_pops;\n          wse_profile::Scope heap_cost(wse_profile::HeapPop);\n          heap_.pop();\n        }",
    )
    assert text.count("WorkChildren();") == 2
    text = text.replace(
        "WorkChildren();",
        "{ wse_profile::Scope traversal_cost(wse_profile::NodeTraversal); WorkChildren(); }",
    )
    text = replace_once(
        text,
        "    AdvanceInnerQueues();\n    XferChildren();",
        "    { wse_profile::Scope queue_cost(wse_profile::QueueAdvance); AdvanceInnerQueues(); }\n    { wse_profile::Scope traversal_cost(wse_profile::NodeXferTraversal); XferChildren(); }",
    )
    text = replace_once(
        text,
        "  auto plans = validate(config, flows);",
        '  if (workers) throw std::invalid_argument("profile build requires workers=0");\n  auto plans = validate(config, flows);',
    )
    text = replace_once(
        text,
        "  const auto run_start = std::chrono::steady_clock::now();",
        "  wse_profile::reset();\n  const auto run_start = std::chrono::steady_clock::now();",
    )
    text = replace_once(
        text,
        "  while (!root->done())\n    system.Step();",
        "  while (!root->done()) {\n    wse_profile::begin_step();\n    { wse_profile::Scope step_cost(wse_profile::Step); system.Step(); }\n    wse_profile::end_step();\n  }",
    )
    text = replace_once(
        text,
        "  root->check_complete();",
        "  wse_profile::write();\n  root->check_complete();",
    )
    return text


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    model = root / "designs/wse_parallel"
    dep = root / ".gfsim-parallel-src"
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    sources = [
        model / "src/model.cpp",
        model / "src/main.cpp",
        model / "include/wse_parallel/model.hpp",
    ]
    sources += sorted(
        p
        for folder in (dep / "src", dep / "include")
        for p in folder.rglob("*")
        if p.is_file()
    )
    original = {str(p): digest(p) for p in sources}
    shutil.copytree(dep / "src", out / "gfsim/src")
    shutil.copytree(dep / "include", out / "gfsim/include")
    shutil.copytree(model / "include", out / "include")
    shutil.copy2(model / "src/main.cpp", out / "main.cpp")
    (out / "model.cpp").write_text(instrument((model / "src/model.cpp").read_text()))
    (out / "native_profile.hpp").write_text(HEADER)
    (out / "CMakeLists.txt").write_text(
        """cmake_minimum_required(VERSION 3.20)
project(NativeCost LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
find_package(Threads REQUIRED)
file(GLOB GFSIM_SOURCES CONFIGURE_DEPENDS gfsim/src/*.cpp)
add_library(gfsim STATIC ${GFSIM_SOURCES})
target_include_directories(gfsim PUBLIC gfsim/include)
target_link_libraries(gfsim PUBLIC Threads::Threads)
add_executable(native_profile_runner model.cpp main.cpp)
target_include_directories(native_profile_runner PRIVATE include)
target_link_libraries(native_profile_runner PRIVATE gfsim)
"""
    )
    toolchains = root.parent / ".wse-toolchains"
    cmake = toolchains / "cmake-4.4.4-macos-universal/CMake.app/Contents/bin/cmake"
    cxx = toolchains / "LLVM-22.1.8-macOS-ARM64/bin/clang++"
    sdk = (
        toolchains
        / "apple-sdk/Payload/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk"
    )
    commands = [
        [
            cmake,
            "-S",
            out,
            "-B",
            out / "build",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_CXX_FLAGS_RELEASE=-O1 -DNDEBUG",
            f"-DCMAKE_CXX_COMPILER={cxx}",
            f"-DCMAKE_OSX_SYSROOT={sdk}",
        ],
        [cmake, "--build", out / "build", "-j", "4"],
    ]
    receipt = {
        "status": "INCOMPLETE",
        "original_sha256": original,
        "script_sha256": digest(Path(__file__)),
        "commands": [],
        "sample_method": "Systematic complete-Step sampling with configurable period/offset; exclusive child subtraction; timer overhead remains in measured buckets",
        "scope": "serial workers=0 only",
    }
    try:
        for index, command in enumerate(commands):
            command = list(map(str, command))
            with (out / f"build-{index}.log").open("w") as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            receipt["commands"].append(
                {"argv": command, "returncode": result.returncode}
            )
            if result.returncode:
                raise RuntimeError(f"build stage {index} failed")
        if any(digest(Path(path)) != value for path, value in original.items()):
            raise RuntimeError("original source changed during build")
        runner = out / "build/native_profile_runner"
        receipt.update(status="PASS", runner=str(runner), runner_sha256=digest(runner))
    except Exception as error:
        receipt.update(status="FAIL", error=repr(error))
        raise
    finally:
        (out / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(runner)


if __name__ == "__main__":
    main()
