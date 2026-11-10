# Demo script (10 minutes)

A walk-through for presenting to the class. It uses the finished reference run `20261029-192408`, so
nothing needs to be built live; every command below is read-only or a dry run, and it is safe to repeat.

**Before class (5 minutes):**
- Wake the PDI: open it in a browser and log in as admin.
- In Terminal: `cd ~/pm-agent` and `.venv/bin/python llm.py --test` (expect `RESULT: OK`).
- Open in browser tabs: the report on GitHub (`reports/20261029-192408.md`, the chart renders there),
  and in the PDI: PRB0040006, PRB0040010, KB0010003, CHG0030003.
- Make the terminal font large.

---

## 0:00 – 1:00 · The problem we are solving

> "Problem Management is about the incidents that keep coming back. ITIL 4 splits it into problem
> identification, problem control and error control. I built a set of AI agents that run all three on a
> real ServiceNow instance, with a human approving every decision that matters."

Show the README's table of steps, or run:
```
.venv/bin/python orchestrator.py --list
```
Point out the four gates and that each agent is one small Python module with its own success check.

## 1:00 – 2:30 · Synthetic data with hidden answers

> "To grade the agents I needed data where I know the truth. The Synthetic Data Agent seeds 200
> incidents over 60 days with four planted problems, a decoy and noise, and writes an answer key that
> the agents never read."

Name the four planted mechanisms: logs filling a disk on db-prod-01, a weekly certificate job that does
not restart the mail proxy, a firmware bug on one VPN gateway, a month-end batch locking the payroll
database. The decoy: seven unrelated SharePoint tickets in one week.

## 2:30 – 4:00 · Detection and Gate 1

```
.venv/bin/python detection_agent.py --check 20261029-192408
```
It rereads the PDI: the six problems, their linked incidents and the Gate 1 notes. Then show the
report's "Detected vs planted" table (recall and precision per pattern). Explain how detection works: it
groups incidents by CI and service, keeps groups with 5+ in 14 days, refines them by text similarity and
scores frequency, impact and users into a priority and a confidence.

(Do not rerun detection itself as the demo: the dataset now also holds the post-change window's
incidents, so a fresh run sees different data from the one Gate 1 decided on.)

> "All four planted problems were found. The decoy came out Low confidence and I rejected it at Gate 1.
> It also found two patterns I did not plant; more on those in a minute."

## 4:00 – 6:00 · Root cause analysis and why the human gate matters

In the PDI, open **PRB0040006** (email). Show *Cause notes* (the certificate job does not reload the
Mail Auth Proxy: the planted cause), *Workaround*, and the work note with the Kepner-Tregoe table, the
5 Whys and the incident numbers cited as evidence.

> "The RCA Agent reads the work notes, CI details and timing. 24 of 26 incidents were on a Monday, right
> after the Sunday certificate job. An LLM writes the analysis, and there is a rule-based fallback; both
> matched 4 of 4 planted causes."

Then the honest part. Open **PRB0040010** (core-sw-01):

> "This one was not planted. Its incidents are background noise: Wi-Fi resets and DNS cache flushes that
> happen to share a switch. The LLM confidently blamed a firmware defect the evidence does not support.
> That is exactly what Gate 2 is for. I accepted it, and the next phases show what happens to a fix
> built on a weak root cause."

Optional (1–2 minutes, a few cents of LLM): `.venv/bin/python rca_agent.py --run-id 20261029-192408 --compare`
ends with the LLM vs fallback scoreboard.

## 6:00 – 7:30 · Known error, change and approvals

Open **KB0010003**: a published Known Error article with description, cause and workaround, linked to the
problem. Then **CHG0030003**: a Normal change with implementation, backout and test plans, approved by the
assignment group and the CAB (the approval comments name Gate 3), implemented and closed Successful.

> "The change is drafted by the agent but only created after I approve it at Gate 3. ServiceNow's own
> approval flow still runs; the agent records my decision on it."

## 7:30 – 9:00 · Verification and Gate 4

> "A change being closed does not mean the problem is fixed. So the agents simulate the weeks after the
> change, about 25 days to include the next Mondays and the next month-end, and look for the pattern
> coming back."

```
.venv/bin/python change_agent.py --check 20261029-192408
```
Read out the closure lines: the four planted problems had a clean window and were closed with Fix
Applied. PRB0040010 had 6 similar incidents after its "fix" and PRB0040011 had 2, so they were kept open.

> "The verification caught the change that did not fix anything, because the root cause was never real."

## 9:00 – 10:00 · The report and wrap-up

Show the report on GitHub: summary, the before/after chart, 30.8 incidents estimated prevented. Then:
```
.venv/bin/python reporting_agent.py --check 20261029-192408
```
> "Every number in the report is recomputed from ServiceNow, not from the program's memory, and this
> check proves it."

Close with the lessons: the platform enforces real rules (state models, approvals, read-only fields)
that I had to work with rather than around; everything is tagged and can be reset; and the AI is useful
but needs the human gates.

---

**If something goes wrong:** a PDI that answers with an HTML page is hibernating, so wake it in the
browser and retry. `--dry-run` commands never change anything, so they are safe to rerun.
