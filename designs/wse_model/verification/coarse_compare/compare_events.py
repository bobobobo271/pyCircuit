"""Compare ordered events shared by detailed and coarse timing semantics.

Detailed kind 3 includes Calendar retry attempts; coarse labels those as 11.
Detailed WAIT/END records and coarse Barrier records have no matching event
record in the other trace. Compare the shared kind/time/source/node/flit fields.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detailed", type=Path, required=True)
    parser.add_argument("--coarse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    def events(path: Path, *, coarse: bool) -> list[tuple]:
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [
            (
                3 if row["kind"] == 11 else row["kind"],
                row["noc_cycle"],
                row["source"],
                row["node"],
                row["flit"],
            )
            for row in rows
            if row["kind"] not in ((10,) if coarse else (7, 8))
        ]

    detailed = events(args.detailed, coarse=False)
    coarse = events(args.coarse, coarse=True)
    result = {
        "status": "PASS" if detailed == coarse else "FAIL",
        "ordered_exact": detailed == coarse,
        "detailed_common_events": len(detailed),
        "coarse_common_events": len(coarse),
        "fields": ["kind", "noc_cycle", "source", "node", "flit"],
        "mapping": "coarse11->detailed3; omit detailed7/8 and coarse10",
        "inputs": {
            str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (args.detailed, args.coarse)
        },
    }
    if detailed != coarse:
        result["first_mismatch"] = next(
            (
                {"index": index, "detailed": left, "coarse": right}
                for index, (left, right) in enumerate(zip(detailed, coarse, strict=False))
                if left != right
            ),
            {"index": min(len(detailed), len(coarse)), "reason": "different event counts"},
        )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if detailed != coarse:
        raise AssertionError("common ordered timing events differ")


if __name__ == "__main__":
    main()
