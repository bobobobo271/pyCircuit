"""Build a Verilator runner from the same final artifact's managed RTL."""

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--build", type=Path, required=True)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    build = args.build.resolve()
    generated = build / "verilog"
    output = args.output.resolve() if args.output else build / "verilator"
    output.mkdir(exist_ok=False)
    metadata = json.loads((generated / "generated.json").read_text())
    ports = json.loads((generated / "ports.json").read_text())
    inputs = {p["name"]: p for p in ports["inputs"]}
    outputs = {p["name"]: p for p in ports["outputs"]}
    assert inputs["host"]["width"] == 588 and outputs["result"]["width"] == 405
    assert metadata["rtl_mode"] == "managed"
    domains = {name: ports["inputs"][index]["wire"] for name, index in ports["domains"].items()}
    connections = [
        (inputs["host"]["wire"], "host"),
        (outputs["result"]["wire"], "result"),
        (domains["clock"], "clk"),
        (domains["reset"], "rst"),
        ("pyc_phase", "phase"),
        ("pyc_root_commit_ok", "commit"),
        ("pyc_check_complete", "checked"),
        ("pyc_sampling_error", "sampling_error"),
        ("pyc_local_error", "local_error"),
    ]
    available = outputs["result"].get("availability")
    if available:
        connections.append((available, "available"))
    wrapper = output / "coarse_rtl.sv"
    wrapper.write_text(
        "module coarse_rtl(input logic [587:0] host,input logic clk,rst,input logic [2:0] phase,input logic commit,output wire [404:0] result,output wire checked,sampling_error,local_error,available);\n"
        + metadata["root_rtl_name"]
        + " dut("
        + ",".join("." + k + "(" + v + ")" for k, v in connections)
        + ");\n"
        + ("" if available else "assign available=1'b1;\n")
        + "endmodule\n"
    )
    rtl = [generated / r["path"] for r in metadata["files"] if r["role"] == "rtl"]
    rtl.sort(key=lambda p: (p.name != "design_top.sv", str(p)))
    sdk = Path(os.environ["PYC_TOOLCHAIN_ROOT"]).resolve()
    primitives = sorted((sdk / "include/verilog").glob("*.v"))
    source = Path(__file__).resolve().parents[1] / "models/coarse"
    cxx = os.environ.get("CXX", "c++")
    command = [
        "verilator",
        "--cc",
        "--exe",
        "--assert",
        "--threads",
        "1",
        "--build",
        "-j",
        "2",
        "-MAKEFLAGS",
        f"CXX={cxx} LINK={cxx} OPT_FAST=-O1 OPT_SLOW=-O1 OPT_GLOBAL=-O1",
        "-Wno-fatal",
        "--top-module",
        "coarse_rtl",
        "--Mdir",
        str(output / "obj"),
        "-CFLAGS",
        f"-std=c++20 -O1 -DCOARSE_VERILATOR -I{source}",
        str(wrapper),
        *map(str, rtl),
        *map(str, primitives),
        str(source / "driver.cpp"),
        "-o",
        "coarse_verilator",
    ]
    source_paths = [source / "driver.cpp", source / "rtl_backend.hpp", Path(__file__).resolve()]
    source_hashes = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths
    }
    version = subprocess.check_output(["verilator", "--version"], text=True).strip()
    cxx_version = subprocess.check_output([cxx, "--version"], text=True).splitlines()[0]
    started = time.monotonic()
    with (output / "build.log").open("w") as log:
        r = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    receipt = {
        "command": command,
        "sources": source_hashes,
        "verilator": version,
        "cxx": cxx_version,
        "source_stable": all(
            hashlib.sha256(path.read_bytes()).hexdigest() == source_hashes[str(path)]
            for path in source_paths
        ),
        "exit_code": r.returncode,
        "seconds": time.monotonic() - started,
        "final_sha256": hashlib.sha256((build / "final.ac").read_bytes()).hexdigest(),
        "runner": str(output / "obj/coarse_verilator"),
    }
    if r.returncode == 0:
        receipt["runner_sha256"] = hashlib.sha256(
            (output / "obj/coarse_verilator").read_bytes()
        ).hexdigest()
    (output / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if r.returncode or not receipt["source_stable"]:
        raise RuntimeError(str(output / "build.log"))


if __name__ == "__main__":
    main()
