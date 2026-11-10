"""One-time setup: create the web-service-only integration user the agents run as.

Runs as SN_ADMIN_USER. Sets the password with sysparm_input_display_value=true so the
platform hashes it (a plain Table API password write stores it unhashed and basic auth
then returns 401). Writes the new credentials to SN_USER/SN_PASSWORD in .env without
printing them. Safe to rerun: an existing user just gets a fresh password and any
missing roles.
"""
import os
import re
import secrets
import string
import sys
from pathlib import Path

from sn_client import SNClient, SNError

USER_NAME = "pm_agent_svc"
ROLES = ["itil", "itil_admin", "user_admin", "problem_coordinator"]  # problem_coordinator: problem state changes (phase 4)
ENV_PATH = Path(__file__).parent / ".env"
DISPLAY = {"sysparm_input_display_value": "true"}


def new_password():
    alphabet = string.ascii_letters + string.digits + "-_"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(24))
        if all(any(c in pw for c in s) for s in (string.ascii_lowercase, string.ascii_uppercase, string.digits, "-_")):
            return pw


def save_env(user, password):
    text = ENV_PATH.read_text()
    for key, value in (("SN_USER", user), ("SN_PASSWORD", f'"{password}"')):
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    ENV_PATH.write_text(text)
    os.environ["SN_USER"], os.environ["SN_PASSWORD"] = user, password  # load_dotenv won't override


def main():
    try:
        admin = SNClient(user_var="SN_ADMIN_USER", password_var="SN_ADMIN_PASSWORD")
        admin.get("sys_user", fields=["sys_id"], limit=1)
        print("1. admin can call the Table API")

        pw = new_password()
        fields = {"user_name": USER_NAME, "first_name": "PM Agent", "last_name": "Service Account",
                  "web_service_access_only": "true", "active": "true", "locked_out": "false",
                  "password_needs_reset": "false", "user_password": pw}
        existing = admin.get("sys_user", f"user_name={USER_NAME}", fields=["sys_id"], limit=1)
        if existing:
            user_id = existing[0]["sys_id"]
            admin.update("sys_user", user_id, fields, params=DISPLAY)
            print(f"2. {USER_NAME} existed: password reset, flags reapplied")
        else:
            user_id = admin.create("sys_user", fields, params=DISPLAY)["sys_id"]
            print(f"2. created {USER_NAME}")

        stored = admin.get("sys_user", f"sys_id={user_id}", fields=["user_password"], limit=1)[0].get("user_password", "")
        if not stored:
            print("3. stored password not readable via API; the login test in step 6 decides")
        elif stored == pw:
            sys.exit("3. Password was stored unhashed; basic auth would fail. Stopping.")
        else:
            print(f"3. stored password hashed by platform (stored length {len(stored)})")

        have = {r["role"] for r in admin.get_all("sys_user_has_role", f"user={user_id}", fields=["role"])}
        for name in ROLES:
            role = admin.get("sys_user_role", f"name={name}", fields=["sys_id"], limit=1)
            if not role:
                print(f"   WARN: role {name} not found")
            elif role[0]["sys_id"] not in have:
                admin.create("sys_user_has_role", {"user": user_id, "role": role[0]["sys_id"]})
        print(f"4. roles ensured: {', '.join(ROLES)}")

        save_env(USER_NAME, pw)
        print(f"5. .env updated: SN_USER={USER_NAME}, SN_PASSWORD=<new, not shown>")
    except SNError as e:
        sys.exit(f"ERROR: {e}")

    try:
        SNClient().get("sys_user", fields=["sys_id"], limit=1)
        print(f"6. {USER_NAME} can call the Table API with basic auth: PASS")
    except SNError as e:
        sys.exit(f"6. {USER_NAME} login test FAILED: {e}")


if __name__ == "__main__":
    import argparse
    argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog="No options: running it creates or refreshes pm_agent_svc and gives it a NEW password in .env.").parse_args()
    main()
