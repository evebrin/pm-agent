"""Synthetic Data Agent: seeds groups, users, CIs and ~200 incidents over the last 60 days,
with four planted recurring patterns (a-d), one red herring (e) and background noise.

Runs as admin (item 13: the basic-auth role stays on admin for seeding). Every record is
tagged: correlation_id = PMSYNTH-<run_id> on incidents and CIs, a "[PMSYNTH]" name prefix
on groups and a "pmsynth." user_name prefix on users. Writes a hidden answer key to
runs/<run_id>_answer_key.json for evaluation only.

    python synthetic_data_agent.py [--dry-run] [--seed N]
    python synthetic_data_agent.py --check <run_id>
"""
import argparse
import json
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import config
from sn_client import SNClient, SNError

LOG = "[synthetic]"
DAYS = 60
FMT = "%Y-%m-%d %H:%M:%S"

GROUPS = ["Service Desk", "Database Support", "Messaging", "Network Operations",
          "Business Applications", "Collaboration"]
# (first, last, group or None = caller only)
USERS = [("Jordan", "Lee", "Service Desk"), ("Priya", "Nair", "Service Desk"), ("Marcus", "Hill", "Database Support"),
         ("Elena", "Cruz", "Database Support"), ("Sam", "Okafor", "Messaging"), ("Hannah", "Weiss", "Network Operations"),
         ("Diego", "Ramos", "Network Operations"), ("Aisha", "Khan", "Business Applications"),
         ("Tom", "Becker", "Collaboration"), ("Grace", "Liu", None), ("Omar", "Haddad", None), ("Nina", "Petrova", None),
         ("Luis", "Ortega", None), ("Chloe", "Martin", None), ("Ben", "Adeyemi", None)]
# (key, table, name, extra fields)
CIS = [("svc_email", "cmdb_ci_service", "Email Service", {}),
       ("svc_vpn", "cmdb_ci_service", "VPN Service", {}),
       ("svc_payroll", "cmdb_ci_service", "Payroll Service", {}),
       ("svc_sharepoint", "cmdb_ci_service", "SharePoint Online", {}),
       ("db_prod_01", "cmdb_ci_server", "db-prod-01", {"os_version": "RHEL 8.8"}),
       ("mail_01", "cmdb_ci_server", "mail-01", {}),
       ("auth_01", "cmdb_ci_server", "auth-01", {}),
       ("payroll_app_01", "cmdb_ci_server", "payroll-app-01", {}),
       ("sp_web_01", "cmdb_ci_server", "sp-web-01", {}),
       ("file_01", "cmdb_ci_server", "file-01", {}),
       ("web_01", "cmdb_ci_server", "web-01", {}),
       ("orders_db", "cmdb_ci_database", "ordersdb", {}),
       ("crm_db", "cmdb_ci_database", "crmdb", {}),
       ("app_payroll", "cmdb_ci_appl", "Payroll Application", {}),
       ("app_crm", "cmdb_ci_appl", "CRM Application", {}),
       ("app_hr_portal", "cmdb_ci_appl", "HR Portal", {}),
       ("app_auth_proxy", "cmdb_ci_appl", "Mail Auth Proxy", {}),
       ("vpn_gw_01", "cmdb_ci_netgear", "vpn-gw-01", {"firmware_version": "7.2.1"}),
       ("vpn_gw_02", "cmdb_ci_netgear", "vpn-gw-02", {"firmware_version": "7.4.0"}),
       ("core_sw_01", "cmdb_ci_netgear", "core-sw-01", {"firmware_version": "16.12.4"})]

