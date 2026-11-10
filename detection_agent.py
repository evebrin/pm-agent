"""Problem Detection Agent (reactive + proactive trending) with Human Gate 1.

Groups tagged incidents by CI and by business service, flags a candidate when a group has
>= DETECT_MIN_INCIDENTS within DETECT_WINDOW_DAYS, refines members by dominant category and
TF-IDF similarity of short descriptions, scores and asks a human to approve / reject / merge /
edit. Approved candidates become problem records with their incidents linked via `parent`.

Runs as pm_agent_svc. Exception (verified on this PDI): closed incidents only accept the
parent link from admin, so those link writes use admin and are logged as such.

    python detection_agent.py --run-id <id> [--dry-run] [--auto-approve]
    python detection_agent.py --check <run_id>
"""
import argparse
import getpass
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import config
from sn_client import SNClient, SNError

LOG = "[detection]"
FMT = "%Y-%m-%d %H:%M:%S"
FIELDS = ["sys_id", "number", "short_description", "opened_at", "cmdb_ci", "business_service", "category",
          "subcategory", "priority", "caller_id", "assignment_group", "state"]
STOP = set("a an the to of in on for and or is are was be my not no i we it this that with at from again "
           "very after keeps cannot can will need".split())


# --- text similarity (TF-IDF, standard library only) -------------------------------------
def tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower()) if t not in STOP and len(t) > 1]


def tfidf(docs):
    df = Counter(t for d in docs for t in set(d))
    n = len(docs)
    idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}
    return [_norm({t: c * idf[t] for t, c in Counter(d).items()}) for d in docs]


def _norm(v):
    length = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {t: x / length for t, x in v.items()}


def cosine(a, b):
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def centroid(vecs):
    total = Counter()
    for v in vecs:
        total.update(v)
    return _norm(dict(total))


# --- clustering methods (run inside one CI/service group) ------------------------------------
METHODS = ("centroid", "agglomerative", "dbscan")


def split(members, vec, method):
    """Split one group's incidents into clusters. Returns a list of incident lists."""
    v = [vec[i["sys_id"]] for i in members]
    if method == "centroid":  # one cluster; drop members far from the average wording
        c = centroid(v)
        return [[i for i, x in zip(members, v) if cosine(x, c) >= config.DETECT_MIN_SIMILARITY]]
    if method == "agglomerative":  # average linkage, merge until the best pair is too dissimilar
        clusters = [[k] for k in range(len(members))]
        while len(clusters) > 1:
            best, pair = -1.0, None
            for a in range(len(clusters)):
                for b in range(a + 1, len(clusters)):
                    s = sum(cosine(v[x], v[y]) for x in clusters[a] for y in clusters[b]) / (len(clusters[a]) * len(clusters[b]))
                    if s > best:
                        best, pair = s, (a, b)
            if best < config.AGG_MIN_SIMILARITY:
                break
            a, b = pair
            clusters[a] += clusters.pop(b)
        return [[members[k] for k in cl] for cl in clusters]
    if method == "dbscan":  # density-based on cosine similarity; isolated incidents are noise
        n = len(members)
        nbrs = [[j for j in range(n) if cosine(v[i], v[j]) >= config.DBSCAN_MIN_SIMILARITY] for i in range(n)]
        label, clusters = [None] * n, []
        for i in range(n):
            if label[i] is not None or len(nbrs[i]) < config.DBSCAN_MIN_POINTS:
                continue
            cl, queue = [], [i]
            label[i] = len(clusters)
            while queue:
                j = queue.pop()
                cl.append(j)
                if len(nbrs[j]) >= config.DBSCAN_MIN_POINTS:
                    for k in nbrs[j]:
                        if label[k] is None:
                            label[k] = len(clusters)
                            queue.append(k)
            clusters.append(cl)
        return [[members[k] for k in cl] for cl in clusters] or [[]]
    raise ValueError(f"unknown clustering method {method!r}; choose from {METHODS}")


# --- detection -----------------------------------------------------------------------------
def max_in_window(times, days):
    times = sorted(times)
    return max((sum(1 for t in times if s <= t < s + timedelta(days=days)) for s in times), default=0)


