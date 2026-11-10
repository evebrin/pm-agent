"""Reporting Agent: reports/<run_id>.md (with a Mermaid chart) and reports/<run_id>.json.

Every number is recomputed from the PDI. The only other inputs are the run tag and, for scoring, the
answer key; the run file (runs/<run_id>.json) is not read. Gate decisions come from the problems'
work notes. Runs as pm_agent_svc; the state history comes from sys_audit, read as admin (read-only).

    python reporting_agent.py --run-id <id>
    python reporting_agent.py --check <run_id>     # recompute independently and compare with the JSON
"""
import argparse
import html
import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone

import config
import rca_agent
from change_agent import recurrence
from sn_client import SNClient, SNError

LOG = "[report]"
FMT = "%Y-%m-%d %H:%M:%S"
REPORTS = config.ROOT / "reports"
STATES = ["New", "Assess", "Root Cause Analysis", "Fix in Progress", "Resolved", "Closed"]
GATE_RE = re.compile(r"Human Gate (\d)( revised)?: (.+?) by (\S+?)(?= at |\.|$)")
WINDOW_RE = re.compile(r"Verification window (\d{4}-\d\d-\d\d) to (\d{4}-\d\d-\d\d)")


def _t(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def _key(run_id):
    return json.loads((config.RUNS_DIR / f"{run_id}_answer_key.json").read_text())["patterns"]


def _notes(text):
    return rca_agent._notes(text)


def state_hours(admin, p, label):
    """Hours spent in each state, from the audit history of problem.state (now for the current state)."""
    changes = admin.get_all("sys_audit", f"tablename=problem^documentkey={p['sys_id']}^fieldname=state^ORDERBYsys_created_on",
                            fields=["oldvalue", "newvalue", "sys_created_on"])
    t, state, hours = _t(p["sys_created_on"]), config.choice_value("problem.state", "New"), Counter()
    for c in changes:
        if c["oldvalue"] == c["newvalue"] or not c["oldvalue"]:
            continue
        when = _t(c["sys_created_on"])
        hours[label.get(state, state)] += (when - t).total_seconds() / 3600
        t, state = when, c["newvalue"]
    if label.get(state) != "Closed":
        hours[label.get(state, state)] += (datetime.now(timezone.utc) - t).total_seconds() / 3600
    return {s: round(hours[s], 1) for s in STATES if s in hours}


def compute(run_id):
    """All report metrics, from the PDI (plus the answer key for scoring)."""
    tag = config.tag(run_id)
    svc = SNClient()
    admin = SNClient(user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
    key = _key(run_id)
    choices = config.load_instance()["choices"]
    label = {c["value"]: c["label"] for c in choices["problem.state"]}
    chg_label = {c["value"]: c["label"] for c in choices["change_request.state"]}
    problems = sorted(svc.get_all("problem", f"correlation_id={tag}", fields=[
        "sys_id", "number", "short_description", "state", "cmdb_ci", "business_service", "correlation_id", "cause_notes",
        "known_error", "primary_known_error_article", "rfc", "resolution_code", "sys_created_on"]), key=lambda p: p["number"])
    notes = {r["number"]: _notes(r["work_notes"]) for r in svc.get_all(
        "problem", f"correlation_id={tag}", fields=["number", "work_notes"], display_value=True)}
    linked = {p["number"]: svc.get_all("incident", f"{config.INCIDENT_PROBLEM_LINK}={p['sys_id']}", fields=["number", "opened_at"])
              for p in problems}

    # detection vs planted
    det = {}
    for pid, pat in key.items():
        planted = set(pat["incidents"])
        best = max(problems, key=lambda p: len(planted & {i["number"] for i in linked[p["number"]]}), default=None)
        got = {i["number"] for i in linked[best["number"]]} if best else set()
        hit = len(planted & got)
        det[pid] = {"ci": pat["ci"], "planted": pat["expect_problem"], "incidents": len(planted),
                    "problem": best["number"] if best and hit else None,
                    "recall": round(hit / len(planted), 2) if planted else 0,
                    "precision": round(hit / len(got), 2) if got and hit else 0}
    pattern_of = {d["problem"]: pid for pid, d in det.items() if d["problem"] and d["planted"]}

    # observation period of the data before the changes: first to last incident opened before any change was implemented
    implemented = [_t(c["work_end"]) for c in svc.get_all("change_request", f"correlation_id={tag}^work_endISNOTEMPTY", fields=["work_end"])]
    before = f"^opened_at<{min(implemented).strftime(FMT)}" if implemented else ""
    first = svc.get("incident", f"correlation_id={tag}{before}^ORDERBYopened_at", fields=["opened_at"], limit=1)
    last = svc.get("incident", f"correlation_id={tag}{before}^ORDERBYDESCopened_at", fields=["opened_at"], limit=1)
    observed_days = max(1.0, (_t(last[0]["opened_at"]) - _t(first[0]["opened_at"])).total_seconds() / 86400) if first else 1.0
    rows, closed_rows = [], []
    for p in problems:
        n = p["number"]
        art = svc.get("kb_template_known_error_article", f"sys_id={p['primary_known_error_article']}", fields=["number", "workflow_state", "kb_cause"]) \
            if p["primary_known_error_article"] else []
        chg = svc.get("change_request", f"sys_id={p['rfc']}", fields=["number", "state", "close_code", "work_end"]) if p["rfc"] else []
        pid = pattern_of.get(n)
        cause = html.unescape(p["cause_notes"])
        rca_hit = rca_agent.score(pid, cause)[0] if pid and cause.strip() else None
        kb_hit = rca_agent.score(pid, re.sub(r"<[^>]+>", " ", art[0]["kb_cause"]))[0] if pid and art else None
        gates = [f"Gate {g.group(1)}{' (revised)' if g.group(2) else ''}: {g.group(3)} by {g.group(4)}"
                 for x in reversed(notes[n]) for g in GATE_RE.finditer(x)]  # work notes come newest first
        row = {"number": n, "title": p["short_description"], "state": label.get(p["state"], p["state"]),
               "pattern": pid, "linked_incidents": len(linked[n]), "known_error": p["known_error"] == "true",
               "article": art[0]["number"] if art else None, "article_state": art[0]["workflow_state"] if art else None,
               "change": chg[0]["number"] if chg else None,
               "change_state": chg_label.get(chg[0]["state"]) if chg else None,
               "change_close_code": chg[0]["close_code"] if chg else None,
               "resolution_code": p["resolution_code"] or None, "rca_match": rca_hit, "kb_match": kb_hit,
               "gate_decisions": gates, "hours_in_state": state_hours(admin, p, label)}
        # estimated recurring incidents prevented, from the verification window recorded at Gate 4
        win = next((WINDOW_RE.search(x) for x in notes[n] if x.startswith("Human Gate 4") and WINDOW_RE.search(x)), None)
        win = win or next((WINDOW_RE.search(x) for x in notes[n] if "Human Gate 4" in x and WINDOW_RE.search(x)), None)
        if win and chg:
            start, end = _t(chg[0]["work_end"]), datetime.fromisoformat(win.group(2)).replace(tzinfo=timezone.utc)
            v = recurrence(svc, p, start, end.replace(hour=12))
            rate = len(linked[n]) / observed_days
            window_days = (end.replace(hour=12) - start).total_seconds() / 86400
            expected = rate * window_days
            row["verification"] = {"window": f"{start:%Y-%m-%d} to {win.group(2)}", "window_days": round(window_days, 1),
                                   "rate_per_day_before": round(rate, 3), "expected_without_fix": round(expected, 1),
                                   "similar_in_window": len(v["similar"]), "clean": v["clean"],
                                   "prevented_estimate": round(max(0.0, expected - len(v["similar"])), 1)}
        rows.append(row)
        if row["state"] == "Closed":
            closed_rows.append(row)

    kb_all = svc.get_all("kb_knowledge", f"short_descriptionSTARTSWITH[{config.TAG_PREFIX}]", fields=["workflow_state"])
    changes = svc.get_all("change_request", f"correlation_id={tag}", fields=["state", "close_code", "type"])
    planted_total = sum(1 for d in det.values() if d["planted"])
    per_state = {s: round(statistics.median([r["hours_in_state"][s] for r in rows if s in r["hours_in_state"]]), 1)
                 for s in STATES if any(s in r["hours_in_state"] for r in rows)}
    return {
        "run_id": run_id, "tag": tag,
        "incidents_total": svc.count("incident", f"correlation_id={tag}"),
        "detection": {"patterns": det, "planted_found": sum(1 for d in det.values() if d["planted"] and d["problem"]),
                      "planted_total": planted_total,
                      "decoy_raised": any(d["problem"] for d in det.values() if not d["planted"]),
                      "unplanted_problems": [r["number"] for r in rows if not r["pattern"]]},
        "rca": {"matched": sum(1 for r in rows if r["rca_match"]), "planted_total": planted_total,
                "kb_matched": sum(1 for r in rows if r["kb_match"])},
        "known_errors": sum(1 for r in rows if r["known_error"]),
        "articles": {"total": len(kb_all), "published": sum(1 for a in kb_all if a["workflow_state"] == "published")},
        "changes": {"raised": len(changes), "normal": sum(1 for c in changes if c["type"] == "normal"),
                    "closed_successful": sum(1 for c in changes if c["state"] == config.choice_value("change_request.state", "Closed")
                                             and c["close_code"] == "successful")},
        "closure": {"closed": len(closed_rows), "open": len(rows) - len(closed_rows),
                    "closed_fix_applied": sum(1 for r in closed_rows if r["resolution_code"] == "fix_applied")},
        "median_hours_in_state": per_state, "observed_days_before_changes": round(observed_days, 1),
        "prevented_estimate_total": round(sum(r["verification"]["prevented_estimate"] for r in closed_rows if "verification" in r), 1),
        "problems": rows,
    }


# --- markdown ------------------------------------------------------------------------------------
def markdown(m):
    pct = lambda x: f"{x:.0%}"
    rows = m["problems"]
    out = [f"# Problem Management run report — {m['run_id']}", "",
           f"Run tag `{m['tag']}` · generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · "
           f"every number below is recomputed from the PDI (`python reporting_agent.py --check {m['run_id']}`).", "",
           "## Summary", "",
           f"- Planted problems detected: **{m['detection']['planted_found']} of {m['detection']['planted_total']}**; "
           f"decoy raised as a problem: **{'yes' if m['detection']['decoy_raised'] else 'no'}**; "
           f"unplanted problems opened: {len(m['detection']['unplanted_problems'])} ({', '.join(m['detection']['unplanted_problems']) or 'none'})",
           f"- Root causes matching the answer key: **{m['rca']['matched']} of {m['rca']['planted_total']}** "
           f"(known error articles: {m['rca']['kb_matched']} of {m['rca']['planted_total']})",
           f"- Known errors: **{m['known_errors']}**, articles published {m['articles']['published']} of {m['articles']['total']}",
           f"- Changes raised: **{m['changes']['raised']}** (Normal {m['changes']['normal']}, closed Successful {m['changes']['closed_successful']})",
           f"- Problems closed: **{m['closure']['closed']}** (Fix Applied {m['closure']['closed_fix_applied']}), kept open: {m['closure']['open']}",
           f"- Estimated recurring incidents prevented: **{m['prevented_estimate_total']}** (closed problems, verification window; see below)",
           f"- Incidents in the run: {m['incidents_total']}", "",
           "## Detected vs planted", "", "| Pattern | CI | Planted incidents | Problem | Recall | Precision |", "|---|---|---|---|---|---|"]
    for pid, d in m["detection"]["patterns"].items():
        out.append(f"| {pid}{'' if d['planted'] else ' (decoy)'} | {d['ci']} | {d['incidents']} | {d['problem'] or 'not raised'} | "
                   f"{pct(d['recall']) if d['problem'] else '-'} | {pct(d['precision']) if d['problem'] else '-'} |")
    out += ["", "## Problems", "",
            "| Problem | Pattern | Linked incidents | State | RCA vs key | Known error | Change | Resolution |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        rca = "match" if r["rca_match"] else ("miss" if r["pattern"] else "not scored")
        out.append(f"| {r['number']} | {r['pattern'] or 'unplanted'} | {r['linked_incidents']} | {r['state']} | {rca} | "
                   f"{r['article'] or '-'} ({r['article_state'] or '-'}) | {r['change'] or '-'} ({r['change_state'] or '-'}, "
                   f"{r['change_close_code'] or '-'}) | {r['resolution_code'] or '-'} |")
    out += ["", "## Incidents before and after the change", "",
            "Linked incidents before the change vs similar incidents in the post-change verification window "
            "(simulated dates after the changes).", "", "```mermaid", "xychart-beta",
            '    title "Incidents per problem: before vs after the change"',
            "    x-axis [" + ", ".join(f'"{r["number"]}"' for r in rows) + "]",
            '    y-axis "Incidents"',
            "    bar [" + ", ".join(str(r["linked_incidents"]) for r in rows) + "]",
            "    bar [" + ", ".join(str(r.get("verification", {}).get("similar_in_window", 0)) for r in rows) + "]",
            "```", "", "First bar: linked incidents before the change. Second bar: similar incidents in the window.", "",
            "## Estimated recurring incidents prevented", "",
            f"Estimate = incident rate before the change (linked incidents ÷ {m['observed_days_before_changes']} days of data "
            "before the changes) × window days − similar incidents seen in the window. It assumes the pattern would have "
            "continued at its average rate; it is an estimate, not a measurement.", "",
            "| Problem | Window | Rate before (/day) | Expected without fix | Similar in window | Prevented (est.) |",
            "|---|---|---|---|---|---|"]
    for r in rows:
        v = r.get("verification")
        if v:
            out.append(f"| {r['number']}{'' if r['state'] == 'Closed' else ' (kept open)'} | {v['window']} | {v['rate_per_day_before']} | "
                       f"{v['expected_without_fix']} | {v['similar_in_window']} | "
                       f"{v['prevented_estimate'] if r['state'] == 'Closed' else 'not counted'} |")
    out += ["", "## Time in each problem state (hours)", "",
            "From the audit history of `problem.state`. These reflect the pace of this project's build and gate runs, "
            "not real operations. Time in Closed is not counted (it ends the lifecycle).", "",
            "| Problem | " + " | ".join(STATES) + " |", "|---|" + "---|" * len(STATES)]
    for r in rows:
        out.append(f"| {r['number']} | " + " | ".join(str(r["hours_in_state"].get(s, "-")) for s in STATES) + " |")
    out.append("| **median** | " + " | ".join(str(m["median_hours_in_state"].get(s, "-")) for s in STATES) + " |")
    out += ["", "## Human gate decisions (from the problems' work notes)", ""]
    for r in rows:
        out.append(f"- **{r['number']}**: " + "; ".join(r["gate_decisions"]))
    return "\n".join(out) + "\n"


# --- check -----------------------------------------------------------------------------------------
def check(run_id):
    """Recompute from the PDI and compare with reports/<run_id>.json; add independent direct counts."""
    path = REPORTS / f"{run_id}.json"
    if not path.exists():
        print(f"{LOG} FAIL  {path.relative_to(config.ROOT)} missing: run `python reporting_agent.py --run-id {run_id}`")
        return False
    saved, fresh, ok = json.loads(path.read_text()), compute(run_id), True
    print(f"{LOG} CHECK (recomputed from the PDI)")

    def flat(d, prefix=""):
        if isinstance(d, dict):
            for k, v in d.items():
                yield from flat(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(d, list):
            for i, v in enumerate(d):
                yield from flat(v, f"{prefix}[{i}]")
        else:
            yield prefix, d

    volatile = re.compile(r"hours_in_state|median_hours")  # open problems keep accruing time
    now_vals = dict(flat(fresh))
    diffs = [(k, v, now_vals.get(k)) for k, v in flat(saved) if not volatile.search(k) and now_vals.get(k) != v]
    good = not diffs
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  every report number matches a fresh recomputation ({len(list(flat(saved)))} values)"
          + "".join(f"\n        {k}: report {a!r} vs PDI {b!r}" for k, a, b in diffs[:10]))
    # independent direct counts (stats API), not through compute()
    sn, tag = SNClient(), config.tag(run_id)
    st = lambda l: config.choice_value("problem.state", l)
    direct = {
        "problems closed": (saved["closure"]["closed"], sn.count("problem", f"correlation_id={tag}^state={st('Closed')}")),
        "known errors": (saved["known_errors"], sn.count("problem", f"correlation_id={tag}^known_error=true")),
        "changes raised": (saved["changes"]["raised"], sn.count("change_request", f"correlation_id={tag}")),
        "changes closed successful": (saved["changes"]["closed_successful"], sn.count(
            "change_request", f"correlation_id={tag}^state={config.choice_value('change_request.state', 'Closed')}^close_code=successful")),
        "articles published": (saved["articles"]["published"], sn.count(
            "kb_knowledge", f"short_descriptionSTARTSWITH[{config.TAG_PREFIX}]^workflow_state=published")),
        "incidents": (saved["incidents_total"], sn.count("incident", f"correlation_id={tag}")),
    }
    for p in saved["problems"]:
        prb = sn.get("problem", f"number={p['number']}", fields=["sys_id"])[0]
        direct[f"{p['number']} linked incidents"] = (p["linked_incidents"], sn.count("incident", f"{config.INCIDENT_PROBLEM_LINK}={prb['sys_id']}"))
    for name, (rep, pdi) in direct.items():
        good = rep == pdi
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name}: report {rep}, PDI {pdi}")
    md = (REPORTS / f"{run_id}.md").read_text() if (REPORTS / f"{run_id}.md").exists() else ""
    good = all(s in md for s in ("Detected vs planted", "Root causes matching", "Linked incidents", "Known errors",
                                 "Changes raised", "Time in each problem state", "recurring incidents prevented", "```mermaid"))
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  markdown report has all sections from the spec and the chart")
    print(f"{LOG} CHECK RESULT: {'ALL PASSED' if ok else 'FAILED'}")
    return ok


def run(context):
    m = compute(context["run_id"])
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"{m['run_id']}.json").write_text(json.dumps(m, indent=2))
    (REPORTS / f"{m['run_id']}.md").write_text(markdown(m))
    print(f"{LOG} wrote reports/{m['run_id']}.md and reports/{m['run_id']}.json")
    return m


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id")
    ap.add_argument("--check", metavar="RUN_ID")
    args = ap.parse_args()
    try:
        if args.check:
            sys.exit(0 if check(args.check) else 1)
        if not args.run_id:
            ap.error("--run-id is required")
        run({"run_id": args.run_id})
        sys.exit(0 if check(args.run_id) else 1)
    except SNError as e:
        sys.exit(f"{LOG} ERROR: {e}")


if __name__ == "__main__":
    main()