# --- planted patterns ------------------------------------------------------------------
PATTERNS = {
    "a": {"title": "Recurring disk-space / slow-query incidents on db-prod-01",
          "ci": "db_prod_01", "service": None, "group": "Database Support", "category": "database",
          "subcategories": ["db_performance", "query_issue"], "impact": "2", "urgency": "2",
          "close_label": "Resolved – Workaround Applied",
          "short": ["Orders app very slow, queries timing out", "Slow queries on ordersdb",
                    "db-prod-01 disk almost full alert", "Order lookups taking over 30 seconds",
                    "ordersdb reports are hanging", "Low disk space warning on db-prod-01 /data",
                    "Checkout page slow, database wait times high", "Orders database unresponsive"],
          "notes": ["df -h on db-prod-01 shows /data at {pct}% used; /data/pg_log is {gb} GB.",
                    "Cleared old files from /data/pg_log, free space back to normal and queries recovered.",
                    "pg_log directory keeps growing; no rotation seems to run on db-prod-01 since the migration.",
                    "Same as last time: /data filled up by database logs, purged manually."],
          "root_cause": "Database log rotation was not set up when ordersdb moved to db-prod-01, so "
                        "/data/pg_log grows until the disk fills and queries slow down.",
          "workaround": "Purge old files from /data/pg_log on db-prod-01.",
          "fix": "Configure log rotation and retention for /data/pg_log on db-prod-01 and add a disk alert at 80%."},
    "b": {"title": "Email authentication failures every Monday after the weekly certificate job",
          "ci": "mail_01", "service": "svc_email", "group": "Messaging", "category": "collaboration_tools",
          "subcategories": ["email"], "impact": "2", "urgency": "1",
          "close_label": "Resolved – Workaround Applied",
          "short": ["Outlook keeps asking for password", "Cannot sign in to email", "Email authentication failed",
                    "Mobile mail app login error", "Outlook disconnected, credentials rejected",
                    "Mail client says certificate not trusted"],
          "notes": ["Auth proxy on mail-01 still presenting the old certificate after the Sunday 23:00 CERT-RENEW-WEEKLY job.",
                    "Restarted the mail auth proxy service, logins working again.",
                    "Seen again this Monday morning; cert renewal log shows success but the proxy was not reloaded.",
                    "Workaround: restart Mail Auth Proxy on mail-01."],
          "root_cause": "The weekly CERT-RENEW-WEEKLY job renews the certificate but does not restart the "
                        "Mail Auth Proxy on mail-01, which keeps serving the expired certificate.",
          "workaround": "Restart the Mail Auth Proxy service on mail-01 after the Sunday renewal job.",
          "fix": "Add a service reload step to CERT-RENEW-WEEKLY and a post-renewal certificate check."},
    "c": {"title": "VPN drops for users on vpn-gw-01 (firmware 7.2.1)",
          "ci": "vpn_gw_01", "service": "svc_vpn", "group": "Network Operations", "category": "network",
          "subcategories": ["vpn"], "impact": "3", "urgency": "2",
          "close_label": "Resolved – Workaround Applied",
          "short": ["VPN disconnects every hour", "VPN connection dropped during meeting",
                    "Remote access keeps dropping", "VPN tunnel drops, have to reconnect",
                    "Lost VPN connection again", "VPN unstable working from home"],
          "notes": ["User connected through vpn-gw-01 (firmware 7.2.1); tunnel dropped after about 60 minutes.",
                    "Moved user to vpn-gw-02, no drops reported since.",
                    "Users on vpn-gw-02 (firmware 7.4.0) are not affected.",
                    "Vendor advisory mentions dead-peer-detection timer bug in 7.2.x releases."],
          "root_cause": "Firmware 7.2.1 on vpn-gw-01 has a dead-peer-detection timer bug that drops tunnels "
                        "after about an hour; vpn-gw-02 on 7.4.0 is unaffected.",
          "workaround": "Reconnect, or route the user through vpn-gw-02.",
          "fix": "Upgrade vpn-gw-01 firmware to 7.4.0."},
    "d": {"title": "Payroll application timeouts on the last business day of the month",
          "ci": "payroll_app_01", "service": "svc_payroll", "group": "Business Applications",
          "category": "internal_applications", "subcategories": ["hr_systems"], "impact": "1", "urgency": "2",
          "close_label": "Resolved – Fixed Without Action",
          "short": ["Payroll system timing out", "Cannot submit timesheets in payroll", "Payroll app error 504",
                    "Payroll screens freezing", "Payroll approvals failing with timeout"],
          "notes": ["PAYROLL-MONTHEND batch started at 17:00 and holds locks on the payroll database.",
                    "App recovered on its own once the month-end batch finished.",
                    "Same timeouts as last month-end, during the batch window.",
                    "Users can retry after the batch completes."],
          "root_cause": "The PAYROLL-MONTHEND batch runs at 17:00 on the last business day, during working "
                        "hours, and locks the payroll database so the application times out.",
          "workaround": "Wait for the batch to finish, then retry.",
          "fix": "Reschedule PAYROLL-MONTHEND to run after hours and split it into smaller commits."},
}
# Red herring: a burst on SharePoint that crosses the count threshold but has unrelated causes.
RED_HERRING = [
    ("file_sharing", "Cannot open shared folder in SharePoint", "User was not in the site members group; added.", "Resolved – Permanent Fix Applied"),
    ("file_sharing", "SharePoint file shows old version", "Browser cache; cleared cache and file shows correctly.", "Resolved – Fixed by Caller"),
    ("sync_error", "OneDrive sync stuck for SharePoint library", "Sync client was two versions behind; updated.", "Resolved – Permanent Fix Applied"),
    ("file_sharing", "Deleted file missing from SharePoint", "Restored from the site recycle bin.", "Resolved – Permanent Fix Applied"),
    ("access_request", "Need access to Finance SharePoint site", "Access approved by site owner.", "Resolved – Permanent Fix Applied"),
    ("application_error", "SharePoint page shows error in old browser", "Unsupported browser; user switched to Edge.", "Resolved – Fixed by Caller"),
    ("licensing", "SharePoint says no license assigned", "New starter's license had not been assigned yet.", "Resolved – Permanent Fix Applied"),
]
RED_HERRING_CATEGORY = {"file_sharing": "collaboration_tools", "sync_error": "cloud_services",
                        "access_request": "service_requests", "application_error": "software", "licensing": "software"}

