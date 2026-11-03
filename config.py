"""Project configuration. Instance-specific values come from config/instance.json,
written by verify_instance.py; nothing instance-specific is hardcoded here."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
INSTANCE_FILE = ROOT / "config" / "instance.json"

TAG_PREFIX = "PMSYNTH"

# This PDI's incident table has no problem_id field (verified in phase 1), so incidents are
# linked to their problem through the task.parent reference instead.
INCIDENT_PROBLEM_LINK = "parent"


def load_instance():
    if not INSTANCE_FILE.exists():
        sys.exit("config/instance.json missing: run `python verify_instance.py` first.")
    return json.loads(INSTANCE_FILE.read_text())


def choice_value(field, label):
    """Value for a choice label, e.g. choice_value('problem.state', 'Root Cause Analysis')."""
    for c in load_instance()["choices"][field]:
        if c["label"].lower() == label.lower():
            return c["value"]
    raise KeyError(f"No choice labelled {label!r} for {field}; see config/instance.json")


# --- run context (runs/<run_id>.json), shared by all agents --------------------
RUNS_DIR = ROOT / "runs"


def tag(run_id):
    return f"{TAG_PREFIX}-{run_id}"


def new_context(seed):
    from datetime import datetime, timezone
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return {"run_id": run_id, "tag": tag(run_id), "seed": seed, "log": []}


def context_path(run_id):
    return RUNS_DIR / f"{run_id}.json"


def load_context(run_id):
    return json.loads(context_path(run_id).read_text())


def save_context(ctx):
    RUNS_DIR.mkdir(exist_ok=True)
    context_path(ctx["run_id"]).write_text(json.dumps(ctx, indent=2))


# --- Problem Detection Agent (phase 3); set before seeing results, not tuned to the answer key ---
DETECT_MIN_INCIDENTS = 5     # candidate when >= this many incidents ...
DETECT_WINDOW_DAYS = 14      # ... fall within this many days on the same CI or service
DETECT_MIN_SIMILARITY = 0.10 # members below this TF-IDF cosine to the cluster centroid are dropped
CONF_HIGH_COHESION = 0.30    # mean similarity to centroid for High confidence
CONF_MEDIUM_COHESION = 0.20  # ... for Medium
CONF_HIGH_PURITY = 0.60      # share of the CI/service group that stays in the cluster, for High
DETECT_METHOD = "centroid"   # clustering inside each CI/service group: centroid | agglomerative | dbscan
AGG_MIN_SIMILARITY = 0.20    # agglomerative: keep merging while average-linkage cosine >= this
DBSCAN_MIN_SIMILARITY = 0.30 # dbscan: two incidents are neighbours when cosine >= this ...
DBSCAN_MIN_POINTS = 3        # ... and a core incident needs at least this many neighbours (itself included)
