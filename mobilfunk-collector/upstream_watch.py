#!/usr/bin/env python3
"""
Upstream-Watcher fuer den Mobilfunk-Collector.

Der Drillisch-Scraper ist HTML-Parsing gegen eine Seite, die uns nicht gehoert.
Wenn Drillisch umbaut, merkt das die Community meist frueher als wir. Dieses
Script prueft deshalb taeglich die einschlaegigen GitHub-Repos auf

  * neue Commits am Parser (dort steht der Fix, den wir uebernehmen koennen)
  * neu geoeffnete Issues       (dort steht die Fehlermeldung, meist vor dem Fix)

und meldet Treffer per Telegram. Ziel: nicht selbst debuggen, sondern zuerst
nachsehen, ob es die Loesung schon gibt.

Aufruf:
    python3 upstream_watch.py            # pruefen und ggf. melden
    python3 upstream_watch.py --status   # aktuellen Stand zeigen, nichts melden
    python3 upstream_watch.py --baseline # aktuellen Stand als "gesehen" merken
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("upstream-watch")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GITHUB_TOKEN     = os.getenv("GITHUB_TOKEN")  # optional, nur fuers Rate-Limit

STATE_FILE = Path(os.getenv("UPSTREAM_STATE_FILE",
                            str(Path.home() / ".mobilfunk_upstream.json")))

# repo -> Pfad, der ueberwacht wird ("" = ganzes Repo)
WATCHED = {
    "BergenSoft/scriptable_premiumsim": "src/PremiumSim.js",  # Referenz-Parser
    "skhg/pyPremiumSIM": "",                                  # Python-Variante
}

API = "https://api.github.com"
TIMEOUT = 20


def gh(path: str, params: dict | None = None) -> list | dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mobilfunk-collector-upstream-watch",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    r = requests.get(API + path, headers=headers, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def notify(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log.info("Telegram nicht konfiguriert - Meldung nur im Log:\n%s", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "disable_web_page_preview": True},
            timeout=10,
        ).raise_for_status()
    except requests.RequestException as e:
        log.error("Telegram-Benachrichtigung fehlgeschlagen: %s", e)


def latest_commit(repo: str, path: str) -> dict | None:
    params = {"per_page": 1}
    if path:
        params["path"] = path
    commits = gh(f"/repos/{repo}/commits", params)
    if not commits:
        return None
    c = commits[0]
    return {
        "sha":     c["sha"],
        "date":    c["commit"]["committer"]["date"],
        "message": c["commit"]["message"].splitlines()[0],
        "url":     c["html_url"],
    }


def new_issues(repo: str, since_iso: str | None) -> list[dict]:
    """Neu geoeffnete Issues (keine Pull Requests) seit dem letzten Lauf."""
    params = {"state": "open", "sort": "created", "direction": "desc", "per_page": 10}
    if since_iso:
        params["since"] = since_iso
    out = []
    for i in gh(f"/repos/{repo}/issues", params):
        if "pull_request" in i:
            continue
        if since_iso and i["created_at"] <= since_iso:
            continue
        out.append({"number": i["number"], "title": i["title"],
                    "url": i["html_url"], "created_at": i["created_at"]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="GitHub-Upstream-Watcher fuer den Drillisch-Scraper")
    ap.add_argument("--status", action="store_true",
                    help="Aktuellen Stand ausgeben, nichts melden und nichts speichern")
    ap.add_argument("--baseline", action="store_true",
                    help="Aktuellen Stand als gesehen markieren (keine Meldung)")
    args = ap.parse_args()

    state = load_state()
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    messages: list[str] = []
    errors = 0

    for repo, path in WATCHED.items():
        prev = state.get(repo, {})
        try:
            commit = latest_commit(repo, path)
            issues = new_issues(repo, prev.get("checked_at"))
        except requests.RequestException as e:
            errors += 1
            log.error("%s: GitHub-Abfrage fehlgeschlagen: %s", repo, e)
            continue

        if args.status:
            print(f"\n{repo}  (beobachtet: {path or 'gesamtes Repo'})")
            if commit:
                print(f"  letzter Commit : {commit['date']}  {commit['sha'][:8]}")
                print(f"                   {commit['message']}")
                print(f"                   {commit['url']}")
            print(f"  zuletzt gesehen: {prev.get('sha', '-')[:8]} "
                  f"(Lauf {prev.get('checked_at', 'nie')})")
            print(f"  neue Issues    : {len(issues)}")
            continue

        if commit and not args.baseline and prev.get("sha") and prev["sha"] != commit["sha"]:
            messages.append(
                f"NEUER COMMIT in {repo}\n"
                f"{commit['date']}  {commit['message']}\n"
                f"{commit['url']}\n"
                f"-> Pruefen ob HTML-Marker in mobilfunk_influx.py angepasst werden muessen."
            )

        if issues and not args.baseline:
            lines = "\n".join(f"  #{i['number']} {i['title']}\n  {i['url']}" for i in issues)
            messages.append(f"NEUE ISSUES in {repo}\n{lines}")

        state[repo] = {
            "sha":        commit["sha"] if commit else prev.get("sha"),
            "date":       commit["date"] if commit else prev.get("date"),
            "message":    commit["message"] if commit else prev.get("message"),
            "url":        commit["url"] if commit else prev.get("url"),
            "checked_at": now_iso,
        }

    if args.status:
        raise SystemExit(0)

    save_state(state)

    if messages:
        notify("Mobilfunk-Collector - Upstream-Aenderung\n\n" + "\n\n".join(messages))
        log.info("%d Meldung(en) verschickt.", len(messages))
    elif args.baseline:
        log.info("Baseline gesetzt - ab jetzt wird gemeldet.")
    else:
        log.info("Keine Aenderungen im Upstream.")

    raise SystemExit(1 if errors == len(WATCHED) else 0)


if __name__ == "__main__":
    main()
