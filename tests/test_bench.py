from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def test_bench_smoke(capsys):
    path = Path(__file__).resolve().parents[1] / "bench" / "bench.py"
    spec = importlib.util.spec_from_file_location("bench", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench"] = module
    spec.loader.exec_module(module)
    assert module.main(["--iterations", "2000", "--keys", "10"]) == 0
    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if line.startswith("| memory |")]
    assert len(rows) == 5
    assert all("µs" in row for row in rows)
