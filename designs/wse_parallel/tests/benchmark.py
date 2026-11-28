"""Initial native-model scaling experiment, with trace disabled.

Each sample starts a fresh process/system. Timed Run reuses workers across all
Steps, but workers are rebuilt between samples. This is not a reset/reuse
steady-state benchmark. Build and process wall time are reported separately.
"""

import argparse
import hashlib
import json
import platform
import os
import resource
from pathlib import Path
import statistics
import subprocess
import time

from verify import cases, write_input


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    receipt = dict(
        status="INCOMPLETE",
        platform=platform.platform(),
        logical_cpus=os.cpu_count(),
        runner_sha256=hashlib.sha256(args.runner.read_bytes()).hexdigest(),
        warmup_per_configuration=2,
        measured_per_configuration=7,
        timing="Step loop only; new system/process per sample",
        samples=[],
    )
    try:
        for name, rows, cols, hop, flows, expected in cases("mesh40")[:3]:
            source = args.output / (name + ".txt")
            write_input(source, rows, cols, hop, flows)
            control = None
            for batch in range(9):
                workers = [0, 1, 2, 4, 8]
                workers = workers[batch % 5 :] + workers[: batch % 5]
                for worker in workers:
                    before_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
                    started = time.perf_counter()
                    p = subprocess.run(
                        [
                            str(args.runner),
                            str(source),
                            str(worker),
                            "batch",
                            "no-trace",
                        ],
                        check=True,
                        text=True,
                        capture_output=True,
                    )
                    elapsed = time.perf_counter() - started
                    after_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
                    result = json.loads(p.stdout)
                    meaning = {
                        k: result[k]
                        for k in (
                            "model_time",
                            "read_bytes",
                            "write_bytes",
                            "link_bytes",
                            "batches",
                            "max_batch",
                        )
                    }
                    if control is None:
                        control = meaning
                    assert meaning == control
                    receipt["samples"].append(
                        dict(
                            case=name,
                            batch=batch,
                            warmup=batch < 2,
                            workers=worker,
                            process_seconds=elapsed,
                            process_cpu_seconds=(
                                after_cpu.ru_utime
                                + after_cpu.ru_stime
                                - before_cpu.ru_utime
                                - before_cpu.ru_stime
                            ),
                            result=result,
                        )
                    )
            print(name, "sampled", flush=True)
        summary = []
        for name in sorted({s["case"] for s in receipt["samples"]}):
            baseline = statistics.median(
                s["result"]["run_seconds"]
                for s in receipt["samples"]
                if s["case"] == name and not s["warmup"] and s["workers"] == 0
            )
            for worker in (0, 1, 2, 4, 8):
                values = [
                    s["result"]["run_seconds"]
                    for s in receipt["samples"]
                    if s["case"] == name and not s["warmup"] and s["workers"] == worker
                ]
                median = statistics.median(values)
                summary.append(
                    dict(
                        case=name,
                        workers=worker,
                        median_seconds=median,
                        min_seconds=min(values),
                        max_seconds=max(values),
                        speedup=baseline / median,
                    )
                )
        receipt.update(status="PASS", summary=summary)
    except Exception as error:
        receipt.update(status="FAIL", error=repr(error))
        raise
    finally:
        (args.output / "benchmark.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )


if __name__ == "__main__":
    main()
