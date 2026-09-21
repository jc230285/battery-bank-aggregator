"""One-shot Battery Bank background jobs.

The production web process is intentionally scheduler-free. Coolify/Home1 invokes
this module in a disposable container for each refresh, so scraper/parser/analysis
memory is returned to the OS when the job exits.
"""
from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("BBA_SCHEDULER_ENABLED", "0")

import app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=("hourly", "discovery"))
    args = parser.parse_args()

    if args.job == "hourly":
        result = app.run_hourly_refresh(trigger="hourly")
    else:
        result = app.run_cycle(trigger="discovery")

    print(json.dumps(result, default=str, sort_keys=True))
    if isinstance(result, dict) and result.get("status") == "failed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
