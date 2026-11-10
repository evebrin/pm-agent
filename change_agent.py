"""Change & Closure Agent, part 1: permanent-fix changes with Human Gate 3.

For each problem with a known error (Root Cause Analysis, Known Error article, no change yet) it drafts a
Normal change for the permanent fix (LLM with a template fallback; the human reviews the proposed fix at
Gate 3, which comes BEFORE the change is created). On approval it creates the change, links it to the
problem (rfc) with fix notes and moves the problem to Fix in Progress, records the Gate 3 decision on the
change's ServiceNow approval records (assignment group, then CAB) using the PDI admin account, and
simulates implementation: Scheduled -> Implement -> Review -> Closed (Successful).

Part 2 (--close): once every known-error problem has a Gate 3 decision, the Synthetic Data Agent seeds a
post-change window of incidents (simulated dates after the changes, long enough to cover each pattern's next
expected occurrence). The agent then checks each problem for recurrence (new incidents on its CI or service,
in its main subcategories, textually similar to its own incidents) and asks Human Gate 4 to close it:
Fix in Progress -> Resolved (fix_applied, close notes) -> Closed, resolving its open incidents.

Runs as pm_agent_svc, except the approval records, which are approved as admin (the student's own PDI
account) with a comment naming the Gate 3 approver.

    python change_agent.py --run-id <id> [--dry-run] [--auto-approve] [--engine llm|fallback] [--problem PRB...]
    python change_agent.py --run-id <id> --close [--dry-run] [--auto-approve] [--problem PRB...]
    python change_agent.py --check <run_id>
"""
import argparse
import getpass
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import config
import llm
import rca_agent
import state_api
import synthetic_data_agent
from detection_agent import centroid, cosine, tfidf
from sn_client import SNClient, SNError

LOG = "[change]"
FMT = "%Y-%m-%d %H:%M:%S"
TRANSIENT = ("dry_run", "auto_approve", "engine", "problems", "ask")
CHG = "change_request"
WAIT = 90  # seconds to wait for the platform's change flows (approvals, auto transitions)


def cv(field, label):
    return config.choice_value(field, label)


def now():
    return datetime.now(timezone.utc)


def accepted(p):
    import html
    return html.unescape(p["cause_notes"].split("\n\nMethod:")[0]).strip(), html.unescape(p["workaround"]).strip()


# --- drafting --------------------------------------------------------------------------------------
def fallback(p, ci):
    cause, workaround = accepted(p)
    return {"short_description": f"Permanent fix for {p['number']} on {ci}",
            "fix_summary": f"Remove the root cause on {ci}: {cause}",
            "description": f"Permanent fix for problem {p['number']} ({p['short_description']}).\n\nRoot cause: {cause}",
            "justification": f"Recurring incidents on {ci}; the current workaround ({workaround}) only restores "
                             "service temporarily.",
            "risk": "Moderate", "risk_reason": "Template default: production CI, change not yet assessed in detail.",
            "impact": "2 - Medium",
            "implementation_plan": f"1. Confirm the root cause on {ci}.\n2. Apply the corrective configuration that removes it.\n"
                                   "3. Monitor the service for the next expected occurrence.",
            "backout_plan": f"Revert the configuration change on {ci} and keep using the workaround: {workaround}",
            "test_plan": "Verify service health after the change and confirm no new incidents of this pattern in the "
                         "verification window."}


SYSTEM = ("You are a change manager drafting a ServiceNow Normal change request for a permanent fix. Base it only on "
          "the accepted root cause and workaround given; you may propose the corrective action that removes that "
          "cause, but do not invent other causes, systems or facts. Answer with one JSON object only.")
SCHEMA = """{"short_description": "one line: what is changed on which CI",
 "fix_summary": "1-2 sentences: the permanent fix (this becomes the problem's Fix notes)",
 "description": "what will be changed and why (refer to the problem number)",
 "justification": "why the change is needed (recurring incidents, workaround only temporary)",
 "risk": "Low" | "Moderate" | "High", "risk_reason": "one sentence",
 "impact": "1 - High" | "2 - Medium" | "3 - Low",
 "implementation_plan": "numbered steps", "backout_plan": "numbered steps", "test_plan": "numbered steps"}"""


