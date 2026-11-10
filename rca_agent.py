"""Root Cause Analysis Agent with Human Gate 2.

For each problem from detection (state New or Root Cause Analysis) it gathers evidence from the PDI:
linked incidents and their work notes, CI attributes, same-class CIs for comparison, changes on the CI,
time and close-code profiles. It then runs 5 Whys, or Kepner-Tregoe "is / is not" when the evidence
is ambiguous, through the LLM (llm.py) with a deterministic rule-based fallback. A human accepts,
edits, sends back or rejects each RCA. Accepted RCAs fill cause_notes and workaround and move the
problem New -> Assess -> Root Cause Analysis (Fix in Progress follows in phase 6, with the fix notes).

Runs as pm_agent_svc. The answer key is read only by evaluation, never by the analysis.

    python rca_agent.py --run-id <id> [--dry-run] [--auto-approve] [--engine llm|fallback] [--problem PRB...]
    python rca_agent.py --run-id <id> --compare      # both engines, scored, no gate, no writes
    python rca_agent.py --check <run_id>
"""
import argparse
import getpass
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import config
import llm
import state_api
from detection_agent import tokens
from sn_client import SNClient, SNError

LOG = "[rca]"
FMT = "%Y-%m-%d %H:%M:%S"
INC_FIELDS = ["sys_id", "number", "short_description", "opened_at", "priority", "caller_id", "category",
              "subcategory", "cmdb_ci", "close_code", "close_notes", "state"]
CI_ATTRS = {"cmdb_ci_netgear": ["firmware_version"], "cmdb_ci_server": ["os_version"]}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Rule thresholds, set before seeing results (assumptions 3 and 5).
AMBIGUOUS_CLUE_SHARE = 0.40   # Kepner-Tregoe when no single clue term is in at least this share of notes
TIME_PATTERN_SHARE = 0.70     # a weekday or month-end pattern needs this share of incidents
HIGH_CLUE_SHARE = 0.50        # High confidence needs one clue term in more than this share
MIN_EVIDENCE = 3              # High confidence needs at least this many valid incident numbers cited

# Fallback text cues. Identifiers: job names, paths, versions, host names.
ID_RE = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+|/[\w./-]+|\b\d+\.\d+\.(?:\d+|x)\b|\b[a-z]+(?:-[a-z0-9]+)*-\d+\b")
CAUSE_RE = re.compile(r"\b(bug|no rotation|not (?:restarted|reloaded|set up|affected)|holds? locks?|keeps growing|"
                      r"still presenting|filled up|because|caused by|since the)\b", re.I)
WORK_RE = re.compile(r"\b(workaround|restart(?:ed)?|clear(?:ed)?|purg(?:e|ed)|moved|retry|reconnect(?:ed)?|"
                     r"recovered|rebooted|flushed|reset)\b", re.I)

# Scoring vs the answer key (evaluation only), fixed before results: every group must match (any synonym).
SCORE_KEYWORDS = {
    "a": [["pg_log", "database log", "log director", "logs"], ["rotation", "rotate", "retention"]],
    "b": [["cert-renew-weekly", "certificate renewal", "cert renewal", "renewal job"], ["proxy"],
          ["restart", "reload"]],
    "c": [["7.2.1", "7.2.x"], ["vpn-gw-01"], ["firmware"]],
    "d": [["payroll-monthend", "month-end", "monthend"], ["batch"], ["lock"]],
}