# Background noise: (category, subcategory, ci key or None, group, descriptions, resolution, close label)
NOISE = [
    ("access", "password_reset", None, "Service Desk", ["Forgot my password", "Password expired, need reset", "Locked out after password change"], "Password reset and user signed in.", "Resolved – Permanent Fix Applied"),
    ("access", "account_lockout", "auth_01", "Service Desk", ["Account locked", "Locked out of my account again"], "Unlocked account; caller had an old password on their phone.", "Resolved – Permanent Fix Applied"),
    ("access", "mfa_issue", None, "Service Desk", ["MFA prompt not arriving", "New phone, need MFA reset"], "Re-registered the authenticator app.", "Resolved – Permanent Fix Applied"),
    ("hardware", "laptop_desktop", None, "Service Desk", ["Laptop will not boot", "Laptop very hot and slow", "Docking station not detected"], "Hardware swapped or drivers updated.", "Resolved – Permanent Fix Applied"),
    ("hardware", "printer", None, "Service Desk", ["Printer on 3rd floor jammed", "Cannot print to office printer", "Printer offline"], "Cleared queue and re-added printer.", "Resolved – Permanent Fix Applied"),
    ("hardware", "monitor", None, "Service Desk", ["Second monitor flickering", "Monitor no signal"], "Replaced cable.", "Resolved – Permanent Fix Applied"),
    ("inquiry", "how_to", None, "Service Desk", ["How do I set an out of office", "How to share my calendar", "Where do I find the expense form"], "Explained steps to caller.", "Resolved – Fixed by Caller"),
    ("software", "installation", None, "Service Desk", ["Need Visio installed", "Install Python for data course", "Request to install Zoom"], "Installed from the software centre.", "Resolved – Permanent Fix Applied"),
    ("software", "application_error", "app_crm", "Business Applications", ["CRM crashes when exporting", "CRM report shows error", "CRM search not returning results"], "Cleared user profile cache in CRM.", "Resolved – Permanent Fix Applied"),
    ("internal_applications", "internal_web_portal", "app_hr_portal", "Business Applications", ["HR portal page not loading", "Cannot update address in HR portal"], "Portal session reset.", "Resolved – Fixed Without Action"),
    ("database", "db_connection", "crm_db", "Database Support", ["CRM cannot connect to database", "crmdb connection refused"], "Connection pool restarted.", "Resolved – Fixed Without Action"),
    ("network", "wireless", "core_sw_01", "Network Operations", ["Wi-Fi weak in meeting room B", "Wi-Fi keeps disconnecting on 2nd floor"], "Access point rebooted.", "Resolved – Fixed Without Action"),
    ("network", "dns", "core_sw_01", "Network Operations", ["Intranet site not resolving", "Cannot reach internal site by name"], "Flushed DNS cache on client.", "Resolved – Fixed by Caller"),
    ("network", "vpn", "vpn_gw_02", "Network Operations", ["VPN will not connect from hotel", "VPN slow on home internet"], "Caller's home or hotel network was the cause.", "Resolved – Fixed by Caller"),
    ("collaboration_tools", "video_conference", None, "Collaboration", ["Teams camera not working", "Cannot share screen in Teams"], "Updated Teams client.", "Resolved – Permanent Fix Applied"),
    ("collaboration_tools", "calendar", "mail_01", "Messaging", ["Meeting invites not showing", "Calendar not syncing to phone"], "Re-added account on device.", "Resolved – Fixed by Caller"),
    ("performance", "slow_system", "web_01", "Business Applications", ["Company website slow", "Intranet pages slow to load"], "Could not reproduce; performance normal.", "Not Resolved – Not Reproducible"),
    ("cloud_services", "storage_issue", "file_01", "Service Desk", ["Shared drive full message", "Cannot save to shared drive"], "Moved old files to archive share.", "Resolved – Permanent Fix Applied"),
    ("security", "phishing", None, "Service Desk", ["Suspicious email received", "Reported phishing email"], "Email confirmed as phishing and blocked.", "Resolved – Permanent Fix Applied"),
]
NOISE_COUNT = 125


