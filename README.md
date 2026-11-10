# pm-agent: ServiceNow Problem Management, end to end, with AI agents and human gates

ITSCM 451Z project (UW–Whitewater). A small multi-agent Python workflow runs ITIL 4 Problem
Management on a ServiceNow Personal Developer Instance (PDI): it seeds synthetic incidents with
hidden recurring problems, detects them, finds root causes, records known errors and knowledge
articles, raises and implements permanent-fix changes, verifies the fixes, closes the problems and
reports the results. A human approves every decision that matters, at four gates.

Plain Python 3.11+, `requests` and `python-dotenv`; no frameworks, no database, no web UI.
Design and ITIL 4 mapping: [ARCHITECTURE.md](ARCHITECTURE.md). Class demo: [DEMO_SCRIPT.md](DEMO_SCRIPT.md).

## What it does

| Step | Agent | Human gate |
|---|---|---|
| `synthetic` | Seeds 6 groups, 15 users, 20 CIs and 200 incidents over 60 days: 4 planted recurring problems, 1 decoy burst and background noise. Writes a hidden answer key used only for scoring. | |
| `detect` | Finds recurring incidents (same CI or service, 5+ in 14 days, TF-IDF text similarity), scores them, opens problems and links the incidents. | **Gate 1**: approve / reject / merge / edit each candidate |
| `rca` | Gathers evidence (work notes, CI details, time pattern, comparable CIs) and runs 5 Whys, or Kepner-Tregoe "is / is not" when the evidence is ambiguous. LLM with a rule-based fallback. | **Gate 2**: accept / edit / send back / reject |
| `knowledge` | Publishes a Known Error article per accepted RCA (description, cause, workaround), marks the problem a known error, notes the workaround on open incidents. | |
| `change` | Drafts a Normal change for the permanent fix (risk, implementation, backout and test plans), creates it after approval, records the ServiceNow approvals, simulates implementation. | **Gate 3**: approve before the change is created |
| `close` | Seeds a post-change verification window of incidents, checks each problem for recurrence, resolves and closes it. | **Gate 4**: close / keep open |
| `report` | Writes `reports/<run_id>.md` and `.json`; every number is recomputed from the PDI. | |

Each agent is one module with `run(context)` and a `check(context)` success check that rereads the PDI.

## Setup

1. **PDI.** Get a ServiceNow PDI at developer.servicenow.com and log in once in a browser (a
   hibernating PDI answers with an HTML page; the agents detect this and tell you to wake it).
2. **Admin REST access.** This PDI enforces the basic-auth restriction, so the admin account needs the
   `snc_basic_auth_api_access` role (System Security > Users > admin > Roles). Admin is used only for
   setup, seeding, reset, metadata reads and the documented exceptions below.