# --- evidence ------------------------------------------------------------------------------------
def _notes(text):
    """Work-note history text -> list of note bodies (headers stripped)."""
    parts = re.split(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d - .*\(Work notes\)\n", text or "", flags=re.M)
    return [p.strip() for p in parts if p.strip()]


def _last_business_day(d):
    nxt = d + timedelta(days=1)
    while nxt.month == d.month:
        if nxt.weekday() < 5:
            return False
        nxt += timedelta(days=1)
    return d.weekday() < 5


def gather(svc, problem, tag, all_incidents, cis):
    """Everything the analysis may use, read from the PDI. Never the answer key."""
    linked = svc.get_all("incident", f"{config.INCIDENT_PROBLEM_LINK}={problem['sys_id']}", fields=INC_FIELDS)
    notes = {r["number"]: _notes(r["work_notes"]) for r in svc.get_all(
        "incident", f"{config.INCIDENT_PROBLEM_LINK}={problem['sys_id']}", fields=["number", "work_notes"], display_value=True)}
    close_labels = {c["value"]: c["label"] for c in config.load_instance()["choices"]["incident.close_code"]}
    incidents = []
    for r in sorted(linked, key=lambda r: r["opened_at"]):
        n = list(dict.fromkeys(notes.get(r["number"], []) + ([r["close_notes"]] if r["close_notes"] else [])))
        when = datetime.strptime(r["opened_at"], FMT)
        incidents.append({"number": r["number"], "opened_at": r["opened_at"], "weekday": DAYS[when.weekday()],
                          "short_description": r["short_description"], "priority": r["priority"],
                          "subcategory": r["subcategory"], "cmdb_ci": cis.get(r["cmdb_ci"], {}).get("name", ""),
                          "close_code": close_labels.get(r["close_code"], r["close_code"]), "notes": n,
                          "_caller": r["caller_id"], "_when": when, "_category": r["category"]})
    ci = cis.get(problem["cmdb_ci"], {})
    category = Counter(i["_category"] for i in incidents).most_common(1)[0][0] if incidents else ""
    linked_numbers = {i["number"] for i in incidents}
    siblings = [{"name": c["name"], **c["attrs"],
                 "incidents_same_category": sum(1 for i in all_incidents if i["cmdb_ci"] == sid and i["category"] == category),
                 "linked_to_this_problem": sum(1 for i in all_incidents if i["cmdb_ci"] == sid and i["number"] in linked_numbers)}
                for sid, c in cis.items() if c["class"] == ci.get("class") and sid != problem["cmdb_ci"]]
    siblings = [s for s in siblings if s["incidents_same_category"] or len(s) > 3]  # comparable ones only
    other_subcats = Counter(i["subcategory"] for i in all_incidents
                            if i["cmdb_ci"] == problem["cmdb_ci"] and i["number"] not in linked_numbers)
    changes = 0
    if incidents and problem["cmdb_ci"]:
        start = (incidents[0]["_when"] - timedelta(days=config.DETECT_WINDOW_DAYS)).strftime(FMT)
        changes = svc.count("change_request", f"cmdb_ci={problem['cmdb_ci']}^opened_at>={start}")

    # profiles
    n = len(incidents)
    wd = Counter(i["weekday"] for i in incidents).most_common(1)
    month_end = sum(_last_business_day(i["_when"].date()) for i in incidents)
    hours = sorted(i["_when"].hour for i in incidents)
    time_pattern = []
    if n and wd and wd[0][1] / n >= TIME_PATTERN_SHARE:
        time_pattern.append(f"{wd[0][1]} of {n} opened on a {wd[0][0]}")
    if n and month_end / n >= TIME_PATTERN_SHARE:
        time_pattern.append(f"{month_end} of {n} opened on the last business day of the month")
    resolved = [i for i in incidents if i["close_code"]]
    return {
        "problem": {"number": problem["number"], "short_description": problem["short_description"]},
        "ci": {"name": ci.get("name", ""), "class": ci.get("class", ""), **ci.get("attrs", {})},
        "same_class_cis": siblings,
        "changes_on_ci_in_window": changes,
        "other_subcategories_on_ci_not_linked": dict(other_subcats),
        "time_profile": {"first": incidents[0]["opened_at"] if n else "", "last": incidents[-1]["opened_at"] if n else "",
                         "weekdays": dict(Counter(i["weekday"] for i in incidents)),
                         "last_business_day_of_month": month_end,
                         "hours_utc": f"{hours[0]:02d}-{hours[-1]:02d}" if hours else "",
                         "patterns": time_pattern},
        "close_codes": dict(Counter(i["close_code"] for i in resolved)),
        "affected_users": len({i["_caller"] for i in incidents}),
        "incidents": incidents,
    }


def _public(ev):
    """Evidence as sent to the LLM / stored (private helper keys dropped)."""
    return {**ev, "incidents": [{k: v for k, v in i.items() if not k.startswith("_")} for i in ev["incidents"]]}


def clue_share(ev):
    """Largest share of incidents whose notes contain one single (non-generic) term."""
    n = len(ev["incidents"])
    if not n:
        return "", 0.0
    df = Counter()
    for i in ev["incidents"]:
        df.update(set(t for t in tokens(" ".join(i["notes"])) if len(t) > 2 and not t.isdigit()))
    term, count = df.most_common(1)[0] if df else ("", 0)
    return term, count / n


def choose_method(ev):
    """Assumption 3: Kepner-Tregoe when the evidence is ambiguous, else 5 Whys."""
    cis = {i["cmdb_ci"] for i in ev["incidents"]}
    subcats = {i["subcategory"] for i in ev["incidents"]}
    term, share = clue_share(ev)
    reasons = []
    if len(cis) > 1:
        reasons.append(f"incidents span {len(cis)} CIs")
    if len(subcats) > 1:
        reasons.append(f"incidents span {len(subcats)} subcategories ({', '.join(sorted(subcats))})")
    if share < AMBIGUOUS_CLUE_SHARE:
        reasons.append(f"no clue term in >= {AMBIGUOUS_CLUE_SHARE:.0%} of notes (best '{term}' {share:.0%})")
    return ("Kepner-Tregoe" if reasons else "5 Whys"), (
        "; ".join(reasons) if reasons else f"single CI and subcategory, clue '{term}' in {share:.0%} of notes")


# --- deterministic fallback --------------------------------------------------------------------
def fallback(ev, method):
    incs, n = ev["incidents"], len(ev["incidents"])
    note_incs = {}
    for i in incs:
        for t in i["notes"]:
            note_incs.setdefault(t, []).append(i["number"])
    # comparison notes ("... not affected") support a cause but are not one, so they rank last
    ranked = sorted(note_incs, key=lambda t: ("not affected" not in t.lower(), len(ID_RE.findall(t)), len(note_incs[t])),
                    reverse=True)
    causes = [t for t in ranked if CAUSE_RE.search(t)][:2]
    works = sorted((t for t in note_incs if WORK_RE.search(t)),
                   key=lambda t: ("workaround" in t.lower(), t not in causes, len(note_incs[t])), reverse=True)
    ci = ev["ci"]
    attrs = ", ".join(f"{k} {v}" for k, v in ci.items() if k not in ("name", "class") and v)
    ci_label = f"{ci['name']} ({attrs})" if attrs else ci["name"]
    pattern = "; ".join(ev["time_profile"]["patterns"])
    top_short = Counter(i["short_description"] for i in incs).most_common(1)[0][0] if incs else ""
    closes = Counter(ev["close_codes"]).most_common(1)
    tp = ev["time_profile"]

    if causes:
        root = f"Most likely cause on {ci_label}: {causes[0]}"
        if len(causes) > 1:
            root += f" Supporting: {causes[1]}"
        if pattern:
            root += f" Pattern: {pattern}."
    else:
        top_note = max(note_incs, key=lambda t: len(note_incs[t])) if note_incs else "none"
        root = (f"Not determined from the evidence: the work notes on {ci_label} only record how incidents were "
                f"handled (\"{top_note}\"), not why they happen. Needs further investigation.")
    cited = list(dict.fromkeys(x for t in causes for x in note_incs[t]))[:8] or [i["number"] for i in incs[:3]]

    term, share = clue_share(ev)
    named_ci = any(ci["name"] in t or any(str(v) in t for k, v in ci.items() if k not in ("name", "class") and v)
                   for t in causes)
    # CI pattern (the Kepner-Tregoe contrast): one CI, and a same-class CI with incidents in this category has none linked
    contrast = [s["name"] for s in ev["same_class_cis"] if s["incidents_same_category"] and not s["linked_to_this_problem"]]
    one_ci = len({i["cmdb_ci"] for i in incs}) == 1
    consistent = bool(pattern) or named_ci or (one_ci and bool(contrast))
    pattern_word = "time pattern" if pattern else ("cause names the CI" if named_ci else f"unlike {', '.join(contrast)}")
    if not causes:
        conf, why = "Low", "no note explains a cause"
    elif share > HIGH_CLUE_SHARE and consistent:
        conf, why = "High", f"clue '{term}' in {share:.0%} of notes and {pattern_word}"
    elif share > HIGH_CLUE_SHARE or consistent:
        conf, why = "Medium", f"clue '{term}' in {share:.0%} of notes; {pattern_word if consistent else 'no time or CI pattern'}"
    else:
        conf, why = "Low", f"best clue '{term}' only in {share:.0%} of notes and no pattern"

    whys = [f"Why are users raising incidents such as \"{top_short}\"? Because {ci_label} keeps failing: {n} incidents "
            f"from {tp['first'][:10]} to {tp['last'][:10]}, {ev['affected_users']} users.",
            f"Why does {ci['name']} keep failing? {causes[0] if causes else 'The work notes do not say.'}"]
    if len(causes) > 1:
        whys.append(f"Why does that happen? {causes[1]}")
    if closes:
        whys.append(f"Why does it keep coming back? {closes[0][1]} of {sum(ev['close_codes'].values())} resolved "
                    f"incidents were closed as '{closes[0][0]}', so the underlying condition was not removed."
                    + (f" {pattern}." if pattern else ""))
    whys.append(f"Root cause: {root}")

    kt = None
    if method == "Kepner-Tregoe":
        subcats = Counter(i["subcategory"] for i in incs)
        def label(s):
            a = ", ".join(f"{k} {v}" for k, v in s.items() if k not in ("name", "incidents_same_category", "linked_to_this_problem"))
            return f"{s['name']} ({a})" if a else s["name"]
        sib = "; ".join(f"{label(s)}: {s['incidents_same_category']} incidents in this category, "
                        f"{s['linked_to_this_problem']} linked" for s in ev["same_class_cis"]) or "no comparable CI"
        kt = {"what": {"is": ", ".join(f"{k} ({v})" for k, v in subcats.items()),
                       "is_not": ", ".join(f"{k} ({v})" for k, v in ev["other_subcategories_on_ci_not_linked"].items()) or "-"},
              "where": {"is": ci_label, "is_not": sib},
              "when": {"is": pattern or f"spread {tp['first'][:10]} to {tp['last'][:10]}, hours UTC {tp['hours_utc']}",
                       "is_not": "other days" if pattern else "-"},
              "extent": {"is": f"{n} incidents, {ev['affected_users']} users", "is_not": "-"}}
    return {"method": method, "whys": whys, "is_is_not": kt, "root_cause": root, "confidence": conf,
            "confidence_reason": why, "evidence": cited,
            "workaround": works[0] if works else "No workaround recorded in the incidents."}


# --- LLM -----------------------------------------------------------------------------------------
SYSTEM = ("You are an ITIL 4 problem analyst doing root cause analysis on ServiceNow data. Use only the evidence "
          "given; do not invent facts. Cite incident numbers exactly as given. Answer with one JSON object only.")
SCHEMA = """{"method": "5 Whys" or "Kepner-Tregoe",
 "whys": ["Why ...? Because ...", ...]   (3-5 steps; for Kepner-Tregoe a short reasoning chain),
 "is_is_not": null, or for Kepner-Tregoe {"what": {"is": "", "is_not": ""}, "where": {...}, "when": {...}, "extent": {...}},
 "root_cause": "one or two sentences naming the specific mechanism",
 "confidence": "High" | "Medium" | "Low",
 "confidence_reason": "why this confidence",
 "evidence": ["INC...", ...]   (incident numbers whose notes support the root cause),
 "workaround": "what operators can do until the fix"}"""


def with_llm(ev, method):
    prompt = (f"Do a root cause analysis for problem {ev['problem']['number']} using {method}"
              f"{' (an is / is not table)' if method == 'Kepner-Tregoe' else ''}. If the evidence does not show a "
              "cause, say so and use Low confidence. High confidence needs a cause supported by most incidents and "
              f"at least {MIN_EVIDENCE} cited incidents.\n\nReturn JSON in this shape:\n{SCHEMA}\n\nEvidence:\n"
              + json.dumps(_public(ev), indent=1))
    r = llm.complete_json(SYSTEM, prompt, max_tokens=2500)
    if not isinstance(r.get("root_cause"), str) or not r["root_cause"].strip():
        raise llm.LLMUnavailable("LLM reply has no root_cause")
    r["method"] = method
    r["confidence"] = r.get("confidence") if r.get("confidence") in ("High", "Medium", "Low") else "Low"
    r.setdefault("whys", [])
    r.setdefault("is_is_not", None)
    r["workaround"] = str(r.get("workaround") or "No workaround identified.")
    return r


def validate(r, ev):
    """Assumption 5: cited incidents must be linked; High needs >= MIN_EVIDENCE of them."""
    linked = {i["number"] for i in ev["incidents"]}
    cited = [x for x in r.get("evidence") or [] if isinstance(x, str)]
    r["evidence"] = [x for x in dict.fromkeys(cited) if x in linked]
    dropped = [x for x in cited if x not in linked]
    if dropped:
        r["evidence_dropped"] = dropped
    if r["confidence"] == "High" and len(r["evidence"]) < MIN_EVIDENCE:
        r["confidence"] = "Medium"
        r["confidence_reason"] = f"{r.get('confidence_reason', '')} [downgraded: fewer than {MIN_EVIDENCE} valid incidents cited]"
    return r


def analyse(ev, engine):
    method, method_reason = choose_method(ev)
    if engine == "llm":
        try:
            r = with_llm(ev, method)
            r["engine"] = f"llm {llm.describe()}"
        except llm.LLMUnavailable as e:
            print(f"{LOG} LLM unavailable for {ev['problem']['number']} ({e}); using deterministic fallback")
            r = fallback(ev, method)
            r["engine"] = f"fallback (LLM unavailable: {e})"
    else:
        r = fallback(ev, method)
        r["engine"] = "fallback"
    r["method_reason"] = method_reason
    return validate(r, ev)


# --- Human Gate 2 -------------------------------------------------------------------------------
def show(num, title, r):
    print(f"\n=== {num}  {title}")
    print(f"    method: {r['method']} ({r['method_reason']})   engine: {r['engine']}")
    print(f"    confidence: {r['confidence']} - {r.get('confidence_reason', '')}")
    if r.get("is_is_not"):
        for k, v in r["is_is_not"].items():
            print(f"      {k.upper():6} is: {v.get('is', '')}\n             is not: {v.get('is_not', '')}")
    for w in r.get("whys", []):
        print(f"      - {w}")
    print(f"    ROOT CAUSE: {r['root_cause']}")
    print(f"    WORKAROUND: {r['workaround']}")
    print(f"    evidence ({len(r['evidence'])}): {', '.join(r['evidence'][:10])}{' ...' if len(r['evidence']) > 10 else ''}")
    print(f"    recommendation: {'SEND BACK (low confidence)' if r['confidence'] == 'Low' else 'accept'}")


def gate(num, title, r, auto, ask=input):
    entry = {"gate": 2, "problem": num, "by": getpass.getuser(), "auto": auto, "engine": r["engine"],
             "confidence": r["confidence"], "changes": {}}
    show(num, title, r)
    if auto:
        entry["decision"] = "send_back" if r["confidence"] == "Low" else "accept"
        print(f"    [auto-approve] {entry['decision']}")
    while "decision" not in entry:
        try:
            a = ask("    Decision [y]accept [e]dit [b]send back [n]reject: ").strip().lower()
        except EOFError:
            raise SystemExit(f"{LOG} no input available; use --auto-approve for unattended rehearsal runs")
        if a == "y":
            entry["decision"] = "accept"
        elif a == "b":
            entry["decision"] = "send_back"
            entry["reason"] = ask("    reason for sending back: ").strip()
        elif a == "n":
            entry["decision"] = "reject"
            entry["reason"] = ask("    reason for rejecting: ").strip()
        elif a == "e":
            for field in ("root_cause", "workaround"):
                new = ask(f"    {field} [Enter keeps current]: ").strip()
                if new:
                    entry["changes"][field] = [r[field], new]
                    r[field] = new
            show(num, title, r)
    entry["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return entry


# --- writes ---------------------------------------------------------------------------------------
# This PDI's "General" problem state model (read in phase 4): New -> Assess needs Assigned to;
# Assess -> Root Cause Analysis has no condition; New -> Root Cause Analysis is not allowed. Fix in Progress
# needs cause AND fix notes, so it is left to phase 6, when the fix is planned (decision A).
# problem.state is read-only to the Table API here, so moves go through state_api (server-side, model enforced).
PATH = ["New", "Assess", "Root Cause Analysis"]


def assignee(svc, group):
    """First member (by user name) of the problem's assignment group: one of this project's own users."""
    members = svc.get_all("sys_user_grmember", f"group={group}^ORDERBYuser.user_name", fields=["user"])
    return members[0]["user"] if members else ""


def advance(svc, p, target, data):
    """Step the problem along PATH to `target`, one transition per call to the state API.
    `data` is written with the final step. Returns the states passed through."""
    labels = {config.choice_value("problem.state", l): l for l in PATH}
    here = labels.get(p["state"])
    if here is None:
        raise SNError(f"{p['number']} is in state {p['state']}, outside {PATH}")
    steps = PATH[PATH.index(here) + 1:PATH.index(target) + 1]
    if not steps:  # already there: just write the data (these fields are writable through the Table API)
        svc.update("problem", p["sys_id"], data)
        return []
    for i, label in enumerate(steps):
        fields = dict(data) if i == len(steps) - 1 else {}
        if label == "Assess" and not p.get("assigned_to"):
            p["assigned_to"] = assignee(svc, p["assignment_group"]) or ("DRYRUN" if svc.dry_run else "")
            if not p["assigned_to"]:
                raise SNError(f"{p['number']}: assignment group has no members to assign the problem to")
        if label == "Assess":
            fields["assigned_to"] = p["assigned_to"]
        state_api.move(svc, p, label, fields)
    return steps


def analysis_note(r):
    lines = [f"RCA ({r['method']}, engine {r['engine']}): {r['method_reason']}.",
             f"Confidence {r['confidence']}: {r.get('confidence_reason', '')}"]
    if r.get("is_is_not"):
        lines += [f"{k.upper()} is: {v.get('is', '')} | is not: {v.get('is_not', '')}" for k, v in r["is_is_not"].items()]
    lines += [f"- {w}" for w in r.get("whys", [])]
    lines.append(f"Evidence: {', '.join(r['evidence'])}")
    return "\n".join(lines)


def apply(svc, p, r, e):
    gate_note = (f"Human Gate 2: {e['decision'].replace('_', ' ')} by {e['by']} at {e['at']}"
                 f"{' (auto-approve rehearsal)' if e['auto'] else ''}. Engine {r['engine']}, confidence {r['confidence']}."
                 + (f" Reason: {e['reason']}" if e.get("reason") else "")
                 + (f" Changes: {json.dumps(e['changes'])}" if e["changes"] else ""))
    note = analysis_note(r) + "\n\n" + gate_note
    if e["decision"] == "accept":
        path = advance(svc, p, "Root Cause Analysis", {
            "cause_notes": f"{r['root_cause']}\n\nMethod: {r['method']} (engine: {r['engine']}). Confidence: {r['confidence']}.",
            "workaround": r["workaround"], "work_notes": note})
    elif e["decision"] == "send_back":
        path = advance(svc, p, "Root Cause Analysis", {"work_notes": note})
    else:
        svc.update("problem", p["sys_id"], {"work_notes": note})
        path = []
    e["states"] = path
    e["assigned_to"] = p.get("assigned_to", "")
    return path


# --- run --------------------------------------------------------------------------------------------
def load_world(svc, tag):
    all_incidents = svc.get_all("incident", f"correlation_id={tag}", fields=["number", "cmdb_ci", "category", "subcategory"])
    cis = {}
    for r in svc.get_all("cmdb_ci", f"correlation_id={tag}", fields=["sys_id", "name", "sys_class_name"]):
        cis[r["sys_id"]] = {"name": r["name"], "class": r["sys_class_name"], "attrs": {}}
    for cls, attrs in CI_ATTRS.items():
        for r in svc.get_all(cls, f"correlation_id={tag}", fields=["sys_id"] + attrs):
            cis[r["sys_id"]]["attrs"] = {a: r[a] for a in attrs if r.get(a)}
    return all_incidents, cis


TRANSIENT = ("dry_run", "auto_approve", "engine", "problems", "ask")


def run(context):
    dry, tag = context.get("dry_run", False), context["tag"]
    engine = context.get("engine") or ("llm" if llm.available() else "fallback")
    svc = SNClient(dry_run=dry)
    print(f"{LOG} run {context['run_id']} as {svc.session.auth[0]} dry_run={dry} engine={engine}"
          f"{' ' + llm.describe() if engine == 'llm' else ''}")
    open_states = {config.choice_value("problem.state", s) for s in PATH}
    rca_state = config.choice_value("problem.state", "Root Cause Analysis")
    all_incidents, cis = load_world(svc, tag)
    problems = svc.get_all("problem", f"correlation_id={tag}", fields=["sys_id", "number", "short_description", "cmdb_ci",
                                                                        "state", "assigned_to", "assignment_group", "cause_notes"])
    only = set(context.get("problems") or [])
    # done = accepted (Root Cause Analysis with cause notes); New, Assess (an interrupted run) and sent-back are (re)analysed
    todo = sorted((p for p in problems if p["state"] in open_states and not (p["state"] == rca_state and p["cause_notes"].strip())
                   and (not only or p["number"] in only)), key=lambda p: p["number"])
    print(f"{LOG} {len(todo)} problems to analyse (New, Assess, or sent back to Root Cause Analysis)")
    results = context.setdefault("rca", {}).setdefault("results", {}) if not dry else {}
    for p in todo:
        ev = gather(svc, p, tag, all_incidents, cis)
        r = analyse(ev, engine)
        e = gate(p["number"], p["short_description"], r, context.get("auto_approve", False), context.get("ask", input))
        path = apply(svc, p, r, e)
        print(f"{LOG} {p['number']}: {e['decision']}{' -> ' + ' -> '.join(path) if path else ''}")
        if not dry:
            context.setdefault("log", []).append(e)
            results[p["number"]] = {**r, "decision": e["decision"], "at": e["at"]}
            config.save_context({k: v for k, v in context.items() if k not in TRANSIENT})  # keep decisions if a later one fails
    return results


# --- evaluation and success check ------------------------------------------------------------
def _key(context):
    return json.loads((config.RUNS_DIR / f"{context['run_id']}_answer_key.json").read_text())["patterns"]


def match_pattern(key, linked):
    pid, p = max(key.items(), key=lambda kv: len(linked & set(kv[1]["incidents"])))
    return pid if linked & set(p["incidents"]) and p["expect_problem"] else None


def score(pid, text):
    """Groups of the planted mechanism named in the text: (all matched, [hit per group])."""
    t = text.lower()
    hits = [any(k in t for k in group) for group in SCORE_KEYWORDS[pid]]
    return all(hits), hits


def compare(context):
    """Both engines on every detection problem, scored against the answer key. No gate, no writes."""
    svc, key = SNClient(), _key(context)
    all_incidents, cis = load_world(svc, context["tag"])
    problems = svc.get_all("problem", f"correlation_id={context['tag']}", fields=["sys_id", "number", "short_description", "cmdb_ci"])
    totals = Counter()
    rows = []
    for p in sorted(problems, key=lambda p: p["number"]):
        ev = gather(svc, p, context["tag"], all_incidents, cis)
        pid = match_pattern(key, {i["number"] for i in ev["incidents"]})
        row = [p["number"], f"{pid}) {key[pid]['ci']}" if pid else "unplanted"]
        for engine in ("fallback", "llm"):
            r = analyse(ev, engine)
            ok, hits = score(pid, r["root_cause"]) if pid else (None, [])
            totals[engine] += bool(ok)
            row.append(f"{r['method'][:2]} {r['confidence']:6} {'MATCH' if ok else ('miss ' + str(hits)) if pid else 'not scored'}")
            print(f"\n--- {p['number']} [{engine}] {r['engine']}\n    {r['root_cause']}\n    workaround: {r['workaround']}")
        rows.append(row)
    print(f"\n{'problem':11} {'planted':16} {'fallback':34} llm")
    for row in rows:
        print(f"{row[0]:11} {row[1]:16} {row[2]:34} {row[3]}")
    planted = sum(1 for v in key.values() if v["expect_problem"])
    print(f"\nroot causes matched: fallback {totals['fallback']} of {planted}, llm {totals['llm']} of {planted}")


def check(context):
    """Success check recomputed from the PDI."""
    admin = SNClient(user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
    key, ok = _key(context), True
    results = context.get("rca", {}).get("results", {})
    st = {s: config.choice_value("problem.state", s) for s in ("Root Cause Analysis", "Fix in Progress")}
    # an accepted RCA ends in Root Cause Analysis; later phases move it on (Fix in Progress, Resolved, Closed)
    rca_or_later = {config.choice_value("problem.state", s) for s in ("Root Cause Analysis", "Fix in Progress", "Resolved", "Closed")}
    print(f"{LOG} CHECK (recomputed from the PDI)")
    problems = admin.get_all("problem", f"correlation_id={context['tag']}",
                             fields=["sys_id", "number", "state", "cause_notes", "workaround", "assigned_to"])
    matched = 0
    for p in sorted(problems, key=lambda p: p["number"]):
        r = results.get(p["number"])
        if not r:
            print(f"  ----  {p['number']} not analysed yet (state {p['state']})")
            continue
        linked = {x["number"] for x in admin.get_all("incident", f"{config.INCIDENT_PROBLEM_LINK}={p['sys_id']}", fields=["number"])}
        notes = admin.get_all("sys_journal_field", f"element_id={p['sys_id']}^element=work_notes", fields=["value"])
        gate_note = any("Human Gate 2" in n["value"] for n in notes)
        ev_ok = len(r["evidence"]) >= MIN_EVIDENCE and set(r["evidence"]) <= linked
        if r["decision"] == "accept":
            good = (p["state"] in rca_or_later and bool(p["assigned_to"]) and bool(p["cause_notes"].strip())
                    and bool(p["workaround"].strip()))
        elif r["decision"] == "send_back":
            good = p["state"] == st["Root Cause Analysis"] and not p["cause_notes"].strip()
        else:
            good = True
        good = good and gate_note and ev_ok
        ok &= good
        pid = match_pattern(key, linked)
        hit = score(pid, p["cause_notes"] or r["root_cause"])[0] if pid else None
        matched += bool(hit)
        print(f"  {'PASS' if good else 'FAIL'}  {p['number']} {r['decision']:9} state={p['state']} "
              f"evidence={len(r['evidence'])} linked-ok={set(r['evidence']) <= linked} gate note={gate_note}  "
              f"{'planted ' + pid + (': MATCH' if hit else ': miss') if pid else 'unplanted: not scored'}")
    decisions = [e for e in context.get("log", []) if e.get("gate") == 2]
    good = bool(decisions) and all({"by", "at", "decision"} <= e.keys() for e in decisions)
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {len(decisions)} gate-2 decisions logged with who/when/what")
    planted = sum(1 for v in key.values() if v["expect_problem"])
    print(f"  root causes matched: {matched} of {planted} planted problems")
    print(f"{LOG} CHECK RESULT: {'ALL PASSED' if ok else 'FAILED'}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--auto-approve", action="store_true", help="rehearsal only: accept High/Medium, send back Low")
    ap.add_argument("--engine", choices=["llm", "fallback"], help="default: llm when configured, else fallback")
    ap.add_argument("--problem", nargs="+", metavar="PRB", help="only these problems (e.g. to rerun one sent back)")
    ap.add_argument("--compare", action="store_true", help="run both engines and score them; no gate, no writes")
    ap.add_argument("--check", metavar="RUN_ID")
    args = ap.parse_args()
    try:
        if args.check:
            sys.exit(0 if check(config.load_context(args.check)) else 1)
        if not args.run_id:
            ap.error("--run-id is required")
        ctx = config.load_context(args.run_id)
        if args.compare:
            compare(ctx)
            return
        ctx.update(dry_run=args.dry_run, auto_approve=args.auto_approve, engine=args.engine, problems=args.problem)
        run(ctx)
        if not args.dry_run:
            for k in TRANSIENT:
                ctx.pop(k, None)
            config.save_context(ctx)
            print(f"{LOG} context saved to runs/{ctx['run_id']}.json")
            sys.exit(0 if check(ctx) else 1)
    except SNError as e:
        sys.exit(f"{LOG} ERROR: {e}")


if __name__ == "__main__":
    main()