def detect(incidents, ci_names, method=None):
    """Return scored candidates. Uses only PDI incident data (never the answer key)."""
    method = method or config.DETECT_METHOD
    vec = dict(zip((i["sys_id"] for i in incidents), tfidf([tokens(i["short_description"]) for i in incidents])))
    for i in incidents:
        i["_t"] = datetime.strptime(i["opened_at"], FMT)
    callers_total = len({i["caller_id"] for i in incidents}) or 1

    raw = []
    for field in ("cmdb_ci", "business_service"):
        groups = {}
        for i in incidents:
            if i[field]:
                groups.setdefault(i[field], []).append(i)
        for key, group in groups.items():
            peak = max_in_window([i["_t"] for i in group], config.DETECT_WINDOW_DAYS)
            if peak < config.DETECT_MIN_INCIDENTS:
                continue
            category = Counter(i["category"] for i in group).most_common(1)[0][0]
            same_cat = [i for i in group if i["category"] == category]
            clusters = split(same_cat, vec, method)
            # every sub-cluster that crosses the threshold is a candidate; otherwise show the largest (as Low)
            keep = [cl for cl in clusters if max_in_window([i["_t"] for i in cl], config.DETECT_WINDOW_DAYS)
                    >= config.DETECT_MIN_INCIDENTS] or [max(clusters, key=len)]
            for members in (m for m in keep if m):  # a group that is all noise yields no candidate
                raw.append({"field": field, "key": key, "group_size": len(group), "group_peak": peak,
                            "category": category, "members": members})

    # merge CI-based and service-based candidates that describe the same incidents
    merged = []
    for cand in sorted(raw, key=lambda c: -len(c["members"])):
        ids = {i["sys_id"] for i in cand["members"]}
        for m in merged:
            mids = {i["sys_id"] for i in m["members"]}
            if ids and len(ids & mids) / len(ids | mids) >= 0.5:
                m["members"] += [i for i in cand["members"] if i["sys_id"] not in mids]
                m.setdefault(cand["field"], cand["key"])
                m["group_size"] = max(m["group_size"], cand["group_size"])
                break
        else:
            merged.append({**cand, cand["field"]: cand["key"]})

    out = []
    for n, cand in enumerate(merged, 1):
        members = sorted(cand["members"], key=lambda i: i["_t"])
        vecs = [vec[i["sys_id"]] for i in members]
        c = centroid(vecs) if vecs else {}
        cohesion = sum(cosine(v, c) for v in vecs) / len(vecs) if vecs else 0.0
        purity = len(members) / cand["group_size"]
        peak = max_in_window([i["_t"] for i in members], config.DETECT_WINDOW_DAYS)
        if peak >= config.DETECT_MIN_INCIDENTS and cohesion >= config.CONF_HIGH_COHESION and purity >= config.CONF_HIGH_PURITY:
            confidence = "High"
        elif peak >= config.DETECT_MIN_INCIDENTS and cohesion >= config.CONF_MEDIUM_COHESION:
            confidence = "Medium"
        else:
            confidence = "Low"
        priorities = [int(i["priority"] or 4) for i in members] or [4]
        frequency = min(1.0, peak / (2 * config.DETECT_MIN_INCIDENTS))
        impact = sum((5 - p) / 4 for p in priorities) / len(priorities)   # P1 -> 1.0, P4 -> 0.25
        users = len({i["caller_id"] for i in members}) / callers_total
        score = round(0.4 * frequency + 0.4 * impact + 0.2 * users, 2)
        imp, urg = ("1", "2") if score >= 0.65 else ("2", "2") if score >= 0.45 else ("3", "3")
        terms = [t for t, _ in Counter(t for i in members for t in set(tokens(i["short_description"]))).most_common(3)]
        where = " / ".join(ci_names.get(cand[f], cand[f]) for f in ("cmdb_ci", "business_service") if cand.get(f))
        out.append({"id": f"C{n}", "title": f"Recurring {cand['category'].replace('_', ' ')} incidents on {where} ({', '.join(terms)})",
                    "cmdb_ci": cand.get("cmdb_ci", ""), "business_service": cand.get("business_service", ""),
                    "where": where, "confidence": confidence, "cohesion": round(cohesion, 2),
                    "purity": round(purity, 2), "peak_14d": peak, "incident_count": len(members),
                    "affected_users": len({i["caller_id"] for i in members}),
                    "score": score, "score_parts": {"frequency": round(frequency, 2), "impact": round(impact, 2),
                                                    "users": round(users, 2)},
                    "impact": imp, "urgency": urg,
                    "assignment_group": Counter(i["assignment_group"] for i in members).most_common(1)[0][0] if members else "",
                    "incidents": [{"sys_id": i["sys_id"], "number": i["number"], "short_description": i["short_description"],
                                   "opened_at": i["opened_at"], "state": i["state"]} for i in members]})
    return sorted(out, key=lambda c: -c["score"])


