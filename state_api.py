"""Problem state moves through a small project-owned Scripted REST API.

Why: on this PDI problem.state is read-only (dictionary override), and the REST Table API silently
drops read-only fields. Server-side code (like the Confirm button) can still move states, so this
endpoint does the move on the server. It still enforces the problem state model and its conditions,
needs the problem_coordinator role, only touches PMSYNTH-tagged problems, and only sets the listed
fields.

    python state_api.py --install    # as admin: create or refresh the endpoint (idempotent)
    python state_api.py --test       # as pm_agent_svc: read-only checks of the endpoint
    python state_api.py --remove     # as admin: delete it (reset.py also does this)

Agents call move(svc, problem, label, fields).
"""
import argparse
import json
import sys
import time

import config
from sn_client import SNClient, SNError

NAME = f"[{config.TAG_PREFIX}] Problem State"
SERVICE_ID = "pmsynth_problem_state"
API_FILE = config.ROOT / "config" / "state_api.json"   # base path, written by --install
FIELDS = ["cause_notes", "workaround", "fix_notes", "close_notes", "resolution_code", "assigned_to"]
# Journal fields are not set here: setValue() on work_notes in server code creates no journal entry
# (seen in phase 4), so move() posts them through the Table API, where work notes do work.
JOURNAL = ("work_notes", "comments")

SCRIPT = """(function process(request, response) {
    var FIELDS = %s;
    var body = request.body.data || {};
    var check = body.check_only === true;
    var gr = new GlideRecord('problem');
    if (!body.sys_id || !gr.get(body.sys_id)) {
        response.setStatus(404);
        return {ok: false, error: 'problem not found'};
    }
    if ((gr.getValue('correlation_id') || '').indexOf('%s-') !== 0) {
        response.setStatus(403);
        return {ok: false, error: 'only %s-tagged problems can be moved by this API'};
    }
    if (!gs.hasRole('problem_coordinator')) {
        response.setStatus(403);
        return {ok: false, error: "caller needs the problem_coordinator role"};
    }
    var fields = body.fields || {};
    for (var k in fields) {
        if (FIELDS.indexOf(k) < 0) {
            response.setStatus(400);
            return {ok: false, error: 'field not allowed: ' + k};
        }
        gr.setValue(k, fields[k]);
    }
    var from = gr.getValue('state'), to = String(body.state);
    var ev = new ProblemModel(gr.prb_model.getRefRecord()).evaluateTransition(gr, to);
    var failed = [];
    for (var i = 0; ev.conditions && i < ev.conditions.length; i++)
        if (!ev.conditions[i].passed) failed.push(ev.conditions[i].condition.name);
    if (!ev.transition_available) {
        response.setStatus(409);
        return {ok: false, from: from, to: to, error: 'problem state model refused ' + from + ' -> ' + to +
                (failed.length ? ' (failed: ' + failed.join(', ') + ')' : ' (no such transition)')};
    }
    if (check) return {ok: true, from: from, to: to, check_only: true};
    gr.setValue('state', to);
    gr.update();
    var after = new GlideRecord('problem');
    after.get(gr.getUniqueValue());
    var ok = after.getValue('state') === to;
    response.setStatus(ok ? 200 : 409);
    return {ok: ok, from: from, to: to, state: after.getValue('state'),
            error: ok ? '' : 'update did not stick (a business rule aborted it)'};
})(request, response);""" % (json.dumps(FIELDS), config.TAG_PREFIX, config.TAG_PREFIX)


# Knowledge articles publish the same way (phase 5): setting workflow_state through the Table API is
# ignored; the Publish button calls KnowledgeUIAction().publish(), which runs the KB's publish flow.
PUBLISH_SCRIPT = """(function process(request, response) {
    var body = request.body.data || {};
    var gr = new GlideRecord('kb_knowledge');
    if (!body.sys_id || !gr.get(body.sys_id)) {
        response.setStatus(404);
        return {ok: false, error: 'article not found'};
    }
    if ((gr.getValue('short_description') || '').indexOf('[%s]') !== 0) {
        response.setStatus(403);
        return {ok: false, error: 'only [%s]-tagged articles can be published by this API'};
    }
    if (gr.getValue('workflow_state') === 'published')
        return {ok: true, workflow_state: 'published', already: true};
    if (!new KBKnowledge().canPublish(gr)) {
        response.setStatus(409);
        return {ok: false, workflow_state: gr.getValue('workflow_state'), error: 'canPublish() is false for this user and article'};
    }
    new global.KnowledgeUIAction().publish(gr);
    var after = new GlideRecord('kb_knowledge');
    after.get(gr.getUniqueValue());
    return {ok: true, workflow_state: after.getValue('workflow_state')};
})(request, response);""" % (config.TAG_PREFIX, config.TAG_PREFIX)
OPERATIONS = [("move", "/move", SCRIPT, "move one problem one state"),
              ("publish_article", "/publish_article", PUBLISH_SCRIPT, "publish one knowledge article")]


