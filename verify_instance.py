"""Phase 1 check: confirm PDI access and capture real choice values into config/instance.json.

Exit codes: 0 all checks passed, 1 a check failed, 2 instance unreachable / auth failure.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sn_client import AuthError, InstanceUnavailable, SNClient, SNError, connect_or_exit

CONFIG_PATH = Path(__file__).parent / "config" / "instance.json"

REQUIRED_TABLES = ["incident", "problem", "change_request", "kb_knowledge", "sys_user", "sys_user_group"]
CMDB_TABLES = ["cmdb_ci", "cmdb_ci_server", "cmdb_ci_appl", "cmdb_ci_database", "cmdb_ci_netgear"]
CHOICE_FIELDS = [("problem", "state"), ("incident", "state"), ("change_request", "state"),
                 ("problem", "resolution_code"), ("incident", "category"), ("incident", "subcategory"),
                 ("incident", "close_code")]
# Fields later phases depend on: (table that defines it, field). Inherited task fields live on "task".
FIELDS = [("task", "correlation_id"), ("task", "work_notes"), ("task", "close_notes"),
          ("task", "parent"), ("problem", "known_error"), ("problem", "cause_notes"),
          ("problem", "fix_notes"), ("problem", "workaround"), ("problem", "resolution_code"),
          ("problem", "rfc"), ("change_request", "type")]


def check_tables(client):
    results = {}
    for t in REQUIRED_TABLES + CMDB_TABLES:
        try:
            client.get(t, fields=["sys_id"], limit=1)
            results[t] = "ok"
        except InstanceUnavailable:
            raise
        except SNError as e:  # includes a per-table 403 (missing role)
            results[t] = f"FAIL: {e}"
    return results


def fetch_choices(client, table, element):
    """Active English choices for table.element; falls back to the parent 'task' definition."""
    for name in (table, "task"):
        rows = client.get_all("sys_choice", f"name={name}^element={element}^inactive=false^language=en^ORDERBYsequence",
                              fields=["value", "label", "sequence"])
        if rows:
            seen, out = set(), []
            for r in rows:
                if r["value"] not in seen:
                    seen.add(r["value"])
                    out.append({"value": r["value"], "label": r["label"]})
            return name, out
    return None, []


def check_fields(client):
    names = sorted({t for t, _ in FIELDS})
    elements = sorted({f for _, f in FIELDS})
    rows = client.get_all("sys_dictionary", f"nameIN{','.join(names)}^elementIN{','.join(elements)}",
                          fields=["name", "element"])
    present = {(r["name"], r["element"]) for r in rows}
    return {f"{t}.{f}": (t, f) in present for t, f in FIELDS}


def main():
    client = connect_or_exit()
    print(f"Connected to {client.base}")
    # Instance metadata (sys_choice, sys_dictionary) is not readable with itil roles; read it as
    # admin, which item 13 keeps for setup. Table access is checked as the agents' own user.
    try:
        admin = SNClient(user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
    except SNError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"Table access checked as {client.session.auth[0]}; choices and fields read as {admin.session.auth[0]}\n")
    ok = True
    try:
        tables = check_tables(client)
        print("TABLE ACCESS")
        for t, status in tables.items():
            optional = t in CMDB_TABLES and t != "cmdb_ci"
            print(f"  {'PASS' if status == 'ok' else ('WARN' if optional else 'FAIL'):4}  {t}"
                  + ("" if status == "ok" else f"  {status}"))
            if status != "ok" and not optional:
                ok = False

        choices = {}
        print("\nCHOICE VALUES (sys_choice)")
        for table, element in CHOICE_FIELDS:
            source, values = fetch_choices(admin, table, element)
            key = f"{table}.{element}"
            choices[key] = values
            print(f"  {key}" + (f"  (defined on {source})" if source and source != table else ""))
            if not values:
                print("    FAIL: no choices found")
                ok = False
            for v in values:
                print(f"    {v['value']:>6}  {v['label']}")

        print("\nFIELDS (sys_dictionary)")
        fields = check_fields(admin)
        for f, present in fields.items():
            print(f"  {'yes' if present else 'NO ':3}  {f}")
        for required in ("task.correlation_id", "task.parent"):
            if not fields[required]:
                ok = False

        ke_state = [c for c in choices["problem.state"] if "known error" in c["label"].lower()]
        known_error = {"field": fields["problem.known_error"], "state": ke_state[0]["value"] if ke_state else None}
        print(f"\nKNOWN ERROR MECHANISM  checkbox problem.known_error: {known_error['field']}, "
              f"'Known Error' state: {known_error['state']}")
        if not known_error["field"] and not known_error["state"]:
            print("  FAIL: no known-error mechanism found")
            ok = False
    except (InstanceUnavailable, AuthError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except SNError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    CONFIG_PATH.parent.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "instance": client.base,
        "tables": tables,
        "choices": choices,
        "fields": fields,
        "known_error": known_error,
    }, indent=2))
    print(f"\nWrote {CONFIG_PATH.relative_to(Path(__file__).parent)}")
    print("RESULT:", "ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
