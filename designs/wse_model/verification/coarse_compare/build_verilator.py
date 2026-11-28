"""Build original WSE host driver against managed RTL from the same final.ac."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def adapt_driver(source: str, ports: dict, root: str) -> str:
    inputs = {row["name"]: row for row in ports["inputs"]}
    result = next(row for row in ports["outputs"] if row["name"] == "result")
    host = inputs["host"]["wire"]
    clock = ports["inputs"][ports["domains"]["clock"]]["wire"]
    reset = ports["inputs"][ports["domains"]["reset"]]["wire"]
    available = result.get("availability")
    availability = f" && dut.{available}" if available else ""
    sample_start = source.index("  Snapshot sample() {")
    decode_start = source.index("    Snapshot out;", sample_start)
    if "auto state_get" in source[sample_start:decode_start]:
        decode_start = source.index("    auto state_get", sample_start)
    cycle_start = source.index("  Snapshot cycle(", decode_start)
    simulation_end = source.index("\n};", cycle_start) + len("\n};")
    decoding = source[decode_start:cycle_start]
    cycling = source[cycle_start:simulation_end]
    wrapper = (
        f"""struct Simulation {{
  VerilatedContext context;
  V{root} dut{{&context}};
  std::array<uint32_t, {(result['width'] + 31) // 32}> sampled{{}};
  std::uint64_t cycles = 0, steps = 0;
  std::ofstream trace;
  unsigned last_event = 0;
  Simulation() {{
    for (auto &word : dut.{host}.m_storage) word = 0;
    dut.{clock} = 0; dut.{reset} = 1;
    dut.pyc_root_commit_ok = 0; dut.pyc_phase = 0; dut.eval();
    dut.pyc_phase = 4; dut.eval();
    dut.pyc_root_commit_ok = 1; dut.pyc_phase = 2; dut.eval();
    dut.pyc_root_commit_ok = 0; dut.pyc_phase = 0; dut.{reset} = 0; dut.eval();
  }}
  ~Simulation() {{ dut.final(); }}
  void drive(const Request &request, unsigned clock) {{
    for (auto &word : dut.{host}.m_storage) word = 0;
    auto put = [&](unsigned offset, unsigned width, std::uint64_t value) {{
      for (unsigned bit = 0; bit < width; ++bit)
        if ((value >> bit) & 1) dut.{host}[(offset + bit) / 32] |= uint32_t{{1}} << ((offset + bit) % 32);
    }};
    put(0, 40, request.permits); put(40, 16, request.count);
    for (unsigned byte = 0; byte < (request.wide ? kBeat : kAlignment); ++byte)
      put(56 + 8 * byte, 8, request.data[byte]);
    put(568, 1, request.wide); put(569, 32, request.address);
    put(601, 6, request.node); put(607, 3, request.kind);
    dut.{clock} = clock; dut.eval();
  }}
  void step() {{
    ++steps;
    dut.pyc_phase = 1; dut.eval();
    require(dut.pyc_check_complete && !dut.pyc_sampling_error && !dut.pyc_local_error{availability},
            "RTL Work/availability check failed");
    std::copy_n(&dut.{result['wire']}[0], sampled.size(), sampled.begin());
    dut.pyc_root_commit_ok = 1; dut.pyc_phase = 2; dut.eval();
    dut.pyc_phase = 0; dut.pyc_root_commit_ok = 0; dut.eval();
  }}
  Snapshot sample() {{
    auto get = [&](unsigned offset, unsigned width) {{
      std::uint64_t value = 0;
      for (unsigned bit = 0; bit < width; ++bit)
        value |= std::uint64_t((sampled[(offset + bit) / 32] >> ((offset + bit) % 32)) & 1u) << bit;
      return value;
    }};
"""
        + decoding
        + cycling
    )
    result_source = (
        source[: source.index("struct Simulation {")] + wrapper + source[simulation_end:]
    )
    result_source = result_source.replace(
        '#include "pycircuit_system.hpp"', f'#include "V{root}.h"'
    )
    begin = result_source.index("template <unsigned W>")
    end = result_source.index("static std::vector", begin)
    result_source = result_source[:begin] + result_source[end:]
    return result_source.replace('\\"backend\\":\\"cpp\\"', '\\"backend\\":\\"verilator\\"')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--driver", type=Path, required=True)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    generated = args.build.resolve() / "verilog"
    metadata = json.loads((generated / "generated.json").read_text())
    if metadata.get("rtl_mode") != "managed":
        raise ValueError("comparison requires managed RTL lifecycle")
    ports = json.loads((generated / "ports.json").read_text())
    root = metadata["root_rtl_name"]
    driver = output / "driver.cpp"
    driver.write_text(adapt_driver(args.driver.read_text(), ports, root))
    rtl = [generated / row["path"] for row in metadata["files"] if row["role"] == "rtl"]
    receipt = {
        "status": "INCOMPLETE",
        "stages": [],
        "conditions": {"optimization": "O1", "workers": 1},
    }
    tracked = [
        args.build / "final.ac",
        args.driver,
        driver,
        generated / "ports.json",
        *rtl,
        Path(__file__),
    ]
    receipt["sources"] = {str(path.resolve()): digest(path) for path in tracked}

    def save() -> None:
        (output / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")

    try:
        commands = [
            [
                "verilator",
                "--cc",
                "--exe",
                "--top-module",
                root,
                "--Mdir",
                output / "obj",
                "--threads",
                "1",
                "--assert",
                "-Wno-fatal",
                "-CFLAGS",
                "-std=c++20",
                "-y",
                args.toolchain / "include/verilog",
                *rtl,
                driver,
            ],
            [
                "make",
                "-C",
                output / "obj",
                "-f",
                f"V{root}.mk",
                "-j1",
                "OPT_FAST=-O1",
                "OPT_SLOW=-O1",
                "OPT_GLOBAL=-O1",
                "CXX=" + os.environ["CXX"],
                "LINK=" + os.environ["CXX"],
                f"V{root}",
            ],
        ]
        for name, command in zip(("verilate", "build"), commands, strict=True):
            row = {"name": name, "command": list(map(str, command))}
            receipt["stages"].append(row)
            save()
            started = time.monotonic()
            with (output / f"{name}.log").open("w") as log:
                completed = subprocess.run(
                    row["command"], stdout=log, stderr=subprocess.STDOUT, timeout=3600
                )
            row.update(returncode=completed.returncode, seconds=time.monotonic() - started)
            save()
            completed.check_returncode()
        runner = output / "obj" / f"V{root}"
        receipt.update(status="PASS", runner=str(runner), runner_sha256=digest(runner))
    except Exception as error:
        receipt.update(status="FAIL", error=str(error))
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