def _admin():
    return SNClient(user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")


def install():
    a = _admin()
    d = a.get("sys_ws_definition", f"name={NAME}", fields=["sys_id"])
    data = {"name": NAME, "service_id": SERVICE_ID, "active": "true",
            "short_description": f"{config.TAG_PREFIX} project: moves tagged problems between states server-side"}
    d = a.update("sys_ws_definition", d[0]["sys_id"], data) if d else a.create("sys_ws_definition", data)
    base = a.get("sys_ws_definition", f"sys_id={d['sys_id']}", fields=["base_uri"])[0]["base_uri"]
    for name, path, script, desc in OPERATIONS:
        op = {"web_service_definition": d["sys_id"], "name": name, "http_method": "POST", "relative_path": path,
              "operation_script": script, "active": "true", "requires_authentication": "true",
              "short_description": f"{config.TAG_PREFIX}: {desc}"}
        have = a.get("sys_ws_operation", f"web_service_definition={d['sys_id']}^name={name}", fields=["sys_id"])
        a.update("sys_ws_operation", have[0]["sys_id"], op) if have else a.create("sys_ws_operation", op)
        print(f"installed {NAME}: POST {base}{path}")
    API_FILE.write_text(json.dumps({"base_uri": base}, indent=2))
    print("saved to config/state_api.json")


def remove(sn=None, apply=True):
    """Delete the endpoint. Returns how many records (definition + operations) matched."""
    a = sn or _admin()
    ops = a.get_all("sys_ws_operation", f"web_service_definition.name={NAME}", fields=["sys_id"])
    defs = a.get_all("sys_ws_definition", f"name={NAME}", fields=["sys_id"])
    if apply:
        for r in ops:
            a.delete("sys_ws_operation", r["sys_id"])
        for r in defs:
            a.delete("sys_ws_definition", r["sys_id"])
        API_FILE.unlink(missing_ok=True)
    return len(ops) + len(defs)


def _path(op="/move"):
    if not API_FILE.exists():
        raise SNError("state API not installed: run `python state_api.py --install` (as admin) first")
    return json.loads(API_FILE.read_text())["base_uri"] + op


def call(svc, sys_id, state, fields=None, check_only=False):
    """POST to the endpoint; returns its JSON result (raises SNError with the reason on refusal)."""
    body = {"sys_id": sys_id, "state": state, "fields": fields or {}, "check_only": check_only}
    try:
        return svc._request("POST", _path(), json=body)["result"]
    except SNError as e:
        text = str(e)
        start = text.find("{")
        if start >= 0:
            try:
                raise SNError(json.loads(text[start:])["result"]["error"]) from None
            except (ValueError, KeyError, TypeError):
                pass
        raise


def _post(svc, op, body):
    try:
        return svc._request("POST", _path(op), json=body)["result"]
    except SNError as e:
        text = str(e)
        start = text.find("{")
        if start >= 0:
            try:
                raise SNError(json.loads(text[start:])["result"]["error"]) from None
            except (ValueError, KeyError, TypeError):
                pass
        raise


def publish(svc, sys_id, number, wait=20):
    """Publish one tagged knowledge article; the publish flow may finish a few seconds later, so poll."""
    if svc.dry_run:
        print(f"[dry-run] PUBLISH article {number}")
        return True
    r = _post(svc, "/publish_article", {"sys_id": sys_id})
    state = r.get("workflow_state")
    for _ in range(wait):
        if state == "published":
            return True
        time.sleep(1)
        state = svc.get("kb_knowledge", f"sys_id={sys_id}", fields=["workflow_state"])[0]["workflow_state"]
    return state == "published"


def move(svc, p, label, fields=None):
    """Move problem p (dict with sys_id, number, state) to the state labelled `label`."""
    want = config.choice_value("problem.state", label)
    fields = dict(fields or {})
    journal = {k: fields.pop(k) for k in JOURNAL if k in fields}
    if svc.dry_run:
        print(f"[dry-run] MOVE problem {p['number']} {p['state']} -> {want} ({label}) with {fields}")
    else:
        r = call(svc, p["sys_id"], want, fields)
        if not r.get("ok") or r.get("state") != want:
            raise SNError(f"{p['number']}: {r.get('error') or 'state did not change'}")
    p["state"] = want
    if journal:
        svc.update("problem", p["sys_id"], journal)


def test():
    """Read-only checks as pm_agent_svc (check_only never writes)."""
    svc = SNClient()
    tag = f"{config.TAG_PREFIX}-"
    probs = svc.get_all("problem", f"correlation_idSTARTSWITH{tag}", fields=["sys_id", "number", "state"])
    st = {config.choice_value("problem.state", l): l for l in ("New", "Assess", "Root Cause Analysis")}
    ok = True
    new = next((p for p in probs if st.get(p["state"]) == "New"), None)
    assess = next((p for p in probs if st.get(p["state"]) == "Assess"), None)
    rca = config.choice_value("problem.state", "Root Cause Analysis")
    if new:
        try:
            call(svc, new["sys_id"], rca, check_only=True)
            print(f"FAIL  {new['number']} New -> Root Cause Analysis was allowed (expected a refusal)")
            ok = False
        except SNError as e:
            print(f"PASS  {new['number']} New -> Root Cause Analysis refused: {e}")
    if assess:
        r = call(svc, assess["sys_id"], rca, check_only=True)
        print(f"{'PASS' if r.get('ok') else 'FAIL'}  {assess['number']} Assess -> Root Cause Analysis allowed (check only)")
        ok &= bool(r.get("ok"))
    try:
        call(svc, probs[0]["sys_id"], rca, fields={"short_description": "x"}, check_only=True)
        print("FAIL  disallowed field accepted")
        ok = False
    except SNError as e:
        print(f"PASS  disallowed field refused: {e}")
    print("RESULT:", "STATE API OK" if ok else "STATE API FAILED")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true")
    g.add_argument("--test", action="store_true")
    g.add_argument("--remove", action="store_true")
    args = ap.parse_args()
    try:
        if args.install:
            install()
        elif args.remove:
            print(f"removed {remove()} records")
        else:
            sys.exit(0 if test() else 1)
    except SNError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