def _last_business_days(start, end):
    """Last weekday of each month whose date falls in [start, end)."""
    out, d = [], start.replace(day=1)
    while d < end:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = nxt - timedelta(days=1)
        while last.weekday() >= 5:
            last -= timedelta(days=1)
        if start <= last < end:
            out.append(last)
        d = nxt
    return out


def _plan(rng, now):
    """Build the full incident plan (no writes). Returns list of dicts with a 'pattern' key."""
    start = now - timedelta(days=DAYS)
    plan = []

    def at(day, hour_lo=14, hour_hi=22):  # business hours, UTC (about 8-17 US Central)
        return (day.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(hours=rng.randint(hour_lo, hour_hi - 1), minutes=rng.randint(0, 59)))

    def add(pattern, when, **fields):
        if when < now:
            plan.append({"pattern": pattern, "opened": when, **fields})

    # a) bursts that get closer together as the disk refills after each manual purge
    for day_offset in (50, 49, 38, 37, 36, 27, 26, 25, 18, 17, 16, 11, 10, 6, 5, 2):
        add("a", at(now - timedelta(days=day_offset)))
    # b) every Monday morning in the window, 2-3 incidents
    d = start
    while d < now:
        if d.weekday() == 0:
            for _ in range(rng.randint(2, 3)):
                add("b", at(d, 13, 17))
        d += timedelta(days=1)
    # c) VPN drops on vpn-gw-01, denser in the last 30 days
    for day_offset in sorted(rng.sample(range(1, 30), 11) + rng.sample(range(31, 58), 4), reverse=True):
        add("c", at(now - timedelta(days=day_offset)))
    # d) month-end batch window (late afternoon local = 22:00-24:00 UTC)
    for day in _last_business_days(start, now):
        for _ in range(7):
            add("d", day.replace(hour=22, minute=0, second=0, microsecond=0) + timedelta(minutes=rng.randint(0, 110)))
    # e) red herring: 7 SharePoint incidents within 10 days
    base = now - timedelta(days=rng.randint(15, 30))
    for i, item in enumerate(RED_HERRING):
        add("e", at(base + timedelta(days=rng.randint(0, 9))), red_herring=i)
    # noise
    for _ in range(NOISE_COUNT):
        add("noise", at(start + timedelta(days=rng.uniform(0, DAYS))), noise=rng.randrange(len(NOISE)))
    plan.sort(key=lambda p: p["opened"])
    return plan


