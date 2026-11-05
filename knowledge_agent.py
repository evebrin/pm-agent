"""Known Error & KB Agent.

For every problem whose RCA was accepted at Gate 2 (Root Cause Analysis with cause notes, no known
error article yet) it writes a Known Error article in the "Known Error" knowledge base (the platform's
Known Error article template: description, cause, workaround), publishes it, marks the problem as a
known error with that primary article, and posts the workaround on the problem's open incidents.

The article may only restate what the human accepted at Gate 2 (the problem's cause notes and
workaround as they are in the PDI); the LLM rewords it for a reader, the deterministic fallback fills
a template. Runs as pm_agent_svc.

    python knowledge_agent.py --run-id <id> [--dry-run] [--engine llm|fallback] [--problem PRB...]
    python knowledge_agent.py --check <run_id>
"""
import argparse
import html
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone

import config
import llm
import rca_agent
import state_api
from sn_client import SNClient, SNError

LOG = "[knowledge]"
KB_TITLE = "Known Error"                       # verified in phase 5: pm_agent_svc (itil, problem_coordinator) can contribute
ARTICLE_TABLE = "kb_template_known_error_article"
TITLE_PREFIX = f"[{config.TAG_PREFIX}] Known error: "
OPEN_INCIDENT_STATES = ("New", "In Progress", "On Hold")
TRANSIENT = ("dry_run", "engine", "problems")


def accepted_text(p):
    """Cause and workaround exactly as accepted at Gate 2 (cause notes minus the method footer)."""
    return (html.unescape(p["cause_notes"].split("\n\nMethod:")[0]).strip(), html.unescape(p["workaround"]).strip())


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z(])", text) if s.strip()]


# --- article text ------------------------------------------------------------------------------
def facts(p, ev):
    tp = ev["time_profile"]
    shorts = [s for s, _ in Counter(i["short_description"] for i in ev["incidents"]).most_common(5)]
    cause, workaround = accepted_text(p)
    return {"problem": p["number"], "problem_title": p["short_description"], "ci": ev["ci"]["name"],
            "incident_count": len(ev["incidents"]), "affected_users": ev["affected_users"],
            "first_seen": tp["first"][:10], "last_seen": tp["last"][:10], "time_pattern": tp["patterns"],
            "typical_symptoms": shorts, "accepted_cause": cause, "accepted_workaround": workaround}


def fallback(f):
    when = "; ".join(f["time_pattern"]) or f"seen between {f['first_seen']} and {f['last_seen']}"
    return {"title": f"{f['ci']}: {f['typical_symptoms'][0] if f['typical_symptoms'] else 'recurring incidents'}",
            "symptoms": f["typical_symptoms"],
            "description": f"Users report recurring issues on {f['ci']} ({f['incident_count']} incidents, "
                           f"{f['affected_users']} users; {when}).",
            "cause": f["accepted_cause"],
            "workaround_steps": _sentences(f["accepted_workaround"]) or [f["accepted_workaround"]]}


SYSTEM = ("You write ServiceNow Known Error knowledge articles for service desk staff. Restate only the facts "
          "given; never add causes, fixes, commands, product names or numbers that are not in the input. "
          "Answer with one JSON object only.")
SCHEMA = """{"title": "short article title naming the CI and the symptom (no prefix)",
 "symptoms": ["what users see, 2-5 short items, from typical_symptoms"],
 "description": "1-2 sentences: who is affected, where, when (from the counts, dates and time_pattern)",
 "cause": "the accepted_cause in plain language, same meaning, nothing added",
 "workaround_steps": ["the accepted_workaround as 1-5 numbered-style steps, nothing added"]}"""


