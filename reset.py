"""Delete every record this project created (and nothing else). Dry run by default.

    python reset.py            # list what would be deleted
    python reset.py --apply    # delete it

Runs as admin (seeding account, item 13). Children are deleted before parents. Untagged
record counts are snapshotted before and after to show nothing else was touched.
"""
import argparse
import sys

import config
import state_api
from sn_client import SNClient, SNError

P = config.TAG_PREFIX
# (table, tag query), in safe deletion order
TAGGED = [
    ("incident", f"correlation_idSTARTSWITH{P}-"),
    ("change_request", f"correlation_idSTARTSWITH{P}-"),
    ("problem", f"correlation_idSTARTSWITH{P}-"),
    ("kb_knowledge", f"short_descriptionSTARTSWITH[{P}]"),
    ("cmdb_ci", f"correlation_idSTARTSWITH{P}-"),
    ("sys_user_grmember", f"group.nameSTARTSWITH[{P}] "),
    ("sys_user_group", f"nameSTARTSWITH[{P}] "),
    ("sys_user", "user_nameSTARTSWITHpmsynth."),
]


def untagged(sn):
    """Records per table that do NOT carry the tag (total minus tagged)."""
    return {t: sn.count(t) - sn.count(t, q) for t, q in TAGGED}


def main():
    ap = argparse.ArgumentParser(description="Delete only PMSYNTH-tagged records.")
    ap.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    args = ap.parse_args()
    try:
        sn = SNClient(user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
        before = untagged(sn)
        total = 0
        for table, q in TAGGED:
            rows = sn.get_all(table, q, fields=["sys_id"])
            total += len(rows)
            print(f"{'DELETING' if args.apply else 'would delete'} {len(rows):4}  {table}")
            if args.apply:
                for r in rows:
                    sn.delete(table, r["sys_id"])
        api = state_api.remove(sn, apply=args.apply)
        total += api
        print(f"{'DELETING' if args.apply else 'would delete'} {api:4}  {state_api.NAME} (Scripted REST definition + operations)")
        if not args.apply:
            print(f"Dry run: {total} tagged records. Rerun with --apply to delete them.")
            return
        left = {t: sn.count(t, q) for t, q in TAGGED}
        after = untagged(sn)
        left["state_api"] = state_api.remove(sn, apply=False)
        ok = not any(left.values()) and before == after
        print(f"tagged records left: {sum(left.values())} | untagged counts unchanged: {before == after}")
        if before != after:
            print("  untagged before/after:", {t: (before[t], after[t]) for t in before if before[t] != after[t]})
        print("RESULT:", "RESET OK" if ok else "RESET INCOMPLETE")
        sys.exit(0 if ok else 1)
    except SNError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