# --- Human Gate 1 ----------------------------------------------------------------------------
PRIORITY = {("1", "1"): 1, ("1", "2"): 2, ("2", "1"): 2, ("1", "3"): 3, ("2", "2"): 3, ("3", "1"): 3,
            ("2", "3"): 4, ("3", "2"): 4, ("3", "3"): 5}


def show(c):
    print(f"\n=== {c['id']}  {c['title']}")
    print(f"    where: {c['where']}   incidents: {c['incident_count']}   peak in 14 days: {c['peak_14d']}   "
          f"affected users: {c['affected_users']}")
    print(f"    confidence: {c['confidence']} (similarity {c['cohesion']}, purity {c['purity']})   "
          f"score: {c['score']} {c['score_parts']}")
    print(f"    proposed priority: P{PRIORITY[(c['impact'], c['urgency'])]} (impact {c['impact']}, urgency {c['urgency']})"
          f"   recommendation: {'REJECT (low confidence)' if c['confidence'] == 'Low' else 'approve'}")
    for i in c["incidents"][:3]:
        print(f"      {i['number']}  {i['opened_at'][:10]}  {i['short_description']}")


def gate(candidates, auto, ask=input):
    """Collect a decision per candidate. Returns list of log entries."""
    by, ids, log = getpass.getuser(), {c["id"]: c for c in candidates}, []
    for c in candidates:
        show(c)
        entry = {"gate": 1, "candidate": c["id"], "by": by, "auto": auto, "changes": {}}
        if auto:
            entry["decision"] = "reject" if c["confidence"] == "Low" else "approve"
            print(f"    [auto-approve] {entry['decision']}")
        while "decision" not in entry:
            try:
                answer = ask("    Decision [y]approve [n]reject [m <id>]merge into [e]dit: ").strip()
            except EOFError:
                raise SystemExit(f"{LOG} no input available; use --auto-approve for unattended rehearsal runs")
            if answer.lower() == "y":
                entry["decision"] = "approve"
            elif answer.lower() == "n":
                entry["decision"] = "reject"
            elif answer.lower().startswith("m "):
                target = answer[2:].strip().upper()
                if target in ids and target != c["id"]:
                    entry.update(decision="merge", into=target)
                else:
                    print("    unknown candidate id")
            elif answer.lower() == "e":
                title = ask(f"    title [{c['title']}]: ").strip()
                prio = ask(f"    impact,urgency [{c['impact']},{c['urgency']}]: ").strip()
                if title:
                    entry["changes"]["title"] = [c["title"], title]
                    c["title"] = title
                if re.fullmatch(r"[123],[123]", prio):
                    entry["changes"]["impact,urgency"] = [f"{c['impact']},{c['urgency']}", prio]
                    c["impact"], c["urgency"] = prio.split(",")
                show(c)
        entry["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log.append(entry)

    # apply merges: fold merged candidates into their target (following chains)
    decision = {e["candidate"]: e for e in log}
    for e in log:
        if e["decision"] == "merge":
            target = e["into"]
            while decision[target]["decision"] == "merge":
                target = decision[target]["into"]
            if decision[target]["decision"] != "approve":
                print(f"{LOG} {e['candidate']} merged into {target}, which was not approved: nothing created")
                continue
            have = {i["sys_id"] for i in ids[target]["incidents"]}
            ids[target]["incidents"] += [i for i in ids[e["candidate"]]["incidents"] if i["sys_id"] not in have]
            decision[target]["changes"].setdefault("merged", []).append(e["candidate"])
    return log


# --- run ---------------------------------------------------------------------------------------
def run(context):
    dry, tag = context.get("dry_run", False), context["tag"]
    svc = SNClient(dry_run=dry)
    admin = SNClient(dry_run=dry, user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
    print(f"{LOG} run {context['run_id']} as {svc.session.auth[0]} dry_run={dry}")
    if not dry and svc.count("problem", f"correlation_id={tag}"):
        raise SystemExit(f"{LOG} problems already exist for this run; reset or use a new run")

    incidents = svc.get_all("incident", f"correlation_id={tag}", fields=FIELDS)
    ci_names = {r["sys_id"]: r["name"] for r in svc.get_all("cmdb_ci", f"correlation_id={tag}", fields=["sys_id", "name"])}
    print(f"{LOG} {len(incidents)} tagged incidents, {len(ci_names)} CIs")
    method = context.get("method") or config.DETECT_METHOD
    candidates = detect(incidents, ci_names, method)
    print(f"{LOG} clustering method: {method}")
    print(f"{LOG} {len(candidates)} candidates (>= {config.DETECT_MIN_INCIDENTS} in {config.DETECT_WINDOW_DAYS} days on a CI or service)")

    log = gate(candidates, context.get("auto_approve", False), context.get("ask", input))
    context.setdefault("log", []).extend(log)

    closed_states = {config.choice_value("incident.state", "Closed"), config.choice_value("incident.state", "Canceled")}
    new_state = config.choice_value("problem.state", "New")
    problems = []
    for c in candidates:
        e = next(x for x in log if x["candidate"] == c["id"])
        if e["decision"] != "approve":
            continue
        note = (f"Human Gate 1: approved by {e['by']} at {e['at']}{' (auto-approve rehearsal)' if e['auto'] else ''}. "
                f"Candidate {c['id']}: {c['incident_count']} incidents, peak {c['peak_14d']} in "
                f"{config.DETECT_WINDOW_DAYS} days, confidence {c['confidence']}, score {c['score']}, clustering {method}."
                + (f" Changes: {json.dumps(e['changes'])}" if e["changes"] else ""))
        sample = "\n".join(f"{i['number']} {i['opened_at'][:10]} {i['short_description']}" for i in c["incidents"][:10])
        p = svc.create("problem", {
            "short_description": c["title"], "correlation_id": tag, "state": new_state,
            "cmdb_ci": c["cmdb_ci"], "business_service": c["business_service"], "impact": c["impact"],
            "urgency": c["urgency"], "assignment_group": c["assignment_group"],
            "first_reported_by_task": c["incidents"][0]["sys_id"] if c["incidents"] else "",
            "description": f"Detected by trend analysis on {c['where']}.\nLinked incidents ({len(c['incidents'])}), first 10:\n{sample}",
            "work_notes": note})
        linked = {"service_account": 0, "admin_closed_incident": 0}
        for i in c["incidents"]:
            if i["state"] in closed_states:
                admin.update("incident", i["sys_id"], {"parent": p["sys_id"]})
                linked["admin_closed_incident"] += 1
            else:
                svc.update("incident", i["sys_id"], {"parent": p["sys_id"]})
                linked["service_account"] += 1
        e["problem"] = p.get("number", "DRYRUN")
        e["linked"] = linked
        problems.append({"candidate": c["id"], "sys_id": p["sys_id"], "number": p.get("number", "DRYRUN"),
                         "incidents": [i["number"] for i in c["incidents"]], "linked_by": linked})
        print(f"{LOG} created {p.get('number', 'DRYRUN')} for {c['id']}, linked {len(c['incidents'])} incidents {linked}")

    result = {"candidates": [{k: v for k, v in c.items() if k != "incidents"} | {"incidents": [i["number"] for i in c["incidents"]]}
                             for c in candidates], "problems": problems, "method": method}
    context["detection"] = result
    evaluate(context, candidates)
    return result


# --- evaluation and success check --------------------------------------------------------------
def _key(context):
    return json.loads((config.RUNS_DIR / f"{context['run_id']}_answer_key.json").read_text())["patterns"]


def evaluate(context, candidates):
    """Compare candidates to the answer key (evaluation only; detection never reads it)."""
    if not (config.RUNS_DIR / f"{context['run_id']}_answer_key.json").exists():
        print(f"{LOG} no answer key for this run (dry run of a new run?); evaluation skipped")
        return
    key = _key(context)
    print(f"\n{LOG} EVALUATION vs answer key (candidates)")
    for pid, p in key.items():
        planted = set(p["incidents"])
        best = max(candidates, key=lambda c: len(planted & set(i["number"] for i in c["incidents"])), default=None)
        got = set(i["number"] for i in best["incidents"]) if best else set()
        hit = len(planted & got)
        recall, precision = hit / len(planted), (hit / len(got) if got else 0)
        if p["expect_problem"]:
            ok = recall >= 0.8 and precision >= 0.8
            print(f"  {'PASS' if ok else 'FAIL'}  {pid}) {p['ci']:15} -> {best['id'] if best else '-'} "
                  f"{best['confidence'] if best else ''}  recall {recall:.0%}  precision {precision:.0%}")
        else:
            ok = not best or hit == 0 or best["confidence"] == "Low"
            print(f"  {'PASS' if ok else 'FAIL'}  {pid}) red herring  -> "
                  f"{(best['id'] + ' ' + best['confidence']) if best and hit else 'not proposed'}")
    planted_all = {n for p in key.values() for n in p["incidents"]}
    for c in candidates:
        if c["confidence"] != "Low" and not planted_all & set(i["number"] for i in c["incidents"]):
            print(f"  NOTE  false positive: {c['id']} {c['confidence']} {c['where']}")


def compare(context):
    """Run detection with every clustering method and evaluate each against the answer key (read-only)."""
    svc = SNClient()
    incidents = svc.get_all("incident", f"correlation_id={context['tag']}", fields=FIELDS)
    ci_names = {r["sys_id"]: r["name"] for r in svc.get_all("cmdb_ci", f"correlation_id={context['tag']}", fields=["sys_id", "name"])}
    for method in METHODS:
        candidates = detect([dict(i) for i in incidents], ci_names, method)
        print(f"\n##### {method}: {len(candidates)} candidates "
              f"({sum(c['confidence'] != 'Low' for c in candidates)} High/Medium)")
        for c in candidates:
            print(f"    {c['id']:3} {c['confidence']:6} n={c['incident_count']:2} peak={c['peak_14d']}  {c['where']}  "
                  f"({c['title'].rsplit('(', 1)[-1]}")
        evaluate(context, candidates)


def check(context):
    """Success check recomputed from the PDI: problems exist, incidents linked, decisions logged."""
    admin = SNClient(user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
    key, ok = _key(context), True
    print(f"{LOG} CHECK (recomputed from the PDI)")
    problems = admin.get_all("problem", f"correlation_id={context['tag']}", fields=["sys_id", "number", "short_description"])
    for p in problems:
        linked = {r["number"] for r in admin.get_all("incident", f"parent={p['sys_id']}", fields=["number"])}
        expected = next((set(x["incidents"]) for x in context["detection"]["problems"] if x["number"] == p["number"]), set())
        notes = admin.get_all("sys_journal_field", f"element_id={p['sys_id']}^element=work_notes", fields=["value"])
        gate_note = any("Human Gate 1" in n["value"] for n in notes)
        match = max(key.items(), key=lambda kv: len(linked & set(kv[1]["incidents"])))
        label = f"{match[0]}) {match[1]['ci']}" if linked & set(match[1]["incidents"]) else "none (unplanted)"
        good = linked == expected and gate_note
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {p['number']} linked={len(linked):3} (expected {len(expected)})  "
              f"gate note={gate_note}  best match: {label}")
    for pid, p in key.items():
        planted = set(p["incidents"])
        opened = [x for x in problems if planted & {r["number"] for r in admin.get_all("incident", f"parent={x['sys_id']}", fields=["number"])}]
        good = bool(opened) if p["expect_problem"] else not opened
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  pattern {pid}) {'problem opened' if opened else 'no problem opened'}"
              f"{'' if p['expect_problem'] else ' (red herring)'}")
    decisions = [e for e in context.get("log", []) if e.get("gate") == 1]
    good = bool(decisions) and all({"by", "at", "decision"} <= e.keys() for e in decisions)
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {len(decisions)} gate-1 decisions logged with who/when/what")
    print(f"{LOG} CHECK RESULT: {'ALL PASSED' if ok else 'FAILED'}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--auto-approve", action="store_true", help="rehearsal only: approve High/Medium, reject Low")
    ap.add_argument("--cluster", choices=METHODS, help=f"clustering method (default {config.DETECT_METHOD})")
    ap.add_argument("--compare", action="store_true", help="evaluate every clustering method; no gate, no writes")
    ap.add_argument("--check", metavar="RUN_ID")
    args = ap.parse_args()
    try:
        if args.compare:
            if not args.run_id:
                ap.error("--compare needs --run-id")
            compare(config.load_context(args.run_id))
            return
        if args.check:
            sys.exit(0 if check(config.load_context(args.check)) else 1)
        if not args.run_id:
            ap.error("--run-id is required")
        ctx = config.load_context(args.run_id)
        ctx.update(dry_run=args.dry_run, auto_approve=args.auto_approve, method=args.cluster)
        run(ctx)
        if not args.dry_run:
            for k in ("dry_run", "auto_approve", "method"):
                ctx.pop(k, None)
            config.save_context(ctx)
            print(f"{LOG} context saved to runs/{ctx['run_id']}.json")
            sys.exit(0 if check(ctx) else 1)
    except SNError as e:
        sys.exit(f"{LOG} ERROR: {e}")


if __name__ == "__main__":
    main()
