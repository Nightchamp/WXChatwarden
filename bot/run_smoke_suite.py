#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BOT_DIR.parent
CONTAINER = os.environ.get("CW_SMOKE_CONTAINER", "wechat-atspi")

RUNTIME_BASE = {
    "v3_smoke.py",
    "v3_followup_gate_smoke.py",
    "v3_imagegen_smoke.py",
    "v3_type49_smoke.py",
}
HOST_STARTUP = {"startup_bootstrap_smoke.py"}


def discover() -> tuple[list[str], list[str], list[str]]:
    found = sorted(path.name for path in BOT_DIR.glob("*smoke.py"))
    versioned = sorted(name for name in found if name.startswith("v3_v"))
    base = sorted(RUNTIME_BASE & set(found))
    host = sorted(HOST_STARTUP & set(found))

    expected = set(versioned) | set(base) | set(host)
    unknown = sorted(set(found) - expected)
    missing_base = sorted(RUNTIME_BASE - set(found))
    missing_host = sorted(HOST_STARTUP - set(found))
    if unknown or missing_base or missing_host:
        raise RuntimeError(
            "smoke classification incomplete: "
            f"unknown={unknown} missing_base={missing_base} "
            f"missing_host={missing_host}"
        )
    return versioned, base, host


def run_checked(argv: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(argv), flush=True)
    completed = subprocess.run(
        argv,
        cwd=PROJECT_DIR,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--runtime-only", action="store_true")
    args = parser.parse_args()

    versioned, base, host = discover()
    runtime = versioned + base

    print(f"SMOKE_DISCOVERED={len(runtime) + len(host)}")
    print(f"SMOKE_RUNTIME_VERSIONED={len(versioned)}")
    print(f"SMOKE_RUNTIME_BASE={len(base)}")
    print(f"SMOKE_RUNTIME_TOTAL={len(runtime)}")
    print(f"SMOKE_HOST_STARTUP={len(host)}")
    print(f"SMOKE_ALL_FILES={len(runtime) + len(host)}")

    if args.list:
        for name in versioned:
            print(f"runtime-versioned\t{name}")
        for name in base:
            print(f"runtime-base\t{name}")
        for name in host:
            print(f"host-startup\t{name}")
        return

    for name in runtime:
        run_checked(
            [
                "docker",
                "exec",
                "-e",
                "CW_LLM_ENDPOINT=http://127.0.0.1:9/v1/execute",
                "-e",
                "CW_DECISION_SHADOW=0",
                "-e",
                "CW_IGNORE_LIVE_OWNER_SETTINGS=1",
                "-e",
                "CW_MEMORY_DOCS_SCHEDULER=0",
                CONTAINER,
                "python3",
                "-B",
                f"/opt/chatwarden/bot/{name}",
            ]
        )

    print(f"RUNTIME_SMOKE_PASS count={len(runtime)}")

    if not args.runtime_only:
        env = dict(os.environ)
        env["CW_START_SH_PATH"] = str(PROJECT_DIR / "start.sh")
        for name in host:
            run_checked([sys.executable, str(BOT_DIR / name)], env=env)
        print(f"HOST_STARTUP_SMOKE_PASS count={len(host)}")

    print(
        "ALL_CLASSIFIED_SMOKE_PASS count="
        f"{len(runtime) + (0 if args.runtime_only else len(host))}"
    )


if __name__ == "__main__":
    main()
