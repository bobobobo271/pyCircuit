"""Build an experimental WSE C++ backend variant from the pinned SDK emission.

Retain pure temporary values across Work() calls. A temporary whose inputs
come from unchanged register outputs or top-level pins can reuse its previous
value. Pointwise node tables can update just the changed row indices.
"""
import argparse
import json
import re
import shutil
from collections import Counter
from functools import lru_cache
from pathlib import Path

LOOP = re.compile(r"    for \(std::size_t pyc_lane = 0; pyc_lane < ([\d()* pyc_count]+); \+\+pyc_lane\) ")
VALUE = re.compile(r"pyc_values\.page<pyc_work_page_(\d+)>\(\1\)\.pyc_value_(\d+)\.element\(")
PIN = re.compile(r"(this->(?:\w+->)*\w+)\.element\(")
DIRECT_PORTS = {"this->host", "this->pyc_7079635f636c6b", "this->pyc_7079635f727374",
                "this->pyc_5f5f7079635f76616c69645f696e5f30"}
HEADER = Path("sources/wse_machine/machine.hpp")


@lru_cache(None)
def shape_count(expr):
    # This SDK emits only literal products for table shapes (one WseMachine).
    if not re.fullmatch(r"[0-9*() ]+", expr.replace("pyc_count", "1")):
        raise ValueError(f"unsupported emitted shape {expr}")
    product = 1
    for factor in re.findall(r"\d+", expr.replace("pyc_count", "1")):
        product *= int(factor)
    return product


def index_expr(rhs, start):
    depth = 1
    for pos in range(start, len(rhs)):
        depth += (rhs[pos] == "(") - (rhs[pos] == ")")
        if not depth:
            return rhs[start:pos]
    raise ValueError("unterminated emitted index")


@lru_cache(None)
def lane_identity(index, size):
    if not re.fullmatch(r"(?:pyc_lane|[0-9+*/%() ])+", index):
        return False
    # Evaluate only the restricted index grammar above.
    code = compile(index.replace("/", "//"), "<emitted lane index>", "eval")
    return all(eval(code, {"__builtins__": {}}, {"pyc_lane": i}) == i for i in range(size))