3. **Python.**
   ```
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
4. **`.env`.** `cp .env.example .env`, then fill in `SN_INSTANCE` (the URL in your browser's address
   bar, no path), `SN_ADMIN_USER` / `SN_ADMIN_PASSWORD`, and optionally the LLM settings. `.env` is in
   `.gitignore`. Close any editor that has `.env` open before running setup (see Lessons).
5. **Integration user** (assignment item 13). Creates `pm_agent_svc` with *Web service access only*,
   sets its password with `sysparm_input_display_value=true` so the platform hashes it, gives it the
   roles itil, itil_admin, user_admin and problem_coordinator, and writes `SN_USER` / `SN_PASSWORD`
   into `.env` without printing them. Rerunning it gives the user a **new** password.
   ```
   .venv/bin/python setup_integration_user.py
   ```
6. **Verify the instance.** Checks tables, fields and access, and reads the real choice values
   (states, close codes, risks…) into `config/instance.json`. Nothing in the code hardcodes them.
   ```
   .venv/bin/python verify_instance.py        # RESULT: ALL CHECKS PASSED
   ```
7. **State API.** Installs the project's small Scripted REST endpoint (see "Why a state API").
   ```
   .venv/bin/python state_api.py --install
   .venv/bin/python state_api.py --test       # RESULT: STATE API OK
   ```
8. **LLM (optional).** Set `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` (anthropic, openai, gemini, xai,
   ollama or any OpenAI-compatible server; see `.env.example`) and check with
   `.venv/bin/python llm.py --test`. Without an LLM every step uses its deterministic fallback.

## Running it

The whole workflow, a new run, with the four gates asked in the terminal:
```
.venv/bin/python orchestrator.py --all
```
Useful variations:
```
.venv/bin/python orchestrator.py --list                              # the steps
.venv/bin/python orchestrator.py --all --dry-run --auto-approve      # rehearsal: no writes at all
.venv/bin/python orchestrator.py --run-id <id> --all                 # continue an existing run
.venv/bin/python orchestrator.py --run-id <id> --step rca            # one step (plus its check)
```
`--dry-run` prints every intended write and makes none. `--auto-approve` answers each gate with its
recommendation, for rehearsals only; such decisions are logged as "auto-approve rehearsal". The gates
need a real terminal (they read your answer from the keyboard).

Each agent can also be run on its own; the useful extra options:

| Command | What it adds |
|---|---|
| `synthetic_data_agent.py [--seed N] [--dry-run]`, `--check <id>` | seed a new dataset (refuses if tagged incidents exist) |
| `detection_agent.py --run-id <id> [--cluster centroid\|agglomerative\|dbscan]` | choose the clustering inside each CI group |
| `detection_agent.py --run-id <id> --compare` | evaluate every clustering method against the answer key; no writes |
| `rca_agent.py --run-id <id> [--engine llm\|fallback] [--problem PRB…]` | pick the engine; rerun one problem (e.g. one sent back) |
| `rca_agent.py --run-id <id> --compare` | LLM vs fallback side by side, scored; no gate, no writes |
| `knowledge_agent.py --run-id <id> [--engine …] [--problem …]` | |
| `change_agent.py --run-id <id>` / `--close` | part 1 (Gate 3, change) / part 2 (window, Gate 4) |
| `reporting_agent.py --run-id <id>`, `--check <id>` | write the report / recompute and compare |
| `<agent>.py --check <run_id>` | rerun that phase's success check against the PDI |

The run context (`runs/<run_id>.json`) holds each step's results and every gate decision (who, when,
what changed); each decision is also written as a work note on the record. `runs/` is gitignored.

## Resetting

Everything the project creates is tagged: `correlation_id = PMSYNTH-<run_id>` on incidents, problems,
changes and CIs, a `[PMSYNTH]` prefix on groups, knowledge articles and the state API, and a
`pmsynth.` prefix on user names.
```
.venv/bin/python reset.py            # dry run: lists what would be deleted
.venv/bin/python reset.py --apply    # deletes it, children first, then checks nothing else changed
```
`reset.py` also deletes the records the platform creates around ours (approvals, change tasks,
conflict and approval-policy records, affected-CI links, knowledge versions, summaries and task links)
and compares the counts of untagged records before and after (`RESULT: RESET OK`). It does not touch
`pm_agent_svc` or the admin role.

## Why a state API

On this PDI `problem.state` is read-only to the Table API (a dictionary override), and the REST API
silently drops read-only fields; knowledge articles ignore `workflow_state` the same way. The UI buttons
work because they run server-side. `state_api.py` installs one tagged Scripted REST API with two
operations that do the same server-side: `move` (one problem, one state; the platform's state model and
its conditions still apply, the caller needs `problem_coordinator`, only `PMSYNTH-` problems and a short
list of fields) and `publish_article` (only `[PMSYNTH]` articles, through the platform's own publish).
`reset.py` removes it.

## Deviations from the spec, and why

- **Incidents link to problems through `task.parent`**: this PDI's incident table has no `problem_id`.
- **Known Error KB instead of "KCS Knowledge Base (demo data)"**: the integration user cannot read the
  KCS base; "Known Error" is the platform's own base for known errors (Known Error article template,
  itil/problem_coordinator can contribute, instant publish).
- **Accepted RCA ends in Root Cause Analysis, not Fix in Progress**: the state model needs cause *and*
  fix notes for Fix in Progress, so the move happens in the change step with the fix notes.
- **Admin exceptions** (all logged): linking *closed* incidents to problems (the integration user may not
  write closed incidents); reading metadata (`sys_choice`, dictionary, audit history); recording the
  Gate 3 decision on the change's ServiceNow approval records. The assignment-group approval belongs to
  our synthetic users; the CAB approval is addressed to demo users, so ServiceNow shows it as approved
  for them by admin, with a comment naming the Gate 3 approver.
- **Admin keeps `snc_basic_auth_api_access`** for seeding (item 13); the integration user does not need it.

## Known limitations

- **Detection weights and thresholds are design choices**, not ITIL standards (score 0.4 frequency +
  0.4 impact + 0.2 users; 5 incidents in 14 days; similarity 0.10). Urgency is not derived separately.
  Centroid clustering cannot split sub-clusters on one CI; agglomerative and DBSCAN are options but did
  worse on paraphrased descriptions (`--compare`).
- **The noise generator creates accidental patterns.** Each noise type is pinned to one CI with 2–3
  wordings, so some types cross the detection threshold. In the reference run this produced two real
  but unplanted problems (core-sw-01 Wi-Fi + DNS; crmdb connections) with no root cause to find.
- **LLM output can be speculative.** In the reference run the LLM invented a firmware cause for one
  unplanted problem; the human gate is what catches (or misses) this. The prompts restrict the LLM to the
  evidence, cited incidents are checked against the PDI, and the fallback says "not determined" instead.
- **Scoring against the answer key** uses fixed keyword groups per planted mechanism, set before the
  results; a correct cause worded differently could be scored a miss.
- **The verification window uses simulated dates after the change** (the change is implemented "now",
  so its window lies in the future). It is long enough to include each pattern's next expected occurrence.
- **Time in state** reflects the pace of building and running this project, not real operations.
- **Prevented incidents** is an estimate (rate before × window length − recurrences seen).
- **CAB approvals are recorded, not given by the CAB members** (see Admin exceptions).

## Lessons from the build

- Check `SN_INSTANCE` first when every login fails: an editor once saved a stale `.env` that pointed at
  an old PDI, and a lot of time went into permission changes that were not the problem.
- A plain Table API password write stores the password unhashed and basic auth then fails; set it with
  `sysparm_input_display_value=true` (item 13).
- When a REST write "succeeds" but a field does not change, look for read-only overrides, ACLs and
  business rules before assuming a permission problem.

## Files

`sn_client.py` (REST client: retries, rate limit, dry run, hibernation detection) · `config.py` (tag,
run context, thresholds, choice lookup) · `llm.py` (one wrapper, any provider) · `verify_instance.py` ·
`setup_integration_user.py` · `state_api.py` · the six agents · `orchestrator.py` · `reset.py` ·
`tests/` (offline unit tests: `python -m unittest discover tests`) · `reports/` (generated reports).
