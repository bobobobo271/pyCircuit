"""Run AllGather profiling and Verilator timing sequentially, after all builds.

Input and correctness baselines come from the completed two-phase comparison.
Profiling measurements are kept separate from uninstrumented timing samples.
"""

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("native", "coarse", "verilator"))
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--coarse-detail",
        type=int,
        choices=(0, 1),
        default=1,
        help="0: one low-overhead coarse phase profile; 1: detailed profiles and control",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    baseline = root / "docs/gates/logs/wse-parallel-20261128/allgather40-two-phase-o1"
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    expected = json.loads((baseline / "parity/coarse/result.json").read_text())
    exe = args.runner.resolve()
    identity = hashlib.sha256(exe.read_bytes()).hexdigest()
    receipt = {
        "status": "INCOMPLETE",
        "kind": args.kind,
        "runner": str(exe),
        "runner_sha256": identity,
        "runs": [],
    }
    meaning = ("model_time", "read_bytes", "write_bytes", "link_bytes")

    def save():
        (out / "runs.json").write_text(json.dumps(receipt, indent=2) + "\n")

    def invoke(command, folder, env):
        folder.mkdir(parents=True, exist_ok=True)
        assert hashlib.sha256(exe.read_bytes()).hexdigest() == identity
        start = time.perf_counter()
        process = subprocess.run(
            list(map(str, command)),
            env={**os.environ, **env},
            capture_output=True,
            text=True,
        )
        wall = time.perf_counter() - start
        (folder / "stdout.json").write_text(process.stdout)
        (folder / "stderr.txt").write_text(process.stderr)
        row = {
            "command": list(map(str, command)),
            "environment": env,
            "returncode": process.returncode,
            "process_seconds": wall,
        }
        receipt["runs"].append(row)
        save()
        assert process.returncode == 0, row
        return row, process.stdout

    save()
    try:
        if args.kind == "native":
            # Separate full-trace parity from no-trace timing/profile runs.
            for phase in range(2):
                control = json.loads(
                    (baseline / f"parity/native-{phase}/stdout.json").read_text()
                )
                source = baseline / f"phase-{phase}.txt"
                row, raw = invoke(
                    [exe, source, "0", "batch"],
                    out / f"trace-{phase}",
                    {
                        "WSE_PROFILE_PERIOD": "64",
                        "WSE_PROFILE_OUTPUT": str(out / f"trace-{phase}.profile.json"),
                    },
                )
                result = json.loads(raw)
                assert all(
                    result[key] == control[key]
                    for key in (*meaning, "execution_steps", "events")
                )
                row.update(
                    status="PASS",
                    phase=phase,
                    mode="trace-parity",
                    result={key: result[key] for key in (*meaning, "run_seconds")},
                )
                save()
            for repeat in range(3):
                for period, offset in ((0, 0), (64, 0), (64, 17), (127, 31)):
                    for phase in range(2):
                        folder = out / f"r{repeat}-p{period}-o{offset}-phase{phase}"
                        env = {
                            "WSE_PROFILE_PERIOD": str(period),
                            "WSE_PROFILE_OFFSET": str(offset),
                            "WSE_PROFILE_OUTPUT": str(folder / "profile.json"),
                        }
                        row, raw = invoke(
                            [
                                exe,
                                baseline / f"phase-{phase}.txt",
                                "0",
                                "batch",
                                "no-trace",
                            ],
                            folder,
                            env,
                        )
                        result = json.loads(raw)
                        control = json.loads(
                            (
                                baseline / f"parity/native-{phase}/stdout.json"
                            ).read_text()
                        )
                        assert all(
                            result[key] == control[key]
                            for key in (
                                *meaning,
                                "execution_steps",
                                "batches",
                                "max_batch",
                            )
                        )
                        profile = json.loads((folder / "profile.json").read_text())
                        assert profile["useful_events"] == len(control["events"])
                        assert profile["steps"] == result["execution_steps"]
                        assert (
                            sum(item["exclusive_ns"] for item in profile["categories"])
                            == profile["categories"][0]["inclusive_ns"]
                        )
                        row.update(
                            status="PASS",
                            repeat=repeat,
                            period=period,
                            offset=offset,
                            phase=phase,
                            result=result,
                            profile=str(folder / "profile.json"),
                        )
                        save()
                print("native repeat", repeat, "PASS", flush=True)
        else:
            configs = (
                [(0, 0, False), (127, 0, True), (127, 31, False)]
                if args.kind == "coarse"
                else [(0, 0, False)] * 9
            )
            if args.kind == "coarse" and not args.coarse_detail:
                configs = [(127, 0, False)]
            for index, (period, offset, trace) in enumerate(configs):
                folder = out / f"run-{index}"
                env = (
                    {
                        "COARSE_PROFILE_PERIOD": str(period),
                        "COARSE_PROFILE_OFFSET": str(offset),
                        "COARSE_PROFILE_OUTPUT": str(folder / "profile"),
                    }
                    if args.kind == "coarse"
                    else {}
                )
                if args.kind == "coarse":
                    env["COARSE_PROFILE_DETAIL"] = str(args.coarse_detail)
                command = [
                    exe,
                    baseline / "coarse-input/loader.bin",
                    folder / "result.json",
                ]
                if trace:
                    command.append(folder / "events.jsonl")
                row, _ = invoke(command, folder, env)
                result = json.loads((folder / "result.json").read_text())
                assert all(
                    result[key] == expected[key]
                    for key in (*meaning, "fault", "events", "execution_cycles")
                )
                if trace:
                    assert (folder / "events.jsonl").read_bytes() == (
                        baseline / "parity/coarse/events.jsonl"
                    ).read_bytes()
                row.update(
                    status="PASS",
                    index=index,
                    period=period,
                    offset=offset,
                    trace=trace,
                    warmup=args.kind == "verilator" and index < 2,
                    result=result,
                )
                save()
                print(args.kind, index, "PASS", result["execution_seconds"], flush=True)
        receipt["status"] = "PASS"
    except Exception as error:
        receipt.update(status="FAIL", error=repr(error))
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