def with_llm(f):
    r = llm.complete_json(SYSTEM, "Draft the change from these facts.\n\nReturn JSON in this shape:\n"
                          f"{SCHEMA}\n\nFacts:\n{json.dumps(f, indent=1)}", max_tokens=2000)
    need = ("short_description", "fix_summary", "implementation_plan", "backout_plan", "test_plan")
    if not all(isinstance(r.get(k), str) and r[k].strip() for k in need):
        raise llm.LLMUnavailable("LLM change draft is missing a required field")
    if r.get("risk") not in ("Low", "Moderate", "High"):
        r["risk"] = "Moderate"
    if r.get("impact") not in [c["label"] for c in config.load_instance()["choices"]["change_request.impact"]]:
        r["impact"] = "2 - Medium"
    for k in ("description", "justification", "risk_reason"):
        r[k] = str(r.get(k) or "")
    return r


def draft(p, ci, engine):
    cause, workaround = accepted(p)
    f = {"problem": p["number"], "problem_title": p["short_description"], "ci": ci,
         "accepted_root_cause": cause, "accepted_workaround": workaround}
    if engine == "llm":
        try:
            d = with_llm(f)
            d["engine"] = f"llm {llm.describe()}"
            return d
        except llm.LLMUnavailable as e:
            print(f"{LOG} LLM unavailable for {p['number']} ({e}); using deterministic fallback")
            d = fallback(p, ci)
            d["engine"] = f"fallback (LLM unavailable: {e})"
            return d
    d = fallback(p, ci)
    d["engine"] = "fallback"
    return d


# --- Human Gate 3 ----------------------------------------------------------------------------------
def show(p, d):
    print(f"\n=== {p['number']}  {p['short_description']}")
    print(f"    change: [{config.TAG_PREFIX}] {d['short_description']}   (draft by {d['engine']})")
    print(f"    FIX: {d['fix_summary']}")
    print(f"    risk: {d['risk']} - {d['risk_reason']}   impact: {d['impact']}")
    for k in ("implementation_plan", "backout_plan", "test_plan"):
        print(f"    {k.replace('_', ' ').upper()}:\n      " + d[k].replace("\n", "\n      "))


def gate(p, d, auto, ask=input):
    e = {"gate": 3, "problem": p["number"], "by": getpass.getuser(), "auto": auto, "engine": d["engine"], "changes": {}}
    show(p, d)
    if auto:
        e["decision"] = "approve"
        print("    [auto-approve] approve")
    while "decision" not in e:
        try:
            a = ask("    Decision [y]approve and create the change [e]dit [n]reject: ").strip().lower()
        except EOFError:
            raise SystemExit(f"{LOG} no input available; use --auto-approve for unattended rehearsal runs")
        if a == "y":
            e["decision"] = "approve"
        elif a == "n":
            e["decision"] = "reject"
            e["reason"] = ask("    reason for rejecting: ").strip()
        elif a == "e":
            for k in ("short_description", "fix_summary", "risk"):
                new = ask(f"    {k} [Enter keeps current]: ").strip()
                if k == "risk" and new and new.capitalize() not in ("Low", "Moderate", "High"):
                    print("    risk must be Low, Moderate or High")
                    continue
                if new:
                    new = new.capitalize() if k == "risk" else new
                    e["changes"][k] = [d[k], new]
                    d[k] = new
            show(p, d)
    e["at"] = now().isoformat(timespec="seconds")
    return e


# --- ServiceNow change lifecycle --------------------------------------------------------------
def wait_for(sn, sys_id, label, seconds=WAIT):
    want = cv("change_request.state", label)
    for _ in range(seconds):
        r = sn.get(CHG, f"sys_id={sys_id}", fields=["state", "approval"])[0]
        if r["state"] == want:
            return True
        time.sleep(1)
    return False