def transform(source, destination):
    text = source.read_text()
    family = text.index("class pyc_family_WseMachine final {")
    start = text.index("  pyc_work_noinline void pyc_work_part_0(", family)
    end = text.index("  pyc_work_noinline void pyc_work_dispatch_", start)
    lines = text[start:end].splitlines(keepends=True)
    roots = {}
    values = {}
    touched = set()
    stats = Counter()
    output = []
    current_guard = None

    def add(line, guard=None):
        nonlocal current_guard
        if guard != current_guard:
            if current_guard is not None:
                output.append("    }\n")
            if guard is not None:
                output.append(f"    if ({guard}) {{\n")
            current_guard = guard
        output.append(line)

    for line in lines:
        loop = LOOP.match(line)
        lhs = VALUE.match(line, loop.end()) if loop else None
        if not lhs or not line[lhs.end():].startswith("pyc_lane) = "):
            # A kernel may drive Q in Work(). Later direct reads of that pin
            # cannot use its entry snapshot. All captures and kernels run.
            if "Outputs{" in line:
                touched.update(PIN.findall(line.split("Outputs{", 1)[1]))
            if loop and line[loop.end():].startswith("this->"):
                pin = PIN.match(line, loop.end())
                if pin:
                    touched.add(pin[1])
            add(line)
            continue
        rhs = line[lhs.end() + len("pyc_lane) = "):]
        deps = set()
        size = shape_count(loop[1])
        row = None
        aligned = size > 1
        args = []
        for ref in VALUE.finditer(rhs):
            dep = values.get(ref[2])
            if dep is None:
                break
            sources, upstream_row, upstream_size = dep
            deps.update(sources)
            if upstream_size > 1 and sources:
                args.append((upstream_row, index_expr(rhs, ref.end()), upstream_size))
        else:
            pins = list(PIN.finditer(rhs))
            recognized = all(p[1] not in touched and (p[1] in DIRECT_PORTS or
                             p[1].endswith("_q")) for p in pins)
            if recognized and len(pins) == rhs.count("this->") and "->work(" not in rhs:
                for pin in pins:
                    name = pin[1]
                    deps.add(name)
                    if size > 1:
                        args.append((name, index_expr(rhs, pin.end()), size))
                for name, index, input_size in args:
                    if (name is None or (row is not None and row != name)
                            or input_size != size or not lane_identity(index, size)):
                        aligned = False
                        break
                    row = name
                row = row if aligned else None
                # A dirty scalar selecting a table can affect all its rows.
                # Use row iteration only for cones depending on one table.
                if len(deps) != 1:
                    row = None
                values[lhs[2]] = (frozenset(deps), row, size)
                for name in sorted(deps):
                    roots.setdefault(name, len(roots))
                mask = sum(1 << roots[name] for name in deps)
                check = f"pyc_changed & {mask}ULL" if deps else "pyc_refresh_all"
                if row is not None:
                    rid = roots[row]
                    # Enforce the same extent, including register-array roots.
                    line = (f"    static_assert(decltype({row[6:]})::size == {size});\n"
                            + f"    for (std::size_t pyc_lane : pyc_rows_{rid}) " + line[loop.end():])
                    stats["row_loops"] += 1
                add(line, check)
                stats["cached_definitions"] += 1
                continue
        add(line)
    add("")
    if len(roots) > 64 or not stats["row_loops"]:
        raise ValueError(f"unsupported dependency shape {len(roots)}, {stats}")
    support = ["  static_assert(pyc_count == 1);\n",
               "  bool pyc_cache_valid = false, pyc_refresh_all = true;\n",
               "  std::uint64_t pyc_changed = 0;\n"]
    refresh = []
    table_ids = {roots[row] for _, row, _ in values.values() if row is not None}
    for name, i in roots.items():
        member = name[6:]
        support.append(f"  decltype({member}) pyc_prev_{i};\n")
        if i in table_ids:
            support.append(f"  std::vector<std::size_t> pyc_rows_{i};\n")
            refresh.append(f"    pyc_rows_{i}.clear();\n")
        refresh.append(f"""    for (std::size_t i=0; i<decltype({member})::size; ++i) {{
      const auto &now = {name}.element(i);
      auto &old = pyc_prev_{i}.element(i);
      if (pyc_refresh_all || !(now.packed().value()==old.packed().value() && now.packed().knownMask()==old.packed().knownMask() && now.packed().zMask()==old.packed().zMask())) {{
        old = now;
        pyc_changed |= (1ULL << {i});
""")
        if i in table_ids:
            refresh.append(f"        pyc_rows_{i}.push_back(i);\n")
        refresh.append("      }\n    }\n")
    support.append("  void pyc_scan_inputs() {\n    pyc_refresh_all = !pyc_cache_valid;\n    pyc_cache_valid = false;\n    pyc_changed = 0;\n")
    support.extend(refresh)
    support.append("  }\n")
    tail = text[end:]
    frame = re.search(r"      pyc_work_frame pyc_values\((\d+)\);", tail)
    if not frame:
        raise ValueError("missing work frame")
    support.append(f"  pyc_work_frame pyc_saved_values{{{frame[1]}}};\n")
    tail = tail[:frame.start()] + "      auto &pyc_values = pyc_saved_values;\n      pyc_scan_inputs();" + tail[frame.end():]
    tail = tail.replace("    } catch (...) { DiscardNext(); throw; }",
                        "      pyc_cache_valid = true;\n    } catch (...) { DiscardNext(); throw; }", 1)
    destination.write_text(text[:start] + "".join(support + output) + tail)
    return dict(stats, roots=len(roots), row_inputs={str(i):name for name,i in roots.items() if i in table_ids})


def specialize_registers(output):
    # These two generated families own only node-indexed DFFE tables. Leave
    # scalar machine registers and all memory kernels on their usual backend.
    support = "wse_sparse_registers.hpp"
    shutil.copyfile(Path(__file__).with_name(support), output / support)
    for filename in ("ledger.hpp", "control_ledger.hpp"):
        path = output / "sources/wse_calendar" / filename
        text = path.read_text()
        original = "gfsim::collection_storage<::gfsim::dffe_kernel<"
        if original not in text:
            raise ValueError(f"unsupported register declarations in {path}")
        text = text.replace(original, "wse_sparse_registers<::gfsim::dffe_kernel<")
        path.write_text(text.replace('#include "pycircuit_support.hpp"',
                                     f'#include "pycircuit_support.hpp"\n#include "{support}"'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("emitted_cpp", type=Path)
    parser.add_argument("output_cpp", type=Path, help="fresh directory for the gfsim variant")
    args = parser.parse_args()
    shutil.copytree(args.emitted_cpp, args.output_cpp)
    result = transform(args.emitted_cpp / HEADER, args.output_cpp / HEADER)
    specialize_registers(args.output_cpp)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
