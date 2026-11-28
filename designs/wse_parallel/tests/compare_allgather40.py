"""Single-thread, matched-O1 comparison of row/column 40-rank AllGather.

Native runs the two phases sequentially as separate processes; coarse runs
one complete program including barriers, waits and retirement. Zero fixed and
endpoint costs, open Calendar. Exact combined event parity is required first.
"""

import argparse
import hashlib
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

from verify import check, flow, write_input


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def allgather40(nbytes):
    phases = []
    for phase in range(2):
        flows = []
        for source in range(40):
            row, column = divmod(source, 8)
            group = (
                [row * 8 + c for c in range(8)]
                if phase == 0
                else [r * 8 + column for r in range(5)]
            )
            rank = group.index(source)
            edges = [
                (group[i + 1], group[i]) if i < rank else (group[i], group[i + 1])
                for i in range(len(group) - 1)
            ]
            flows.append(
                flow(
                    phase * 40 + source,
                    source,
                    edges,
                    group,
                    nbytes * (8 if phase else 1),
                )
            )
        phases.append(flows)
    return phases


def cache(path):
    return {
        line.split(":", 1)[0]: line.split("=", 1)[1]
        for line in path.read_text().splitlines()
        if line and not line.startswith(("#", "//")) and ":" in line and "=" in line
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--native", type=Path, required=True)
    p.add_argument("--coarse", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--bytes-per-rank", type=int, default=128)
    p.add_argument("--warmups", type=int, default=2)
    p.add_argument("--samples", type=int, default=7)
    args = p.parse_args()
    if args.bytes_per_rank <= 0 or args.warmups < 0 or args.samples < 1:
        p.error("positive byte count and samples required")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    executables = {
        "WSE_parallel": args.native.resolve(),
        "WSE_coarse": args.coarse.resolve(),
    }
    hashes = {key: digest(value) for key, value in executables.items()}
    receipt = {
        "status": "INCOMPLETE",
        "conditions": {
            "nodes": 40,
            "rows": 5,
            "cols": 8,
            "bytes_per_rank": args.bytes_per_rank,
            "algorithm": "row AllGather (8 ranks), then column AllGather (5 ranks, 8x payload)",
            "profile": "open Calendar; zero endpoint/tx/rx/guard/barrier; hop 1; useful/wire flit 52/64 B",
            "threads": "native workers=0 (main-thread serial); coarse pyc_dut{1}",
            "warmups": args.warmups,
            "samples": args.samples,
            "trace_during_timing": False,
            "order": "alternating, sequential",
            "optimization": "-O1 -DNDEBUG",
            "platform": platform.platform(),
            "logical_cpus": os.cpu_count(),
            "primary_interval": "native sum of two Step loops; coarse full-program execution loop",
            "secondary_interval": "native sum of two child-process wall times; coarse one full-program process",
            "control_boundary": "native host waits for phase 0 drain before phase 1; coarse in-model WaitGroup/RetireGroup/Barrier",
        },
        "executables": {
            name: {"path": str(path), "sha256": hashes[name]}
            for name, path in executables.items()
        },
        "samples": [],
    }

    def save():
        (out / "benchmark.json").write_text(json.dumps(receipt, indent=2) + "\n")

    def run(name, command, folder):
        folder.mkdir(parents=True, exist_ok=True)
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.perf_counter()
        process = subprocess.run(
            list(map(str, command)), text=True, capture_output=True
        )
        elapsed = time.perf_counter() - started
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        (folder / "stdout.json").write_text(process.stdout)
        (folder / "stderr.txt").write_text(process.stderr)
        row = {
            "implementation": name,
            "command": list(map(str, command)),
            "returncode": process.returncode,
            "process_seconds": elapsed,
            "process_cpu_seconds": after.ru_utime
            + after.ru_stime
            - before.ru_utime
            - before.ru_stime,
        }
        (folder / "invocation.json").write_text(json.dumps(row, indent=2) + "\n")
        assert process.returncode == 0, row
        return row, process.stdout

    save()
    try:
        build_settings = {}
        for name, exe in executables.items():
            file = exe.parent / "CMakeCache.txt"
            settings = cache(file)
            assert settings["CMAKE_CXX_FLAGS_RELEASE"] == "-O1 -DNDEBUG"
            assert settings["CMAKE_BUILD_TYPE"] == "Release"
            build_settings[name] = {
                key: settings.get(key)
                for key in (
                    "CMAKE_CXX_COMPILER",
                    "CMAKE_OSX_SYSROOT",
                    "CMAKE_CXX_FLAGS",
                    "CMAKE_CXX_FLAGS_RELEASE",
                    "CMAKE_BUILD_TYPE",
                )
            }
            receipt["executables"][name]["cmake_cache_sha256"] = digest(file)
        assert build_settings["WSE_parallel"] == build_settings["WSE_coarse"]
        receipt["matched_build_settings"] = build_settings["WSE_parallel"]
        old_build = args.coarse.resolve().parent.parent / "build.json"
        old_identity = json.loads(old_build.read_text())
        assert (
            old_identity["status"] == "PASS"
            and old_identity["runner_sha256"] == hashes["WSE_coarse"]
        )
        receipt["coarse_existing_build_receipt"] = {
            "path": str(old_build),
            "sha256": digest(old_build),
        }
        phases = allgather40(args.bytes_per_rank)
        (out / "flows.json").write_text(json.dumps(phases, indent=2) + "\n")
        inputs = [out / f"phase-{phase}.txt" for phase in range(2)]
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "wse_model/src"))
        from wse_model.programs.coarse import (
            Barrier,
            Profile,
            RetireGroup,
            Route,
            Transfer,
            WaitGroup,
            Workload,
        )

        routes, commands = [], []
        for phase, flows in enumerate(phases):
            write_input(inputs[phase], 5, 8, 1, flows)
            commands.append(Barrier())
            for f in flows:
                routes.append(
                    Route(f["id"], f["source"], tuple(f["edges"]), tuple(f["targets"]))
                )
                commands.append(
                    Transfer(
                        f["id"],
                        f["source"],
                        f["id"],
                        f["nbytes"],
                        dependency_group=0 if phase else None,
                        completion_group=phase,
                    )
                )
            commands.extend((WaitGroup(phase), RetireGroup(phase)))
        loader = Workload(routes, commands, Profile.open()).write(out / "coarse-input")
        input_hashes = {str(path): digest(path) for path in (*inputs, loader)}
        receipt["input_sha256"] = input_hashes
        phase_controls, combined_events = [], []
        offset = 0
        keys = ("model_time", "read_bytes", "write_bytes", "link_bytes")
        for phase, source in enumerate(inputs):
            _, raw = run(
                "WSE_parallel",
                [args.native, source, "0", "batch"],
                out / f"parity/native-{phase}",
            )
            result = json.loads(raw)
            check(result, phases[phase], 8, {})
            phase_controls.append({key: result[key] for key in keys})
            combined_events.extend(
                {
                    key: (value + offset if key == "noc_cycle" else value)
                    for key, value in e.items()
                    if key != "seq"
                }
                for e in result["events"]
            )
            offset += result["model_time"]
        expected = {key: sum(item[key] for item in phase_controls) for key in keys}
        parity = out / "parity/coarse"
        print("native phases verified; checking full coarse program", flush=True)
        run(
            "WSE_coarse",
            [args.coarse, loader, parity / "result.json", parity / "events.jsonl"],
            parity,
        )
        coarse = json.loads((parity / "result.json").read_text())
        assert coarse["fault"] == 0 and {key: coarse[key] for key in keys} == expected
        old_events = [
            json.loads(line)
            for line in (parity / "events.jsonl").read_text().splitlines()
        ]
        barriers = [e["noc_cycle"] for e in old_events if e["kind"] == 10]
        assert barriers == [0, phase_controls[0]["model_time"]]
        shared_events = [e for e in old_events if e["kind"] != 10]
        assert combined_events == shared_events, next(
            (
                (i, a, b)
                for i, (a, b) in enumerate(
                    zip(combined_events, shared_events, strict=False)
                )
                if a != b
            ),
            (len(combined_events), len(shared_events)),
        )
        receipt["correctness"] = {
            "status": "PASS",
            "exact_matching_events": len(shared_events),
            "coarse_barriers": barriers,
            "phase_results": phase_controls,
            "model_result": expected,
            "final_completion_count": 40 * 5,
            "bytes_per_destination": 40 * args.bytes_per_rank,
            "native_driver": "two sequential phase invocations, offset second-phase timestamps by first-phase end",
        }
        save()
        print(f"exact parity PASS: {len(shared_events)} events; {expected}", flush=True)
        for iteration in range(args.warmups + args.samples):
            order = list(executables)
            if iteration % 2:
                order.reverse()
            for name in order:
                assert digest(executables[name]) == hashes[name]
                assert all(
                    digest(Path(path)) == value for path, value in input_hashes.items()
                )
                folder = out / "runs" / f"{iteration:02d}-{name}"
                if name == "WSE_parallel":
                    parts = []
                    for phase, source in enumerate(inputs):
                        part, raw = run(
                            name,
                            [executables[name], source, "0", "batch", "no-trace"],
                            folder / str(phase),
                        )
                        data = json.loads(raw)
                        assert {key: data[key] for key in keys} == phase_controls[phase]
                        part.update(result=data, execution_seconds=data["run_seconds"])
                        parts.append(part)
                    row = {
                        key: sum(part[key] for part in parts)
                        for key in (
                            "process_seconds",
                            "process_cpu_seconds",
                            "execution_seconds",
                        )
                    }
                    row.update(implementation=name, phases=parts)
                else:
                    row, _ = run(
                        name,
                        [executables[name], loader, folder / "result.json"],
                        folder,
                    )
                    data = json.loads((folder / "result.json").read_text())
                    assert data["fault"] == 0 and data["events"] == len(old_events)
                    assert {key: data[key] for key in keys} == expected
                    row.update(result=data, execution_seconds=data["execution_seconds"])
                row.update(iteration=iteration, warmup=iteration < args.warmups)
                receipt["samples"].append(row)
                save()
                print(
                    iteration,
                    name,
                    "warmup" if row["warmup"] else "sample",
                    row["execution_seconds"],
                    flush=True,
                )
        summary = {}
        for name in executables:
            samples = [
                row
                for row in receipt["samples"]
                if row["implementation"] == name and not row["warmup"]
            ]
            summary[name] = {}
            for field in (
                "execution_seconds",
                "process_seconds",
                "process_cpu_seconds",
            ):
                values = [row[field] for row in samples]
                summary[name][field] = {
                    "raw": values,
                    "median": statistics.median(values),
                    "mean": statistics.mean(values),
                    "min": min(values),
                    "max": max(values),
                }
        receipt.update(
            status="PASS",
            summary=summary,
            execution_speedup=summary["WSE_coarse"]["execution_seconds"]["median"]
            / summary["WSE_parallel"]["execution_seconds"]["median"],
            process_speedup=summary["WSE_coarse"]["process_seconds"]["median"]
            / summary["WSE_parallel"]["process_seconds"]["median"],
        )
    except Exception as error:
        receipt.update(status="FAIL", error=repr(error))
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