def approve_round(admin, change, label, e):
    """Approve one requested approval record for the change (a group approval needs only one) as admin."""
    for _ in range(WAIT):
        recs = admin.get_all("sysapproval_approver", f"sysapproval={change['sys_id']}^state=requested",
                             fields=["sys_id", "approver", "group"])
        if recs:
            break
        time.sleep(1)
    else:
        raise SNError(f"{change['number']}: no {label} approval was requested within {WAIT}s")
    r = recs[0]
    group = admin.get("sysapproval_group", f"sys_id={r['group']}", fields=["assignment_group"], display_value=True) if r["group"] else []
    who = admin.get("sys_user", f"sys_id={r['approver']}", fields=["name"])[0]["name"]
    admin.update("sysapproval_approver", r["sys_id"], {
        "state": "approved",
        "comments": f"Approved at Human Gate 3 by {e['by']} at {e['at']} (recorded with the PDI admin account; "
                    f"{label} round)."})
    return {"round": label, "approval": r["sys_id"], "approver": who,
            "group": group[0]["assignment_group"] if group else ""}


def simulate(svc, change):
    """Scheduled -> Implement -> Review -> Closed, closing any change tasks the platform created."""
    start = now()
    svc.update(CHG, change["sys_id"], {"state": cv("change_request.state", "Implement"), "work_start": start.strftime(FMT)})
    if not svc.dry_run and not wait_for(svc, change["sys_id"], "Implement", 10):
        raise SNError(f"{change['number']}: did not move to Implement")
    time.sleep(0 if svc.dry_run else 3)  # the Implement flow may create change tasks
    for t in ([] if svc.dry_run else svc.get_all("change_task", f"change_request={change['sys_id']}^active=true", fields=["sys_id", "number"])):
        svc.update("change_task", t["sys_id"], {"state": cv("change_task.state", "Closed"), "close_code": "successful",
                                               "close_notes": "Simulated implementation (PMSYNTH)."})
    end = now()
    svc.update(CHG, change["sys_id"], {"state": cv("change_request.state", "Review"), "work_end": end.strftime(FMT)})
    if not svc.dry_run and not wait_for(svc, change["sys_id"], "Review", 10):
        raise SNError(f"{change['number']}: did not move to Review")
    svc.update(CHG, change["sys_id"], {"state": cv("change_request.state", "Closed"),
                                       "close_code": cv("change_request.close_code", "Successful"),
                                       "close_notes": "Simulated implementation completed successfully (PMSYNTH synthetic run). "
                                                      "Effectiveness is verified separately in a post-change window."})
    if not svc.dry_run and not wait_for(svc, change["sys_id"], "Closed", 10):
        raise SNError(f"{change['number']}: did not move to Closed")
    return {"work_start": start.isoformat(timespec="seconds"), "work_end": end.isoformat(timespec="seconds")}