def with_llm(f):
    r = llm.complete_json(SYSTEM, "Write the article from these facts.\n\nReturn JSON in this shape:\n"
                          f"{SCHEMA}\n\nFacts:\n{json.dumps(f, indent=1)}", max_tokens=1500)
    if not all(r.get(k) for k in ("title", "cause", "workaround_steps")):
        raise llm.LLMUnavailable("LLM article is missing title, cause or workaround steps")
    r["symptoms"] = [str(s) for s in r.get("symptoms") or f["typical_symptoms"]][:5]
    r["workaround_steps"] = [str(s) for s in r["workaround_steps"]][:6]
    r["description"] = str(r.get("description") or "")
    return r


def write(f, engine):
    if engine == "llm":
        try:
            a = with_llm(f)
            a["engine"] = f"llm {llm.describe()}"
            return a
        except llm.LLMUnavailable as e:
            print(f"{LOG} LLM unavailable for {f['problem']} ({e}); using deterministic fallback")
            a = fallback(f)
            a["engine"] = f"fallback (LLM unavailable: {e})"
            return a
    a = fallback(f)
    a["engine"] = "fallback"
    return a


def to_fields(a, f, kb, p):
    esc = html.escape
    desc = (f"<p>{esc(a['description'])}</p><p><strong>Symptoms</strong></p><ul>"
            + "".join(f"<li>{esc(s)}</li>" for s in a["symptoms"]) + "</ul>"
            + f"<p>Problem: {esc(f['problem'])}. Affected CI: {esc(f['ci'])}.</p>")
    work = "<ol>" + "".join(f"<li>{esc(s)}</li>" for s in a["workaround_steps"]) + "</ol>"
    return {"short_description": TITLE_PREFIX + a["title"], "kb_knowledge_base": kb, "kb_description": desc,
            "kb_cause": f"<p>{esc(a['cause'])}</p>", "kb_workaround": work, "cmdb_ci": p["cmdb_ci"],
            "source": p["sys_id"], "workflow_state": "draft"}


def untagged_articles(sn, kb):
    """Articles this project did not create: total count, and the Known Error KB's ones with their last update."""
    tagged = f"short_descriptionSTARTSWITH[{config.TAG_PREFIX}]"
    ours = {r["sys_id"] for r in sn.get_all("kb_knowledge", tagged, fields=["sys_id"])}
    ke = sn.get_all("kb_knowledge", f"kb_knowledge_base={kb}", fields=["sys_id", "sys_updated_on"])
    return {"untagged_articles": sn.count("kb_knowledge") - len(ours),
            "known_error_kb_untagged": [r["sys_id"] + "|" + r["sys_updated_on"] for r in ke if r["sys_id"] not in ours]}


