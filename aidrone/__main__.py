"""CLI: ``python -m aidrone list | run <scenario> | all``."""

from __future__ import annotations

import argparse
import sys
import time

from .scenarios import SCENARIOS, evaluate, run


def _print_events(res, limit: int | None) -> None:
    events = res.events if limit is None else res.events[-limit:]
    for e in events:
        print(f"  t={e.t:7.1f}s [{e.phase:>15}] {e.category:<10} {e.message}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aidrone", description="All-weather urban delivery drone autonomy simulator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list scenarios")
    r = sub.add_parser("run", help="run one scenario")
    r.add_argument("scenario", choices=sorted(SCENARIOS))
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--events", type=int, default=None, help="show only the last N decision-log events")
    a = sub.add_parser("all", help="run every scenario and print a scorecard")
    a.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    if args.cmd == "list":
        for name, sc in SCENARIOS.items():
            print(f"{name:<22} {sc.description}")
        return 0

    if args.cmd == "run":
        t0 = time.time()
        res = run(args.scenario, args.seed)
        print("Decision log:")
        _print_events(res, args.events)
        print()
        print(res.summary())
        ok, why = evaluate(res)
        print(f"result           : {'PASS' if ok else 'FAIL'} ({why}) in {time.time() - t0:.1f}s wall")
        return 0 if ok else 1

    failures = 0
    print(f"{'scenario':<22} {'result':<6} {'outcome':<18} {'err cm':>7} {'time s':>7} {'Wh':>6}  note")
    for name in SCENARIOS:
        res = run(name, args.seed)
        ok, why = evaluate(res)
        failures += not ok
        err = res.delivery_error_m if res.delivery_error_m is not None else res.parcel_error_m
        outcome = f"delivered/{res.delivery_method}" if res.delivered else res.final_phase
        note = why if not ok else (res.abort_reason or "")
        print(f"{name:<22} {'PASS' if ok else 'FAIL':<6} {outcome:<18} "
              f"{(err * 100 if err is not None else float('nan')):>7.1f} {res.flight_time_s:>7.0f} {res.energy_used_wh:>6.0f}  {note[:70]}")
    print(f"\n{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