def run(context):
    dry, tag = context.get("dry_run", False), context["tag"]
    engine = context.get("engine") or ("llm" if llm.available() else "fallback")
    svc = SNClient(dry_run=dry)
    admin = SNClient(dry_run=dry, user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
    print(f"{LOG} run {context['run_id']} as {svc.session.auth[0]} dry_run={dry} engine={engine}"
          f"{' ' + llm.describe() if engine == 'llm' else ''}")
    rca = cv("problem.state", "Root Cause Analysis")
    only = set(context.get("problems") or [])
    problems = svc.get_all("problem", f"correlation_id={tag}", fields=[
        "sys_id", "number", "short_description", "state", "cmdb_ci", "business_service", "assignment_group",
        "cause_notes", "workaround", "known_error", "primary_known_error_article", "rfc", "impact", "urgency"])
    ci_names = {r["sys_id"]: r["name"] for r in svc.get_all("cmdb_ci", f"correlation_id={tag}", fields=["sys_id", "name"])}
    todo = sorted((p for p in problems if p["state"] == rca and p["known_error"] == "true" and p["primary_known_error_article"]
                   and not p["rfc"] and (not only or p["number"] in only)), key=lambda p: p["number"])
    print(f"{LOG} {len(todo)} known-error problems without a change")
    out = context.setdefault("change", {}).setdefault("problems", {}) if not dry else {}
    for p in todo:
        ci = ci_names.get(p["cmdb_ci"], "the CI")
        d = draft(p, ci, engine)
        e = gate(p, d, context.get("auto_approve", False), context.get("ask", input))
        if e["decision"] != "approve":
            svc.update("problem", p["sys_id"], {"work_notes": f"Human Gate 3: change rejected by {e['by']} at {e['at']}. "
                                                              f"Reason: {e.get('reason', '')}. No change created."})
            print(f"{LOG} {p['number']}: rejected, no change created")
            if not dry:
                context.setdefault("log", []).append(e)
                out[p["number"]] = {"decision": "reject", "at": e["at"]}
                config.save_context({k: v for k, v in context.items() if k not in TRANSIENT})
            continue
        start = now() + timedelta(minutes=5)
        change = svc.create(CHG, {
            "short_description": f"[{config.TAG_PREFIX}] {d['short_description']}", "correlation_id": tag,
            "type": cv("change_request.type", "Normal"), "cmdb_ci": p["cmdb_ci"], "business_service": p["business_service"],
            "assignment_group": p["assignment_group"], "risk": cv("change_request.risk", d["risk"]),
            "impact": cv("change_request.impact", d["impact"]), "description": d["description"],
            "justification": d["justification"], "implementation_plan": d["implementation_plan"],
            "backout_plan": d["backout_plan"], "test_plan": d["test_plan"],
            "risk_impact_analysis": d["risk_reason"], "start_date": start.strftime(FMT),
            "end_date": (start + timedelta(hours=2)).strftime(FMT),
            "work_notes": f"Raised for problem {p['number']}. Human Gate 3: approved by {e['by']} at {e['at']}"
                          f"{' (auto-approve rehearsal)' if e['auto'] else ''}. Draft by {d['engine']}."
                          + (f" Changes: {json.dumps(e['changes'])}" if e["changes"] else "")})
        change.setdefault("number", "DRYRUN")
        print(f"{LOG} {p['number']}: created {change['number']}")
        # problem: link the change, record the planned fix, move to Fix in Progress (needs cause + fix notes)
        svc.update("problem", p["sys_id"], {"rfc": change["sys_id"]})
        if not dry and svc.get("problem", f"sys_id={p['sys_id']}", fields=["rfc"])[0]["rfc"] != change["sys_id"]:
            raise SNError(f"{p['number']}: the rfc link to {change['number']} did not save")
        state_api.move(svc, p, "Fix in Progress", {
            "fix_notes": f"{d['fix_summary']}\n\nChange: {change['number']}.",
            "work_notes": f"Human Gate 3: change {change['number']} approved by {e['by']} at {e['at']}. "
                          f"Planned fix: {d['fix_summary']}"})
        # change: New -> Assess, then the platform asks for approvals; record the Gate 3 decision on them
        approvals = []
        svc.update(CHG, change["sys_id"], {"state": cv("change_request.state", "Assess")})
        if not dry:
            approvals.append(approve_round(admin, change, "assignment group", e))
            if not wait_for(svc, change["sys_id"], "Authorize"):
                raise SNError(f"{change['number']}: did not reach Authorize after the assignment group approval")
            approvals.append(approve_round(admin, change, "CAB", e))
            if not wait_for(svc, change["sys_id"], "Scheduled"):
                raise SNError(f"{change['number']}: did not reach Scheduled after the CAB approval")
        else:
            print(f"[dry-run] APPROVE assignment group round, then CAB round, as admin with a Gate 3 comment")
        times = simulate(svc, change)
        print(f"{LOG} {p['number']}: {change['number']} approved ({', '.join(a['round'] + ': ' + a['approver'] for a in approvals) or 'dry run'}), "
              f"implemented and closed; problem in Fix in Progress")
        e.update(change=change["number"], approvals=approvals)
        if not dry:
            context.setdefault("log", []).append(e)
            out[p["number"]] = {"decision": "approve", "change": change["number"], "change_sys_id": change["sys_id"],
                                "engine": d["engine"], "risk": d["risk"], "fix_summary": d["fix_summary"],
                                "approvals": approvals, **times, "at": e["at"]}
            config.save_context({k: v for k, v in context.items() if k not in TRANSIENT})
    return out


# --- part 2: verification window and Human Gate 4 ---------------------------------------------
WINDOW_DAYS = 14            # minimum post-change window: covers two weekly cycles
MAIN_SUBCATEGORY_SHARE = 0.20  # a subcategory is part of the pattern when >= this share of its incidents


def _dt(s):
    return datetime.fromisoformat(s) if "T" in s else datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def window_end(start, patterns):
    """Long enough to include each pattern's next expected occurrence (weekly -> 14 days; month-end -> next one)."""
    end = start + timedelta(days=WINDOW_DAYS)
    if any("last business day" in t for t in patterns):
        nxt = synthetic_data_agent._last_business_days(start, start + timedelta(days=62))
        if nxt:
            end = max(end, nxt[0].replace(hour=0, minute=0, second=0) + timedelta(days=1, hours=12))
    return end


def recurrence(svc, p, start, end):
    """New incidents in the window that look like this problem's pattern (same CI/service, main subcategory,
    TF-IDF cosine to the problem's incidents >= the detection threshold)."""
    link = config.INCIDENT_PROBLEM_LINK
    fields = ["number", "short_description", "subcategory", "cmdb_ci", "business_service", "opened_at"]
    linked = svc.get_all("incident", f"{link}={p['sys_id']}", fields=fields)
    subs = {k for k, v in Counter(i["subcategory"] for i in linked).items() if v / len(linked) >= MAIN_SUBCATEGORY_SHARE}
    window = svc.get_all("incident", f"correlation_id={p['correlation_id']}^{link}ISEMPTY^opened_at>={start.strftime(FMT)}"
                                     f"^opened_at<{end.strftime(FMT)}", fields=fields)
    near = [i for i in window if i["cmdb_ci"] == p["cmdb_ci"] or (p["business_service"] and i["business_service"] == p["business_service"])]
    vecs = tfidf([i["short_description"] for i in linked + near])
    c = centroid(vecs[:len(linked)])
    matches = []
    for i, v in zip(near, vecs[len(linked):]):
        sim = cosine(v, c)
        if i["subcategory"] in subs and sim >= config.DETECT_MIN_SIMILARITY:
            matches.append({"number": i["number"], "opened_at": i["opened_at"], "short_description": i["short_description"],
                            "similarity": round(sim, 2)})
    return {"start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
            "window_incidents": len(window), "on_ci_or_service": len(near), "main_subcategories": sorted(subs),
            "similar": matches, "clean": not matches}


def gate4(p, c, v, auto, ask=input):
    e = {"gate": 4, "problem": p["number"], "by": getpass.getuser(), "auto": auto, "change": c["number"],
         "clean": v["clean"], "changes": {}}
    print(f"\n=== {p['number']}  {p['short_description']}")
    print(f"    change: {c['number']} state={c['state_label']} close code={c['close_code']}  work end {c['work_end']}")
    print(f"    verification window {v['start'][:10]} to {v['end'][:10]}: {v['window_incidents']} new incidents, "
          f"{v['on_ci_or_service']} on this CI/service, main subcategories {', '.join(v['main_subcategories'])}")
    if v["clean"]:
        print("    similar incidents: none -> the pattern did not recur")
    else:
        print(f"    similar incidents: {len(v['similar'])} -> the pattern RECURRED")
        for m in v["similar"][:8]:
            print(f"      {m['number']}  {m['opened_at'][:10]}  sim {m['similarity']}  {m['short_description']}")
    notes = (f"Permanent fix implemented by {c['number']} (closed {c['close_code']}). Verification window "
             f"{v['start'][:10]} to {v['end'][:10]}: no recurrence of the pattern.")
    if not v["clean"]:
        notes = notes.replace("no recurrence of the pattern.", f"{len(v['similar'])} similar incidents recurred.")
    print(f"    close notes: {notes}")
    print(f"    recommendation: {'CLOSE' if v['clean'] else 'DO NOT CLOSE (pattern recurred)'}")
    if auto:
        e["decision"] = "close" if v["clean"] else "keep_open"
        print(f"    [auto-approve] {e['decision']}")
    while "decision" not in e:
        try:
            a = ask("    Decision [y]close the problem [e]dit close notes [n]keep open: ").strip().lower()
        except EOFError:
            raise SystemExit(f"{LOG} no input available; use --auto-approve for unattended rehearsal runs")
        if a == "y":
            e["decision"] = "close"
            if not v["clean"]:
                e["override"] = "closed although the verification found a recurrence"
        elif a == "n":
            e["decision"] = "keep_open"
            e["reason"] = ask("    reason for keeping it open: ").strip()
        elif a == "e":
            new = ask("    close notes [Enter keeps current]: ").strip()
            if new:
                e["changes"]["close_notes"] = [notes, new]
                notes = new
            print(f"    close notes: {notes}")
    e["close_notes"] = notes
    e["at"] = now().isoformat(timespec="seconds")
    return e


def close(context):
    dry, tag = context.get("dry_run", False), context["tag"]
    svc = SNClient(dry_run=dry)
    print(f"{LOG} closure run {context['run_id']} as {svc.session.auth[0]} dry_run={dry}")
    ch = context.get("change", {}).get("problems", {})
    problems = svc.get_all("problem", f"correlation_id={tag}", fields=[
        "sys_id", "number", "short_description", "state", "cmdb_ci", "business_service", "rfc", "known_error",
        "correlation_id"])
    pending = [p["number"] for p in problems if p["known_error"] == "true" and p["number"] not in ch]
    if pending:
        raise SystemExit(f"{LOG} Gate 3 still needed for {', '.join(pending)}: run change_agent.py --run-id {context['run_id']} first")
    closed_chg = cv("change_request.state", "Closed")
    changes = {}
    for p in problems:
        if p["rfc"]:
            c = svc.get(CHG, f"sys_id={p['rfc']}", fields=["number", "state", "close_code", "work_end"])[0]
            c["state_label"] = next(x["label"] for x in config.load_instance()["choices"]["change_request.state"] if x["value"] == c["state"])
            changes[p["number"]] = c
    implemented = {n for n, c in changes.items() if c["state"] == closed_chg and c["close_code"] == "successful"}
    # the window starts after the last implementation and covers each problem's next expected occurrence
    pc = context.get("synthetic", {}).get("post_change")
    if pc:
        start, end = _dt(pc["start"]), _dt(pc["end"])
    else:
        start = max((_dt(changes[n]["work_end"]) for n in implemented), default=now()).replace(microsecond=0)
        all_incidents, cis = rca_agent.load_world(svc, tag)
        patterns = []
        for p in problems:
            if p["number"] in implemented:
                patterns += rca_agent.gather(svc, p, tag, all_incidents, cis)["time_profile"]["patterns"]
        end = window_end(start, patterns)
        fixed_sets = [[i["number"] for i in svc.get_all("incident", f"{config.INCIDENT_PROBLEM_LINK}={p['sys_id']}", fields=["number"])]
                      for p in problems if p["number"] in implemented]
        pc = synthetic_data_agent.post_change(context, start, end, fixed_sets, dry=dry)
        if not dry:
            config.save_context({k: v for k, v in context.items() if k not in TRANSIENT})
    fip = cv("problem.state", "Fix in Progress")
    only = set(context.get("problems") or [])
    out = context.setdefault("closure", {}).setdefault("problems", {}) if not dry else {}
    for p in sorted(problems, key=lambda p: p["number"]):
        if p["number"] not in implemented or p["state"] != fip or (only and p["number"] not in only):
            continue
        v = recurrence(svc, p, start, end)
        e = gate4(p, changes[p["number"]], v, context.get("auto_approve", False), context.get("ask", input))
        note = (f"Human Gate 4: {e['decision'].replace('_', ' ')} by {e['by']} at {e['at']}"
                f"{' (auto-approve rehearsal)' if e['auto'] else ''}. Verification window {v['start'][:10]} to "
                f"{v['end'][:10]}: {'clean' if v['clean'] else str(len(v['similar'])) + ' similar incidents'}."
                + (f" Override: {e['override']}." if e.get("override") else "")
                + (f" Reason: {e['reason']}" if e.get("reason") else ""))
        if e["decision"] == "close":
            state_api.move(svc, p, "Resolved", {"resolution_code": cv("problem.resolution_code", "Fix Applied"), "close_notes": e["close_notes"],
                                                "work_notes": note})
            state_api.move(svc, p, "Closed", {})
            incs = svc.get_all("incident", f"{config.INCIDENT_PROBLEM_LINK}={p['sys_id']}^stateIN"
                                           f"{','.join(cv('incident.state', s) for s in ('New', 'In Progress', 'On Hold'))}",
                               fields=["sys_id", "number"])
            for i in incs:
                svc.update("incident", i["sys_id"], {
                    "state": cv("incident.state", "Resolved"),
                    "close_code": cv("incident.close_code", "Resolved – Fixed by Problem"),
                    "close_notes": f"Resolved by the permanent fix for {p['number']} ({changes[p['number']]['number']})."})
            e["incidents_resolved"] = [i["number"] for i in incs]
            print(f"{LOG} {p['number']}: Resolved -> Closed (fix_applied); {len(incs)} open incidents resolved")
        else:
            svc.update("problem", p["sys_id"], {"work_notes": note})
            print(f"{LOG} {p['number']}: kept open in Fix in Progress")
        if not dry:
            context.setdefault("log", []).append(e)
            out[p["number"]] = {"decision": e["decision"], "verification": v, "at": e["at"],
                                "incidents_resolved": e.get("incidents_resolved", [])}
            config.save_context({k: v for k, v in context.items() if k not in TRANSIENT})
    return out


def check_closure(context):
    admin = SNClient(user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
    ok, res = True, context.get("closure", {}).get("problems", {})
    pc = context.get("synthetic", {}).get("post_change")
    print(f"{LOG} CHECK part 2: closure (recomputed from the PDI)")
    if not pc:
        print("  FAIL  no post-change window")
        return False
    start, end = _dt(pc["start"]), _dt(pc["end"])
    n = admin.count("incident", f"correlation_id={context['tag']}^opened_at>={start.strftime(FMT)}^opened_at<{end.strftime(FMT)}")
    good = n == len(pc["incidents"])
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  post-change window {pc['start'][:10]} to {pc['end'][:10]}: {n} incidents in the PDI "
          f"({len(pc['incidents'])} seeded)")
    for num, r in sorted(res.items()):
        p = admin.get("problem", f"number={num}", fields=["sys_id", "state", "resolution_code", "close_notes", "rfc",
                                                          "cmdb_ci", "business_service", "correlation_id", "number",
                                                          "short_description"])[0]
        v = recurrence(admin, p, start, end)
        if r["decision"] == "close":
            c = admin.get(CHG, f"sys_id={p['rfc']}", fields=["number", "state", "close_code"])[0] if p["rfc"] else {}
            good = (p["state"] == cv("problem.state", "Closed") and p["resolution_code"] == cv("problem.resolution_code", "Fix Applied")
                    and p["close_notes"].strip() != "" and c.get("state") == cv("change_request.state", "Closed")
                    and c.get("close_code") == "successful" and (v["clean"] or bool(r.get("override"))))
            done = {cv("incident.state", "Resolved"), cv("incident.state", "Closed")}
            for n in r.get("incidents_resolved", []):
                good &= admin.get("incident", f"number={n}", fields=["state"])[0]["state"] in done
            print(f"  {'PASS' if good else 'FAIL'}  {num} Closed fix_applied, change {c.get('number')} {c.get('close_code')}, "
                  f"window {'clean' if v['clean'] else str(len(v['similar'])) + ' similar'}, "
                  f"open incidents resolved: {len(r.get('incidents_resolved', []))}")
        else:
            good = p["state"] == cv("problem.state", "Fix in Progress")
            print(f"  {'PASS' if good else 'FAIL'}  {num} kept open (Fix in Progress); window "
                  f"{'clean' if v['clean'] else str(len(v['similar'])) + ' similar incidents'}")
        ok &= good
    decisions = [e for e in context.get("log", []) if e.get("gate") == 4]
    good = bool(decisions) and all({"by", "at", "decision"} <= e.keys() for e in decisions)
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {len(decisions)} gate-4 decisions logged with who/when/what")
    print(f"{LOG} CHECK RESULT (closure): {'ALL PASSED' if ok else 'FAILED'}")
    return ok


# --- success check -------------------------------------------------------------------------
def check(context):
    admin = SNClient(user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
    ok, res = True, context.get("change", {}).get("problems", {})
    fip = cv("problem.state", "Fix in Progress")
    after_fix = {fip, cv("problem.state", "Resolved"), cv("problem.state", "Closed")}  # later states once Gate 4 ran
    print(f"{LOG} CHECK part 1 (recomputed from the PDI)")
    for n, r in sorted(res.items()):
        p = admin.get("problem", f"number={n}", fields=["sys_id", "state", "rfc", "fix_notes"])[0]
        if r["decision"] != "approve":
            good = not p["rfc"]
            print(f"  {'PASS' if good else 'FAIL'}  {n} rejected at Gate 3: no change linked")
            ok &= good
            continue
        c = admin.get(CHG, f"sys_id={p['rfc']}", fields=["number", "type", "state", "close_code", "correlation_id",
                                                          "implementation_plan", "backout_plan", "test_plan", "work_start", "work_end"])
        c = c[0] if c else {}
        appr = admin.get_all("sysapproval_approver", f"sysapproval={p['rfc']}^state=approved", fields=["sys_id"])
        noted = sum(admin.count("sys_journal_field", f"element_id={a['sys_id']}^element=comments^valueLIKEHuman Gate 3") for a in appr)
        good = (c.get("type") == "normal" and c.get("correlation_id") == context["tag"]
                and c.get("state") == cv("change_request.state", "Closed") and c.get("close_code") == "successful"
                and all(c.get(k, "").strip() for k in ("implementation_plan", "backout_plan", "test_plan", "work_start", "work_end"))
                and noted >= 2 and p["state"] in after_fix and p["fix_notes"].strip() != "")
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {n} -> {c.get('number', '-')} state={c.get('state')} close={c.get('close_code')} "
              f"approvals with Gate 3 comment={noted}  problem state={p['state']} fix notes={bool(p['fix_notes'].strip())}")
    decisions = [e for e in context.get("log", []) if e.get("gate") == 3]
    good = bool(decisions) and all({"by", "at", "decision"} <= e.keys() for e in decisions)
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {len(decisions)} gate-3 decisions logged with who/when/what")
    print(f"{LOG} CHECK RESULT: {'ALL PASSED' if ok else 'FAILED'}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--auto-approve", action="store_true", help="rehearsal only: approve every drafted change")
    ap.add_argument("--engine", choices=["llm", "fallback"], help="default: llm when configured, else fallback")
    ap.add_argument("--problem", nargs="+", metavar="PRB")
    ap.add_argument("--close", action="store_true", help="part 2: verification window and Human Gate 4")
    ap.add_argument("--check", metavar="RUN_ID")
    args = ap.parse_args()
    try:
        if args.check:
            ctx = config.load_context(args.check)
            ok = check(ctx)
            if ctx.get("closure"):
                ok = check_closure(ctx) and ok
            sys.exit(0 if ok else 1)
        if not args.run_id:
            ap.error("--run-id is required")
        ctx = config.load_context(args.run_id)
        ctx.update(dry_run=args.dry_run, auto_approve=args.auto_approve, engine=args.engine, problems=args.problem)
        (close if args.close else run)(ctx)
        if not args.dry_run:
            for k in TRANSIENT:
                ctx.pop(k, None)
            config.save_context(ctx)
            print(f"{LOG} context saved to runs/{ctx['run_id']}.json")
            sys.exit(0 if (check_closure(ctx) if args.close else check(ctx)) else 1)
    except SNError as e:
        sys.exit(f"{LOG} ERROR: {e}")


if __name__ == "__main__":
    main()