def _incident_fields(rng, item, ref, now, tag):
    """Turn one plan item into incident fields."""
    users = ref["callers"]
    opened = item["opened"]
    age = now - opened
    if item["pattern"] in PATTERNS:
        p = PATTERNS[item["pattern"]]
        note = rng.choice(p["notes"]).format(pct=rng.randint(91, 99), gb=rng.randint(28, 60))
        f = {"short_description": rng.choice(p["short"]), "category": p["category"],
             "subcategory": rng.choice(p["subcategories"]), "cmdb_ci": ref["ci"][p["ci"]],
             "business_service": ref["ci"].get(p["service"] or "", ""), "impact": p["impact"], "urgency": p["urgency"],
             "group": p["group"], "close_label": p["close_label"], "close_notes": note, "work_notes": note}
    elif item["pattern"] == "e":
        sub, short, resolution, close_label = RED_HERRING[item["red_herring"]]
        f = {"short_description": short, "category": RED_HERRING_CATEGORY[sub], "subcategory": sub,
             "cmdb_ci": ref["ci"]["sp_web_01"], "business_service": ref["ci"]["svc_sharepoint"], "impact": "3",
             "urgency": "3", "group": "Collaboration", "close_label": close_label,
             "close_notes": resolution, "work_notes": resolution}
    else:
        cat, sub, ci, group, shorts, resolution, close_label = NOISE[item["noise"]]
        f = {"short_description": rng.choice(shorts), "category": cat, "subcategory": sub,
             "cmdb_ci": ref["ci"][ci] if ci else "", "impact": rng.choice("233"), "urgency": rng.choice("233"),
             "group": group, "close_label": close_label, "close_notes": resolution, "work_notes": resolution}
    group = f.pop("group")
    close_label = f.pop("close_label")
    caller = rng.choice(users)
    f.update({"caller_id": caller, "assignment_group": ref["group"][group],
              "assigned_to": rng.choice(ref["members"][group]), "opened_at": opened.strftime(FMT),
              "correlation_id": tag, "description": f["short_description"] + ". Reported by user."})
    if age > timedelta(days=3):
        resolved = opened + timedelta(hours=rng.uniform(1, 30))
        state_label = "Closed" if age > timedelta(days=7) else "Resolved"
        f.update({"state": config.choice_value("incident.state", state_label),
                  "close_code": config.choice_value("incident.close_code", close_label),
                  "resolved_at": resolved.strftime(FMT)})
        if state_label == "Closed":
            f["closed_at"] = (resolved + timedelta(days=3)).strftime(FMT)
    else:
        f["state"] = config.choice_value("incident.state", rng.choice(["New", "In Progress"]))
        f.pop("close_notes")
    return f