# --- run ---------------------------------------------------------------------------------------
def run(context):
    dry, tag = context.get("dry_run", False), context["tag"]
    engine = context.get("engine") or ("llm" if llm.available() else "fallback")
    svc = SNClient(dry_run=dry)
    print(f"{LOG} run {context['run_id']} as {svc.session.auth[0]} dry_run={dry} engine={engine}"
          f"{' ' + llm.describe() if engine == 'llm' else ''}")
    kb = svc.get("kb_knowledge_base", f"title={KB_TITLE}^active=true", fields=["sys_id"])
    if not kb:
        raise SNError(f"knowledge base {KB_TITLE!r} not found or not readable by {svc.session.auth[0]}")
    kb = kb[0]["sys_id"]
    rca = config.choice_value("problem.state", "Root Cause Analysis")
    accepted = {n for n, r in context.get("rca", {}).get("results", {}).items() if r.get("decision") == "accept"}
    only = set(context.get("problems") or [])
    problems = svc.get_all("problem", f"correlation_id={tag}", fields=[
        "sys_id", "number", "short_description", "state", "cmdb_ci", "cause_notes", "workaround",
        "known_error", "primary_known_error_article"])
    todo = sorted((p for p in problems if p["state"] == rca and p["cause_notes"].strip() and p["workaround"].strip()
                   and not p["primary_known_error_article"] and p["number"] in accepted
                   and (not only or p["number"] in only)), key=lambda p: p["number"])
    print(f"{LOG} {len(todo)} accepted problems without a known error article")
    knowledge = context.setdefault("knowledge", {}) if not dry else {}
    if not dry and "baseline" not in knowledge:  # untagged articles, to show phase 5 touches no one else's
        knowledge["baseline"] = untagged_articles(svc, kb)
    # articles we created earlier that are still draft (e.g. before the publish operation existed)
    for p in problems:
        if p["number"] not in accepted or not p["primary_known_error_article"] or (only and p["number"] not in only):
            continue
        a = svc.get("kb_knowledge", f"sys_id={p['primary_known_error_article']}", fields=["number", "workflow_state", "short_description"])
        if a and a[0]["workflow_state"] == "draft" and a[0]["short_description"].startswith(TITLE_PREFIX):
            ok = state_api.publish(svc, p["primary_known_error_article"], a[0]["number"])
            print(f"{LOG} {p['number']}: existing draft {a[0]['number']} {'published' if ok else 'still draft'}")
            if ok and not dry:
                svc.update("problem", p["sys_id"], {"work_notes": f"Known error article {a[0]['number']} published."})
                if p["number"] in results_so_far(context):
                    context["knowledge"]["articles"][p["number"]]["published"] = True
    all_incidents, cis = rca_agent.load_world(svc, tag)
    open_states = {config.choice_value("incident.state", s) for s in OPEN_INCIDENT_STATES}
    results = knowledge.setdefault("articles", {}) if not dry else {}
    for p in todo:
        ev = rca_agent.gather(svc, p, tag, all_incidents, cis)
        f = facts(p, ev)
        a = write(f, engine)
        fields = to_fields(a, f, kb, p)
        print(f"\n=== {p['number']}  ->  {fields['short_description']}   ({a['engine']})")
        print(f"    cause: {a['cause']}\n    workaround: " + " | ".join(a["workaround_steps"]))
        art = svc.create(ARTICLE_TABLE, fields)
        art_id = art["sys_id"]
        number = art.get("number", "DRYRUN")
        # workflow_state is ignored by the Table API here; the project's endpoint calls the platform's publish
        published = state_api.publish(svc, art_id, number)
        if not published:
            print(f"{LOG} WARNING {number} was not published (still draft); not forcing it")
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        svc.update("problem", p["sys_id"], {
            "known_error": "true", "primary_known_error_article": art_id,
            "work_notes": f"Known error recorded by the Known Error & KB agent at {at}: {number} "
                          f"({'published' if published else 'draft'}) in the {KB_TITLE} knowledge base. "
                          f"Article text by {a['engine']}, restating the cause and workaround accepted at Gate 2."})
        open_incs = svc.get_all("incident", f"{config.INCIDENT_PROBLEM_LINK}={p['sys_id']}^stateIN{','.join(open_states)}",
                                fields=["sys_id", "number"])
        for i in open_incs:
            svc.update("incident", i["sys_id"], {"work_notes":
                       f"Known error {number} ({p['number']}). Workaround: {accepted_text(p)[1]}"})
        print(f"{LOG} {p['number']}: {number} {'published' if published else 'DRAFT'}; problem marked known error; "
              f"{len(open_incs)} open incidents noted")
        if not dry:
            results[p["number"]] = {"article": number, "sys_id": art_id, "published": published, "engine": a["engine"],
                                    "open_incidents_noted": [i["number"] for i in open_incs], "at": at}
            context.setdefault("log", []).append({"phase": 5, "problem": p["number"], "article": number,
                                                  "published": published, "at": at, "by": svc.session.auth[0]})
            config.save_context({k: v for k, v in context.items() if k not in TRANSIENT})
    return results


def results_so_far(context):
    return context.get("knowledge", {}).get("articles", {})


# --- success check -----------------------------------------------------------------------------
def _text(h):
    return html.unescape(re.sub(r"<[^>]+>", " ", h or ""))


