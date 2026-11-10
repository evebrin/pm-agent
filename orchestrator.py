"""Orchestrator (agent 0): runs the Problem Management agents in order, with their success checks.

    python orchestrator.py --all [--dry-run] [--auto-approve] [--seed N]       # new run, every step
    python orchestrator.py --run-id <id> --all [--dry-run] [--auto-approve]    # continue an existing run
    python orchestrator.py --run-id <id> --step <name> [--dry-run] [--auto-approve]
    python orchestrator.py --list

Steps: synthetic -> detect (Gate 1) -> rca (Gate 2) -> knowledge -> change (Gate 3) -> close (Gate 4) -> report.
Every step reads and writes the run context runs/<run_id>.json. A live step is followed by its success check;
--all stops at the first failure. --dry-run makes no writes (and skips the checks, which read the PDI).
--auto-approve answers the gates with each gate's recommendation; it is for rehearsals, and every decision it
makes is logged as "auto-approve rehearsal".
"""
import argparse
import sys

import change_agent
import config
import detection_agent
import knowledge_agent
import rca_agent
import reporting_agent
import synthetic_data_agent
from sn_client import SNError

LOG = "[orchestrator]"
TRANSIENT = ("dry_run", "auto_approve", "engine", "problems", "ask", "method")
# name -> (what it does, run(context), check(context) -> bool)
STEPS = {
    "synthetic": ("Synthetic Data: seed groups, users, CIs and incidents", synthetic_data_agent.run, synthetic_data_agent.check),
    "detect": ("Problem Detection + Human Gate 1", detection_agent.run, detection_agent.check),
    "rca": ("Root Cause Analysis + Human Gate 2", rca_agent.run, rca_agent.check),
    "knowledge": ("Known Error & KB", knowledge_agent.run, knowledge_agent.check),
    "change": ("Change: permanent-fix change + Human Gate 3", change_agent.run, change_agent.check),
    "close": ("Closure: verification window + Human Gate 4", change_agent.close, change_agent.check_closure),
    "report": ("Reporting: reports/<run_id>.md, numbers from the PDI",
               lambda ctx: reporting_agent.run(ctx) if not ctx.get("dry_run") else print(f"{LOG} (dry run: report not written)"),
               lambda ctx: reporting_agent.check(ctx["run_id"])),
}


def step(ctx, name, dry, auto):
    desc, run, check = STEPS[name]
    print(f"\n{LOG} ===== {name}: {desc}{' (dry run)' if dry else ''}")
    ctx.update(dry_run=dry, auto_approve=auto)
    try:
        run(ctx)
    finally:
        for k in TRANSIENT:
            ctx.pop(k, None)
    if dry:
        return True
    config.save_context(ctx)
    ok = check(ctx)
    ctx.setdefault("steps", []).append({"step": name, "check": "passed" if ok else "failed"})
    config.save_context(ctx)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id", help="existing run (omit with --all to start a new one)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="run every step in order")
    g.add_argument("--step", choices=STEPS)
    g.add_argument("--list", action="store_true", help="list the steps")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--auto-approve", action="store_true", help="rehearsal: answer each gate with its recommendation")
    ap.add_argument("--seed", type=int, default=451, help="seed for a new run's synthetic data")
    args = ap.parse_args()
    if args.list:
        for n, (desc, _, _) in STEPS.items():
            print(f"  {n:10} {desc}")
        return
    try:
        if args.run_id:
            ctx = config.load_context(args.run_id)
        elif args.all:
            ctx = config.new_context(args.seed)
            print(f"{LOG} new run {ctx['run_id']} (tag {ctx['tag']})")
        else:
            ap.error("--step needs --run-id")
        names = list(STEPS) if args.all else [args.step]
        if args.all and args.run_id and ctx.get("synthetic"):
            names.remove("synthetic")  # this run's data already exists
            print(f"{LOG} run {ctx['run_id']} already has synthetic data; continuing from detection")
        for n in names:
            if not step(ctx, n, args.dry_run, args.auto_approve):
                sys.exit(f"{LOG} step {n!r} failed its success check; stopping")
        print(f"\n{LOG} done: {', '.join(names)}{' (dry run, nothing written)' if args.dry_run else ''}")
    except SNError as e:
        sys.exit(f"{LOG} ERROR: {e}")


if __name__ == "__main__":
    main()