def run(context):
    dry = context.get("dry_run", False)
    tag = context["tag"]
    rng = random.Random(context["seed"])
    sn = SNClient(dry_run=dry, user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
    print(f"{LOG} run {context['run_id']} seed={context['seed']} dry_run={dry}")

    existing = sn.count("incident", f"correlation_idSTARTSWITH{config.TAG_PREFIX}-")
    if existing and not dry:
        raise SystemExit(f"{LOG} {existing} tagged incidents already exist; run `python reset.py --apply` first.")

    created = {}

    def make(table, data):
        r = sn.create(table, data)
        created.setdefault(table, []).append(r["sys_id"])
        return r

    ref = {"group": {}, "members": {g: [] for g in GROUPS}, "callers": [], "ci": {}}
    for g in GROUPS:
        ref["group"][g] = make("sys_user_group", {"name": f"[{config.TAG_PREFIX}] {g}", "description": tag})["sys_id"]
    for first, last, group in USERS:
        u = make("sys_user", {"user_name": f"pmsynth.{first[0].lower()}{last.lower()}", "first_name": first,
                              "last_name": last, "email": f"{first.lower()}.{last.lower()}@example.com"})["sys_id"]
        ref["callers"].append(u)
        if group:
            make("sys_user_grmember", {"group": ref["group"][group], "user": u})
            ref["members"][group].append(u)
    print(f"{LOG} {len(GROUPS)} groups, {len(USERS)} users created")
    for key, table, name, extra in CIS:
        ref["ci"][key] = make(table, {"name": name, "correlation_id": tag, **extra})["sys_id"]
    print(f"{LOG} {len(CIS)} CIs created")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    plan = _plan(rng, now)
    key = {pid: {**{k: p[k] for k in ("title", "root_cause", "workaround", "fix")},
                 "ci": dict((c[0], c[2]) for c in CIS)[p["ci"]], "expect_problem": True, "incidents": []}
           for pid, p in PATTERNS.items()}
    key["e"] = {"title": "Red herring: SharePoint burst with unrelated causes", "ci": "sp-web-01",
                "root_cause": None, "expect_problem": False, "incidents": []}
    for n, item in enumerate(plan, 1):
        r = make("incident", _incident_fields(rng, item, ref, now, tag))
        if item["pattern"] in key:
            key[item["pattern"]]["incidents"].append(r.get("number", "DRYRUN"))
        if n % 25 == 0:
            print(f"{LOG} {n}/{len(plan)} incidents")
    counts = {t: len(ids) for t, ids in created.items()}
    print(f"{LOG} done: {counts}")

    if not dry:
        key_path = config.RUNS_DIR / f"{context['run_id']}_answer_key.json"
        config.RUNS_DIR.mkdir(exist_ok=True)
        key_path.write_text(json.dumps({"run_id": context["run_id"], "patterns": key}, indent=2))
        print(f"{LOG} answer key written to {key_path.relative_to(config.ROOT)} (evaluation only)")
    result = {"created": created, "counts": counts, "pattern_sizes": {k: len(v["incidents"]) for k, v in key.items()}}
    context["synthetic"] = result
    return result


# --- post-change verification window (phase 6) ----------------------------------------------
def _window_plan(rng, start, end, continued):
    """Noise at the seeding rate, plus the planted patterns whose fix was not implemented (they keep occurring)."""
    days = (end - start).total_seconds() / 86400
    plan = []

    def at(day, hour_lo=14, hour_hi=22):
        return (day.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(hours=rng.randint(hour_lo, hour_hi - 1), minutes=rng.randint(0, 59)))

    def add(pattern, when, **fields):
        if start <= when < end:
            plan.append({"pattern": pattern, "opened": when, **fields})

    for _ in range(round(NOISE_COUNT * days / DAYS)):
        add("noise", at(start + timedelta(days=rng.uniform(0, days))), noise=rng.randrange(len(NOISE)))
    d = start
    while d < end:
        if "b" in continued and d.weekday() == 0:
            for _ in range(rng.randint(2, 3)):
                add("b", at(d, 13, 17))
        if "a" in continued and (d - start).days % 9 in (2, 3):
            add("a", at(d))
        if "c" in continued and rng.random() < 0.35:
            add("c", at(d))
        d += timedelta(days=1)
    if "d" in continued:
        for day in _last_business_days(start, end):
            for _ in range(7):
                add("d", day.replace(hour=22, minute=0, second=0, microsecond=0) + timedelta(minutes=rng.randint(0, 110)))
    return sorted(plan, key=lambda p: p["opened"])


def post_change(context, start, end, fixed_incident_sets, dry=False):
    """Seed tagged incidents opened in [start, end) (simulated dates after the changes).
    `fixed_incident_sets`: for each problem whose permanent fix was implemented, its linked incident numbers.
    The simulator (which owns the answer key) stops the planted patterns those problems cover; others continue."""
    key = json.loads((config.RUNS_DIR / f"{context['run_id']}_answer_key.json").read_text())["patterns"]
    fixed = {pid for pid, p in key.items() if p["expect_problem"]
             and any(len(set(p["incidents"]) & set(s)) * 2 >= len(p["incidents"]) for s in fixed_incident_sets)}
    tag = context["tag"]
    sn = SNClient(dry_run=dry, user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
    if context.get("synthetic", {}).get("post_change") and not dry:
        raise SystemExit(f"{LOG} the post-change window already exists for this run")
    partial = sn.count("incident", f"correlation_id={tag}^opened_at>={start.strftime(FMT)}")
    if partial and not dry:
        raise SystemExit(f"{LOG} {partial} tagged incidents already exist after {start:%Y-%m-%d %H:%M} (an interrupted "
                         "window?); delete those incidents before seeding the window again")
    ref = {"group": {}, "members": {g: [] for g in GROUPS}, "callers": [], "ci": {}}
    for g in sn.get_all("sys_user_group", f"nameSTARTSWITH[{config.TAG_PREFIX}] ", fields=["sys_id", "name"]):
        ref["group"][g["name"].split("] ", 1)[1]] = g["sys_id"]
    for m in sn.get_all("sys_user_grmember", f"group.nameSTARTSWITH[{config.TAG_PREFIX}] ", fields=["group", "user"]):
        name = next(k for k, v in ref["group"].items() if v == m["group"])
        ref["members"][name].append(m["user"])
    ref["callers"] = [u["sys_id"] for u in sn.get_all("sys_user", "user_nameSTARTSWITHpmsynth.", fields=["sys_id"])]
    by_name = {c[2]: c[0] for c in CIS}
    for c in sn.get_all("cmdb_ci", f"correlation_id={tag}", fields=["sys_id", "name"]):
        ref["ci"][by_name[c["name"]]] = c["sys_id"]
    continued = sorted(set(PATTERNS) - set(fixed))
    rng = random.Random(context["seed"] + 1)
    plan = _window_plan(rng, start, end, continued)
    print(f"{LOG} post-change window {start:%Y-%m-%d} to {end:%Y-%m-%d}: {len(plan)} incidents "
          f"(fixed patterns stop: {', '.join(sorted(fixed)) or 'none'}; continuing: {', '.join(continued) or 'none'})")
    numbers = []
    for item in plan:
        f = _incident_fields(rng, item, ref, end, tag)   # simulated clock: the end of the window
        f["description"] += " (post-change verification window)"
        numbers.append(sn.create("incident", f).get("number", "DRYRUN"))
    result = {"start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
              "incidents": numbers, "fixed": sorted(fixed), "continued": continued,
              "by_pattern": dict(Counter(i["pattern"] for i in plan))}
    if not dry:
        context.setdefault("synthetic", {})["post_change"] = result
    return result


def check(context):
    """Success check, recomputed from the PDI: counts match, every record tagged, patterns present."""
    sn = SNClient(user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
    tag, created = context["tag"], context["synthetic"]["created"]
    queries = {"incident": f"correlation_id={tag}", "sys_user_group": f"nameSTARTSWITH[{config.TAG_PREFIX}] ",
               "sys_user": "user_nameSTARTSWITHpmsynth.",
               "sys_user_grmember": f"group.nameSTARTSWITH[{config.TAG_PREFIX}] "}
    ok = True
    print(f"{LOG} CHECK counts and tags (recomputed from the PDI)")
    for table, ids in created.items():
        q = queries.get(table, f"correlation_id={tag}")
        found = {r["sys_id"] for r in sn.get_all(table, q, fields=["sys_id"])}
        good = found == set(ids)
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {table:20} generated={len(ids):3}  tagged in PDI={len(found):3}"
              + ("" if good else f"  untagged={len(set(ids) - found)} extra={len(found - set(ids))}"))

    key = json.loads((config.RUNS_DIR / f"{context['run_id']}_answer_key.json").read_text())["patterns"]
    rows = sn.get_all("incident", f"correlation_id={tag}", fields=["number", "opened_at", "state", "close_code"])
    opened = {r["number"]: datetime.strptime(r["opened_at"], FMT) for r in rows}
    print(f"{LOG} CHECK planted patterns (max incidents in any 14-day window)")
    for pid, p in key.items():
        times = sorted(opened[n] for n in p["incidents"])
        best = max((sum(1 for t in times if s <= t < s + timedelta(days=14)) for s in times), default=0)
        good = best >= 5
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {pid}) {p['ci']:12} total={len(times):3}  max/14d={best:3}"
              f"  {'(red herring: crosses threshold by design)' if not p['expect_problem'] else ''}")
    closed = config.choice_value("incident.state", "Closed")
    missing = [r["number"] for r in rows if r["state"] == closed and not r["close_code"]]
    ok &= not missing
    print(f"  {'PASS' if not missing else 'FAIL'}  closed incidents all have a close code"
          f"  states={dict(Counter(r['state'] for r in rows))}")
    print(f"{LOG} CHECK RESULT: {'ALL PASSED' if ok else 'FAILED'}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print intended writes, make none")
    ap.add_argument("--seed", type=int, default=451)
    ap.add_argument("--check", metavar="RUN_ID", help="re-run the success check for an existing run")
    args = ap.parse_args()
    try:
        if args.check:
            sys.exit(0 if check(config.load_context(args.check)) else 1)
        ctx = config.new_context(args.seed)
        ctx["dry_run"] = args.dry_run
        run(ctx)
        if not args.dry_run:
            config.save_context(ctx)
            print(f"{LOG} context saved to runs/{ctx['run_id']}.json")
            sys.exit(0 if check(ctx) else 1)
    except SNError as e:
        sys.exit(f"{LOG} ERROR: {e}")


if __name__ == "__main__":
    main()