def check(context):
    admin = SNClient(user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
    key, ok, matched = rca_agent._key(context), True, 0
    k = context.get("knowledge", {})
    arts = k.get("articles", {})
    kb = admin.get("kb_knowledge_base", f"title={KB_TITLE}", fields=["sys_id"])[0]["sys_id"]
    accepted = {n for n, r in context.get("rca", {}).get("results", {}).items() if r.get("decision") == "accept"}
    print(f"{LOG} CHECK (recomputed from the PDI)")
    problems = admin.get_all("problem", f"correlation_id={context['tag']}",
                             fields=["sys_id", "number", "known_error", "primary_known_error_article"])
    for p in sorted(problems, key=lambda p: p["number"]):
        if p["number"] not in accepted:
            good = p["known_error"] != "true" and not p["primary_known_error_article"]
            ok &= good
            print(f"  {'PASS' if good else 'FAIL'}  {p['number']} not accepted at Gate 2: no known error")
            continue
        a = admin.get(ARTICLE_TABLE, f"sys_id={p['primary_known_error_article']}", fields=[
            "number", "short_description", "kb_knowledge_base", "workflow_state", "source", "cmdb_ci", "kb_cause",
            "kb_workaround"]) if p["primary_known_error_article"] else []
        if not a:
            ok = False
            print(f"  FAIL  {p['number']} has no known error article")
            continue
        a = a[0]
        good = (p["known_error"] == "true" and a["kb_knowledge_base"] == kb and a["workflow_state"] == "published"
                and a["source"] == p["sys_id"] and bool(a["cmdb_ci"]) and _text(a["kb_cause"]).strip() != ""
                and _text(a["kb_workaround"]).strip() != "" and a["short_description"].startswith(TITLE_PREFIX))
        noted = arts.get(p["number"], {}).get("open_incidents_noted", [])
        for n in noted:
            inc = admin.get("incident", f"number={n}", fields=["sys_id"])[0]
            good &= admin.count("sys_journal_field", f"element_id={inc['sys_id']}^element=work_notes^valueLIKEKnown error {a['number']}") > 0
        linked = {x["number"] for x in admin.get_all("incident", f"{config.INCIDENT_PROBLEM_LINK}={p['sys_id']}", fields=["number"])}
        pid = rca_agent.match_pattern(key, linked)
        hit = rca_agent.score(pid, _text(a["kb_cause"]))[0] if pid else None
        matched += bool(hit)
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {p['number']} {a['number']} {a['workflow_state']:9} known_error={p['known_error']} "
              f"open incidents noted={len(noted)}  {'planted ' + pid + (': MATCH' if hit else ': miss') if pid else 'unplanted: not scored'}")
    base, now = k.get("baseline", {}), untagged_articles(admin, kb)
    untagged, ke_now = now["untagged_articles"], now["known_error_kb_untagged"]
    good = base and untagged == base["untagged_articles"] and sorted(ke_now) == sorted(base["known_error_kb_untagged"])
    ok &= bool(good)
    print(f"  {'PASS' if good else 'FAIL'}  articles we did not create unchanged ({untagged} now, "
          f"{base.get('untagged_articles')} before; {len(ke_now)} demo Known Error articles untouched)")
    planted = sum(1 for v in key.values() if v["expect_problem"])
    print(f"  known error causes matched: {matched} of {planted} planted problems")
    print(f"{LOG} CHECK RESULT: {'ALL PASSED' if ok else 'FAILED'}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--engine", choices=["llm", "fallback"], help="default: llm when configured, else fallback")
    ap.add_argument("--problem", nargs="+", metavar="PRB")
    ap.add_argument("--check", metavar="RUN_ID")
    args = ap.parse_args()
    try:
        if args.check:
            sys.exit(0 if check(config.load_context(args.check)) else 1)
        if not args.run_id:
            ap.error("--run-id is required")
        ctx = config.load_context(args.run_id)
        ctx.update(dry_run=args.dry_run, engine=args.engine, problems=args.problem)
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
