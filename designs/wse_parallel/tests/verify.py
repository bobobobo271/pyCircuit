"""Independent path/byte/time checks plus serial/parallel and coarse comparisons.

All cases use 52 useful bytes per 64-byte flit, open Calendar, zero endpoint,
TX/RX/guard costs, one active transfer per source, and simultaneous release.
Only hop latency changes in the explicit latency control.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def flow(identity, source, edges, targets, nbytes=200):
    return dict(id=identity, source=source, edges=edges, targets=targets, nbytes=nbytes)


def cases(stage):
    if stage == "prototype":
        return [
            ("single-hop", 1, 2, 1, [flow(7, 0, [(0, 1)], [1])], {(7, 1): 4}),
            ("two-hop", 1, 3, 1, [flow(7, 0, [(0, 1), (1, 2)], [2])], {(7, 2): 5}),
            (
                "hop-latency-control",
                1,
                3,
                3,
                [flow(7, 0, [(0, 1), (1, 2)], [2])],
                {(7, 2): 9},
            ),
            (
                "independent",
                1,
                4,
                1,
                [flow(7, 0, [(0, 1)], [1]), flow(2, 2, [(2, 3)], [3])],
                {(7, 1): 4, (2, 3): 4},
            ),
            (
                "ejection-order",
                1,
                3,
                1,
                [flow(1, 2, [(2, 1)], [1], 52), flow(99, 0, [(0, 1)], [1], 52)],
                {(99, 1): 1, (1, 1): 2},
            ),
            (
                "shared-link",
                2,
                3,
                1,
                [flow(8, 0, [(0, 1), (1, 2)], [2]), flow(4, 4, [(4, 1), (1, 2)], [2])],
                {},
            ),
            (
                "multicast-local",
                2,
                3,
                1,
                [flow(9, 0, [(0, 1), (1, 2), (1, 4)], [0, 2, 4])],
                {(9, 0): 0, (9, 2): 5, (9, 4): 5},
            ),
            (
                "drain-after-local",
                1,
                3,
                1,
                [flow(7, 0, [(0, 1), (1, 2)], [0], 52)],
                {(7, 0): 0},
            ),
        ]
    # Forty independent source streams exercise every node. Each adjacent pair
    # exchanges in both directions, so all 40 completions have the same formula.
    exchange = [flow(100 + n, n, [(n, n ^ 1)], [n ^ 1], 520) for n in range(40)]
    # A rooted spanning tree uses all 40 nodes and 39 links exactly once/flit.
    tree = [(n - 1 if n % 8 else n - 8, n) for n in range(1, 40)]
    times = {(7, n): (n // 8 + n % 8 + 3) if n else 0 for n in range(40)}
    return [
        (
            "mesh40-exchange",
            5,
            8,
            1,
            exchange,
            {(100 + n, n ^ 1): 10 for n in range(40)},
        ),
        ("mesh40-broadcast", 5, 8, 1, [flow(7, 0, tree, list(range(40)))], times),
        (
            "mesh40-shared",
            5,
            8,
            1,
            [
                flow(1, 0, [(0, 1), (1, 2), (2, 3)], [3]),
                flow(2, 9, [(9, 1), (1, 2), (2, 10)], [10]),
            ],
            {},
        ),
        (
            "mesh40-node33",
            5,
            8,
            1,
            [
                flow(
                    1,
                    0,
                    [(0, 1), (1, 2), (1, 9), (9, 17), (17, 25), (25, 33)],
                    [0, 2, 33],
                )
            ],
            {(1, 0): 0, (1, 2): 5, (1, 33): 8},
        ),
    ]


def write_input(path, rows, cols, hop, flows):
    lines = [f"{rows} {cols} {hop} {len(flows)}"]
    for f in flows:
        lines.append(
            f"{f['id']} {f['source']} {f['nbytes']} {len(f['targets'])} {len(f['edges'])}"
        )
        lines.append(" ".join(map(str, f["targets"])))
        lines.extend(f"{a} {b}" for a, b in f["edges"])
    path.write_text("\n".join(lines) + "\n")


def semantic(result):
    return {
        key: result[key]
        for key in ("model_time", "read_bytes", "write_bytes", "link_bytes", "events")
    }


def check(result, flows, cols, expected):
    events = result["events"]
    assert all(a["noc_cycle"] <= b["noc_cycle"] for a, b in zip(events, events[1:]))
    completions = [e for e in events if e["kind"] == 5]
    assert Counter((e["id"], e["node"]) for e in completions) == Counter(
        (f["id"], n) for f in flows for n in f["targets"]
    )
    actual = {(e["id"], e["node"]): e["noc_cycle"] for e in completions}
    for key, time in expected.items():
        assert actual[key] == time, (key, actual[key], time)
    # Targets can be internal nodes: completion does not imply the remaining
    # branches have drained. Source retirement (kind 6) determines termination.
    assert result["model_time"] >= max(actual.values(), default=0)
    assert result["read_bytes"] == sum(f["nbytes"] for f in flows)
    assert result["write_bytes"] == sum(f["nbytes"] * len(f["targets"]) for f in flows)
    assert result["link_bytes"] == sum(
        ((f["nbytes"] + 51) // 52) * len(f["edges"]) * 64 for f in flows
    )
    links = [e for e in events if e["kind"] == 3]
    offset = (-cols, -1, 1, cols)
    assert Counter(
        (e["id"], e["node"], e["node"] + offset[e["direction"]], e["flit"])
        for e in links
    ) == Counter(
        (f["id"], a, b, k)
        for f in flows
        for a, b in f["edges"]
        for k in range((f["nbytes"] + 51) // 52)
    )
    slots = [(e["node"], e["direction"], e["noc_cycle"]) for e in links]
    assert len(slots) == len(set(slots)), "directed link overbooked"
    landings = [e for e in events if e["kind"] == 9]
    slots = [(e["node"], e["noc_cycle"]) for e in landings]
    assert len(slots) == len(set(slots)), "shared ejection port overbooked"
    assert Counter((e["id"], e["node"], e["flit"]) for e in landings) == Counter(
        (f["id"], n, k)
        for f in flows
        for n in f["targets"]
        if n != f["source"]
        for k in range((f["nbytes"] + 51) // 52)
    )


def coarse_compare(runner, folder, cols, hop, flows, result):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "wse_model/src"))
    from wse_model.programs.coarse import Profile, Route, Transfer, Workload

    def node(n):
        return n // cols * 8 + n % cols

    routes = [
        Route(
            i,
            node(f["source"]),
            tuple((node(a), node(b)) for a, b in f["edges"]),
            tuple(node(n) for n in f["targets"]),
        )
        for i, f in enumerate(flows)
    ]
    transfers = [
        Transfer(f["id"], node(f["source"]), i, f["nbytes"])
        for i, f in enumerate(flows)
    ]
    old = folder / "coarse"
    old.mkdir(exist_ok=True)
    loader = Workload(routes, transfers, Profile.open(hop_ticks=hop)).write(old)
    subprocess.run(
        [
            str(runner),
            str(loader),
            str(old / "receipt.json"),
            str(old / "events.jsonl"),
        ],
        check=True,
    )
    receipt = json.loads((old / "receipt.json").read_text())
    assert receipt["fault"] == 0
    for key in ("model_time", "read_bytes", "write_bytes", "link_bytes"):
        assert receipt[key] == result[key], (key, receipt[key], result[key])
    original = [
        json.loads(line) for line in (old / "events.jsonl").read_text().splitlines()
    ]
    mapped = [
        {
            key: (node(value) if key in ("source", "node") else value)
            for key, value in event.items()
            if key != "seq"
        }
        for event in result["events"]
    ]
    assert mapped == original, next(
        ((i, a, b) for i, (a, b) in enumerate(zip(mapped, original)) if a != b),
        (len(mapped), len(original)),
    )
    return len(original)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runner", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--stage", choices=("prototype", "mesh40"), required=True)
    p.add_argument("--coarse-runner", type=Path)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    receipt = dict(
        status="INCOMPLETE",
        stage=args.stage,
        runner_sha256=hashlib.sha256(args.runner.read_bytes()).hexdigest(),
        cases=[],
    )
    if args.coarse_runner:
        receipt["coarse_runner_sha256"] = hashlib.sha256(
            args.coarse_runner.read_bytes()
        ).hexdigest()
    try:
        for name, rows, cols, hop, flows, expected in cases(args.stage):
            folder = args.output / name
            folder.mkdir(exist_ok=True)
            source = folder / "input.txt"
            write_input(source, rows, cols, hop, flows)
            baseline = None
            entry = dict(name=name, rows=rows, cols=cols, hop_ticks=hop, modes=[])
            receipt["cases"].append(entry)
            for mode, workers in [("reference", 0)] + [
                ("batch", w) for w in (0, 1, 2, 4, 8)
            ]:
                command = [str(args.runner), str(source), str(workers), mode]
                process = subprocess.run(command, text=True, capture_output=True)
                (folder / f"{mode}-{workers}.json").write_text(process.stdout)
                (folder / f"{mode}-{workers}.stderr").write_text(process.stderr)
                assert process.returncode == 0, (command, process.stderr)
                result = json.loads(process.stdout)
                check(result, flows, cols, expected)
                if name == "drain-after-local":
                    assert result["model_time"] == 3
                else:
                    assert result["model_time"] == max(
                        e["noc_cycle"] for e in result["events"] if e["kind"] == 5
                    )
                if baseline is None:
                    baseline = result
                else:
                    assert semantic(result) == semantic(baseline), (name, mode, workers)
                if mode == "batch" and name in ("independent", "mesh40-exchange"):
                    assert result["max_batch"] >= 2
                entry["modes"].append(
                    dict(
                        mode=mode,
                        workers=workers,
                        max_batch=result["max_batch"],
                        steps=result["execution_steps"],
                        status="PASS",
                    )
                )
            if args.coarse_runner:
                entry["coarse_exact_events"] = coarse_compare(
                    args.coarse_runner, folder, cols, hop, flows, baseline
                )
            entry["status"] = "PASS"
            print(name, "PASS", flush=True)
        receipt["status"] = "PASS"
    except Exception as error:
        receipt.update(status="FAIL", error=repr(error))
        raise
    finally:
        (args.output / "verification.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )


if __name__ == "__main__":
    main()
