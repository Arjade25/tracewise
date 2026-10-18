"""Start a local FalkorDB without Docker, using the server binaries shipped in the
``falkordblite`` wheel (real redis-server + the FalkorDB module; macOS and Linux)."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


def binaries() -> tuple[Path, Path]:
    try:
        import redislite  # provided by falkordblite
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("pip install falkordblite  (or run FalkorDB with Docker: "
                         "docker run -p 6379:6379 falkordb/falkordb)") from exc
    bin_dir = Path(redislite.__file__).parent / "bin"
    return bin_dir / "redis-server", bin_dir / "falkordb.so"


def start(port: int = 6379, data_dir: str = "./data", foreground: bool = False) -> subprocess.Popen | None:
    server, module = binaries()
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    args = [str(server), "--port", str(port), "--loadmodule", str(module), "--dir", str(Path(data_dir).resolve()),
            "--save", "60", "1", "--appendonly", "no", "--protected-mode", "no"]
    if foreground:
        os.execv(args[0], args)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    from falkordb import FalkorDB
    for _ in range(50):
        try:
            FalkorDB(port=port).list_graphs()
            return proc
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("FalkorDB did not start")
