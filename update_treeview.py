"""
EOSPAY Jira Treeview Updater – GitHub Actions Version
======================================================
API: Jira REST API v3 → /rest/api/3/search/jql
Paginierung: nextPageToken
Typen: Epic, Story, Sub-Task, Dev-Story
Hierarchie: Epic → Story → Sub-Task → Dev-Story

NEU: generiert zusätzlich docs/report.html (Reporting-Tab)
     - Burn-up getrennt nach Doku-Story / Dev-Story
     - Dev-Story-Status je Sprint-Zuordnung (Snapshot) + Throughput
     - Epic-Tabelle mit Doku/Dev-Split, inkl. Epic-Vererbung für
       Dev-Stories ohne eigene Epic-Zuordnung über ihre Verlinkung
       zur zugehörigen Doku-Story (beliebige Link-Richtung)
     - Budget optional aus docs/report_budget.json (siehe unten)
"""

import json
import os
import re
import sys
import requests
from datetime import datetime
from pathlib import Path

# ============================================================
# CONFIG – aus GitHub Secrets / Umgebungsvariablen
# ============================================================
JIRA_BASE_URL = "https://apk.atlassian.net"
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_PROJECT = "EOSPAY"
PM_TEAM_ID = "586f2bf4-7e68-4e18-a519-989b592bdf4d"
PM_EPIC_PREFIX = "[PM]"
TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL", "")
GITHUB_PAGES_URL = os.environ.get("GITHUB_PAGES_URL", "")

OUTPUT_FILE = Path("docs/index.html")
REPORT_OUTPUT_FILE = Path("docs/report.html")
BUDGET_CONFIG_FILE = Path("docs/report_budget.json")

FIELDS = [
    "summary", "status", "issuetype", "parent", "subtasks",
    "issuelinks", "labels", "customfield_10001", "customfield_10020"
]
TARGET_TYPES = {"Epic", "Story", "Sub-Task", "Dev-Story"}

# Status-Namen mit statusCategory "done" (Fertig UND Abgebrochen zählen
# technisch als "done" in Jira - Abgebrochen wird in der Reporting-Tabelle
# separat ausgewiesen, siehe compute_report_data)
DONE_STATUS_NAMES = {"Fertig", "Abgebrochen"}
CANCELLED_STATUS_NAMES = {"Abgebrochen"}

# ============================================================
# JIRA API v3 – /rest/api/3/search/jql mit nextPageToken
# ============================================================
def jira_search(jql: str, fields: list, max_results: int = 50) -> list:
    """
    Paginierter Jira v3 Query.
    Endpoint: GET /rest/api/3/search/jql
    Paginierung: nextPageToken aus Response-Header 'Link' oder Response-Body.
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {"Accept": "application/json"}
    all_issues = []
    next_page_token = None

    while True:
        params = {
            "jql": jql,
            "fields": ",".join(fields),
            "maxResults": max_results,
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token

        resp = requests.get(url, auth=auth, headers=headers, params=params)
        if resp.status_code == 400:
            print(f"  JQL-Fehler: {resp.text[:300]}")
            return []
        resp.raise_for_status()
        data = resp.json()
        issues = data.get("issues", [])
        all_issues.extend(issues)
        total = data.get("total", len(all_issues))
        print(f"  Geladen: {len(all_issues)} / {total}")

        next_page_token = data.get("nextPageToken")
        if not next_page_token or not issues or len(issues) < max_results:
            break

    return all_issues


# ============================================================
# PARSING
# ============================================================
def parse_issue(raw: dict) -> dict:
    f = raw["fields"]
    key = raw["key"]
    itype = f["issuetype"]["name"]
    status = f["status"]["name"]
    summary = f["summary"]
    labels = f.get("labels", [])
    parent = f.get("parent") or {}
    parent_key = parent.get("key", "")
    team = f.get("customfield_10001") or {}
    team_id = team.get("id", "")

    # Sub-tasks listed under parent (from subtasks field)
    subtask_keys = [st["key"] for st in f.get("subtasks", [])]

    # Sprint (customfield_10020 = array of sprint objects)
    sprint_info = None
    for sp in reversed(f.get("customfield_10020") or []):
        name = sp.get("name", "")
        state = sp.get("state", "")
        m = re.search(r"Sprint\s+(\d+)", name)
        if m:
            sprint_info = {"num": m.group(1), "state": state}
            break

    # Issue links – store direction AND link-type name
    links = []
    for lk in f.get("issuelinks", []):
        ltype_name = lk.get("type", {}).get("name", "")
        if "inwardIssue" in lk:
            links.append({
                "type": "blocked-by",
                "link_type": ltype_name,
                "key": lk["inwardIssue"]["key"],
            })
        if "outwardIssue" in lk:
            links.append({
                "type": "blocks",
                "link_type": ltype_name,
                "key": lk["outwardIssue"]["key"],
            })

    return {
        "key": key, "type": itype, "status": status, "summary": summary,
        "labels": labels, "parent": parent_key, "team_id": team_id,
        "subtask_keys": subtask_keys,
        "sprint": sprint_info, "links": links,
    }


def fetch_all_issues() -> list:
    print("Lade alle EOSPAY Issues (API v3)...")
    raw = jira_search(
        f"project = {JIRA_PROJECT} ORDER BY created ASC",
        FIELDS,
        max_results=50,
    )
    print(f"  → {len(raw)} Issues gesamt")
    found_types = set(r["fields"]["issuetype"]["name"] for r in raw)
    print(f"  Gefundene Typen: {found_types}")
    filtered = [r for r in raw if r["fields"]["issuetype"]["name"] in TARGET_TYPES]
    print(f"  → {len(filtered)} relevante Issues (Epic/Story/Sub-Task/Dev-Story)")
    return [parse_issue(r) for r in filtered]


# ============================================================
# DATEN AUFBEREITEN (Treeview)
# ============================================================
def prepare_data(all_issues: list) -> dict:
    imap = {i["key"]: i for i in all_issues}

    epics = [
        {"key": i["key"], "status": i["status"], "summary": i["summary"]}
        for i in all_issues
        if i["type"] == "Epic" and not i["summary"].startswith(PM_EPIC_PREFIX)
    ]
    epic_keys = set(e["key"] for e in epics)

    # PM-Team filter: only exclude stories that have NO valid epic parent.
    # Stories assigned to PM Team but under a non-PM epic are always shown.
    pm_story_keys = {
        i["key"] for i in all_issues
        if i["type"] == "Story"
        and i["team_id"] == PM_TEAM_ID
        and i["parent"] not in epic_keys  # has no valid (non-PM) epic parent
    }

    stories = [
        {k: v for k, v in i.items() if k not in ("team_id", "sprint", "subtask_keys")}
        for i in all_issues
        if i["type"] == "Story" and i["key"] not in pm_story_keys
    ]

    subtasks = [
        {k: v for k, v in i.items() if k not in ("team_id", "subtask_keys")}
        for i in all_issues
        if i["type"] == "Sub-Task"
    ]

    devs = [
        {k: v for k, v in i.items() if k not in ("team_id", "subtask_keys")}
        for i in all_issues
        if i["type"] == "Dev-Story"
    ]

    sprints = {
        i["key"]: i["sprint"]
        for i in all_issues
        if i["type"] == "Dev-Story" and i["sprint"]
    }

    # ── Relationship maps ──────────────────────────────────
    story_subtask_map = {}
    for i in all_issues:
        if i["type"] == "Story" and i["key"] not in pm_story_keys:
            if i["subtask_keys"]:
                story_subtask_map[i["key"]] = i["subtask_keys"]

    # Dev-Story resolution: accept ANY link direction to a Story or Sub-Task
    story_dev_map = {}     # story_key → [dev_key]
    subtask_dev_map = {}   # subtask_key → [dev_key]

    for d in devs:
        for lk in d["links"]:
            target = imap.get(lk["key"])
            if not target:
                continue
            if target["type"] == "Sub-Task":
                subtask_dev_map.setdefault(lk["key"], []).append(d["key"])
                break
            if target["type"] == "Story" and lk["key"] not in pm_story_keys:
                story_dev_map.setdefault(lk["key"], []).append(d["key"])
                break

    print(f"\n  Epics (ohne [PM]): {len(epics)}")
    print(f"  Stories (ohne PM-Team): {len(stories)}")
    print(f"  Sub-Tasks: {len(subtasks)}")
    print(f"  Dev-Stories: {len(devs)}")
    print(f"  PM-Stories ausgeblendet: {len(pm_story_keys)}")

    return {
        "epics": epics,
        "stories": stories,
        "subtasks": subtasks,
        "devs": devs,
        "sprints": sprints,
        "pm_exclude": list(pm_story_keys),
        "story_subtask_map": story_subtask_map,
        "story_dev_map": story_dev_map,
        "subtask_dev_map": subtask_dev_map,
    }


# ============================================================
# DATEN AUFBEREITEN (Reporting)
# ============================================================
def compute_report_data(all_issues: list) -> dict:
    """
    Baut die Reporting-Kennzahlen: Doku-/Dev-Story getrennt gezählt,
    Epic-Zuordnung inkl. Vererbung für Dev-Stories ohne eigenes parent,
    Sprint-Aggregation (nur Dev-Story, da Doku-Stories nicht sprintgebunden sind).
    """
    imap = {i["key"]: i for i in all_issues}

    epics = [i for i in all_issues if i["type"] == "Epic" and not i["summary"].startswith(PM_EPIC_PREFIX)]
    epic_keys = {e["key"] for e in epics}
    epic_name = {e["key"]: e["summary"] for e in epics}

    pm_story_keys = {
        i["key"] for i in all_issues
        if i["type"] == "Story" and i["team_id"] == PM_TEAM_ID and i["parent"] not in epic_keys
    }

    dokus = [i for i in all_issues if i["type"] == "Story" and i["key"] not in pm_story_keys]
    devs = [i for i in all_issues if i["type"] == "Dev-Story"]

    def own_epic(item):
        return item["parent"] if item["parent"] in epic_keys else None

    # Dev-Story: eigenes Epic, sonst Vererbung über Verlinkung zur Doku-Story
    # (beliebige Link-Richtung, wie im Treeview bereits gelöst)
    dev_epic = {}
    dev_epic_inherited = {}
    for d in devs:
        e = own_epic(d)
        inherited = False
        if not e:
            for lk in d["links"]:
                target = imap.get(lk["key"])
                if target and target["type"] == "Story" and target["key"] not in pm_story_keys:
                    e2 = own_epic(target)
                    if e2:
                        e = e2
                        inherited = True
                        break
        dev_epic[d["key"]] = e
        dev_epic_inherited[d["key"]] = inherited

    doku_epic = {s["key"]: own_epic(s) for s in dokus}

    # ── Epic-Rollup ─────────────────────────────────────────
    rollup = {}

    def bump(epic_key, kind, status):
        name = epic_name.get(epic_key) if epic_key else None
        name = name or "(kein Epic zugeordnet)"
        r = rollup.setdefault(name, {"doku_total": 0, "doku_done": 0, "dev_total": 0, "dev_done": 0})
        r[f"{kind}_total"] += 1
        if status in DONE_STATUS_NAMES:
            r[f"{kind}_done"] += 1

    for s in dokus:
        bump(doku_epic[s["key"]], "doku", s["status"])
    for d in devs:
        bump(dev_epic[d["key"]], "dev", d["status"])

    inherited_count = sum(1 for v in dev_epic_inherited.values() if v)

    # ── Sprint-Aggregation (nur Dev-Story) ──────────────────
    sprint_stats = {}  # sprint_num(str) -> {"total":n, "done":n, "state":...}
    for d in devs:
        sp = d.get("sprint")
        if not sp or not sp.get("num"):
            continue
        n = sp["num"]
        sprint_stats.setdefault(n, {"total": 0, "done": 0, "state": sp.get("state")})
        sprint_stats[n]["total"] += 1
        if d["status"] in DONE_STATUS_NAMES:
            sprint_stats[n]["done"] += 1

    doku_total = len(dokus)
    doku_done = sum(1 for s in dokus if s["status"] in DONE_STATUS_NAMES)
    dev_total = len(devs)
    dev_done = sum(1 for d in devs if d["status"] in DONE_STATUS_NAMES)
    dev_cancelled = sum(1 for d in devs if d["status"] in CANCELLED_STATUS_NAMES)
    doku_cancelled = sum(1 for s in dokus if s["status"] in CANCELLED_STATUS_NAMES)

    no_epic_dev = sum(1 for d in devs if not dev_epic[d["key"]])
    no_epic_doku = sum(1 for s in dokus if not doku_epic[s["key"]])

    return {
        "rollup": rollup,
        "sprint_stats": sprint_stats,
        "doku_total": doku_total, "doku_done": doku_done, "doku_cancelled": doku_cancelled,
        "dev_total": dev_total, "dev_done": dev_done, "dev_cancelled": dev_cancelled,
        "inherited_count": inherited_count,
        "no_epic_dev": no_epic_dev, "no_epic_doku": no_epic_doku,
    }


def load_budget_config() -> dict:
    """
    Optional: docs/report_budget.json im Repo pflegen, Format z.B.:
    {
      "currency": "EUR",
      "sprints": ["S1","S2","S3","S4","S5","S6","S7","S8"],
      "plan_cumulative":   [24000, 55000, 90000, 130000, 175000, 220000, 265000, 310000],
      "actual_cumulative": [22000, 50000, 80000, 105000, 150000, 187000, null, null]
    }
    Wird die Datei manuell im Repo gepflegt (z.B. einmal pro Sprint aktualisiert),
    erscheint der echte Verlauf automatisch im Reporting-Tab.
    """
    if BUDGET_CONFIG_FILE.exists():
        try:
            return json.loads(BUDGET_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  Warnung: report_budget.json konnte nicht gelesen werden: {e}")
    return {}


CHARTJS_VENDOR_PATH = Path("docs/vendor/chart.umd.min.js")
CHARTJS_CDN_URLS = [
    "https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.4/chart.umd.min.js",
    "https://unpkg.com/chart.js@4.4.4/dist/chart.umd.min.js",
]


def ensure_chartjs_vendored():
    """
    Lädt Chart.js einmalig auf dem GitHub-Actions-Runner herunter und legt es
    als eigene Datei im Repo ab (docs/vendor/chart.umd.min.js). So muss der
    Browser der Person, die den Report ansieht, NIE einen externen CDN-Request
    machen - falls z.B. das Firmennetzwerk cdnjs.cloudflare.com/jsdelivr.net
    blockiert, funktioniert der Report trotzdem, weil die Datei von derselben
    GitHub-Pages-Domain wie report.html selbst kommt.
    Wird nur heruntergeladen, wenn die Datei noch nicht existiert - danach ist
    sie ein normaler versionierter Teil des Repos.
    Versucht mehrere CDN-Quellen nacheinander, falls eine blockiert/down ist.
    """
    if CHARTJS_VENDOR_PATH.exists():
        print(f"  Chart.js bereits vendored: {CHARTJS_VENDOR_PATH}")
        return
    CHARTJS_VENDOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    for url in CHARTJS_CDN_URLS:
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            CHARTJS_VENDOR_PATH.write_bytes(resp.content)
            print(f"  Chart.js heruntergeladen von {url} und vendored: "
                  f"{CHARTJS_VENDOR_PATH} ({len(resp.content):,} bytes)")
            return
        except Exception as e:
            print(f"  {url} fehlgeschlagen ({e}), versuche nächste Quelle...")
    print(f"  WARNUNG: Chart.js konnte von keiner Quelle vendored werden. "
          f"report.html fällt auf CDN-Link zurück - falls das Firmennetzwerk "
          f"CDNs blockiert, bleiben die Charts dann leer.")


# ============================================================
# NAV (gemeinsam für beide Seiten)
# ============================================================
def nav_html(active: str) -> str:
    tv = "active" if active == "treeview" else ""
    rp = "active" if active == "report" else ""
    return f"""
<div class="tabnav">
  <a class="tab {tv}" href="index.html">🌳 Treeview</a>
  <a class="tab {rp}" href="report.html">📊 Reporting</a>
</div>
<style>
.tabnav{{display:flex;gap:4px;margin-bottom:12px}}
.tabnav .tab{{font-size:12px;font-weight:600;padding:6px 14px;border-radius:8px 8px 0 0;
  text-decoration:none;color:#777;background:#ececea;border:1px solid #e0dfd8;border-bottom:none}}
.tabnav .tab.active{{color:#111;background:#fff}}
.tabnav .tab:hover{{color:#111}}
</style>
"""


# ============================================================
# HTML GENERIEREN (Treeview)
# ============================================================
def generate_html(data: dict, timestamp: str) -> str:
    sprint_nums = sorted(
        set(v["num"] for v in data["sprints"].values() if v["num"]),
        key=lambda x: int(x)
    )
    sprint_options = "\n            ".join(
        f'<option value="{n}">Sprint {n}</option>' for n in sprint_nums
    )

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EOSPAY Jira Treeview – {timestamp}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;color:#1a1a1a;background:#f5f5f3;padding:16px}}
h1{{font-size:15px;font-weight:600;margin-bottom:4px;color:#111}}
.updated{{font-size:11px;color:#aaa;margin-bottom:12px}}
.toolbar{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:10px 0;border-bottom:1px solid #e0dfd8;margin-bottom:10px}}
.toolbar input{{flex:1;min-width:120px;font-size:13px;padding:6px 10px;border:1px solid #ccc;border-radius:6px;background:#fff}}
.toolbar input::placeholder{{color:#aaa}}
.toolbar select{{font-size:13px;padding:5px 8px;border:1px solid #ccc;border-radius:6px;background:#fff}}
.filter-btn{{font-size:12px;padding:4px 10px;border:1px solid #ccc;border-radius:6px;background:#f0f0ee;color:#555;cursor:pointer}}
.filter-btn.active{{background:#dbeafe;color:#1d4ed8;border-color:#93c5fd}}
.stats{{display:flex;gap:16px;padding:0 0 10px;flex-wrap:wrap;font-size:12px;color:#666;border-bottom:1px solid #e0dfd8;margin-bottom:10px}}
.stats strong{{color:#111}}
.hint{{margin-left:auto;font-size:11px;color:#999}}
.legend{{display:flex;gap:10px;flex-wrap:wrap;padding:4px 0 8px;font-size:11px;color:#888;align-items:center}}
.sb{{font-size:10px;font-weight:500;padding:2px 6px;border-radius:10px}}
.sc{{background:#d4d2c9;color:#444}}.sa{{background:#86efac;color:#14532d}}.sf{{background:#fcd34d;color:#78350f}}
.be{{font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px;background:#ede9fe;color:#5b21b6;flex-shrink:0}}
.bs{{font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px;background:#dbeafe;color:#1e40af;flex-shrink:0}}
.bst{{font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px;background:#fef9c3;color:#854d0e;flex-shrink:0}}
.bd{{font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px;background:#dcfce7;color:#166534;flex-shrink:0}}
.pill{{font-size:10px;padding:2px 7px;border-radius:10px;flex-shrink:0;font-weight:500;white-space:nowrap}}
.p-todo{{background:#f1f0ea;color:#555}}.p-ip{{background:#dbeafe;color:#1e40af}}
.p-done{{background:#dcfce7;color:#166534}}.p-test{{background:#fef3c7;color:#92400e}}
.p-cancel{{background:#fee2e2;color:#991b1b}}.p-review{{background:#fce7f3;color:#9d174d}}
.ikey{{font-size:11px;color:#999;font-family:'SF Mono',monospace;flex-shrink:0}}
.ikey a{{color:inherit;text-decoration:none}}.ikey a:hover{{text-decoration:underline;color:#1d4ed8}}
.in{{flex:1;min-width:0;font-size:13px;line-height:1.4}}
.en{{font-size:13px;font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.chev{{font-size:13px;color:#999;width:14px;flex-shrink:0;transition:transform .15s;display:inline-block}}
.chev.open{{transform:rotate(90deg)}}
.epic-block{{margin-bottom:4px;border:1px solid #e0dfd8;border-radius:8px;overflow:hidden;background:#fff}}
.epic-row{{display:flex;align-items:center;gap:8px;padding:8px 12px;background:#f5f5f3;cursor:pointer;user-select:none}}
.epic-row:hover{{background:#ededeb}}
.epic-empty{{opacity:0.55}}
.sprint-match{{border-color:#fcd34d}}
.sprint-match .epic-row{{background:#fffbeb}}
.story-row{{display:flex;align-items:flex-start;gap:8px;padding:6px 12px 6px 28px;border-top:1px solid #f0efe9;cursor:pointer;user-select:none}}
.story-row:hover{{background:#fafaf8}}
.subtask-row{{display:flex;align-items:flex-start;gap:8px;padding:5px 12px 5px 44px;border-top:1px solid #f0efe9;background:#fafaf8;cursor:pointer;user-select:none}}
.subtask-row:hover{{background:#f5f5f3}}
.dev-row{{display:flex;align-items:flex-start;gap:8px;padding:5px 12px 5px 60px;border-top:1px solid #f0efe9;background:#f5f5f3}}
.dev-row-direct{{padding-left:44px}}
.lbs{{display:flex;gap:4px;flex-wrap:wrap;margin-top:2px}}
.lb{{font-size:9px;padding:1px 5px;border-radius:5px;background:#f0f0ee;color:#888;border:1px solid #e0dfd8}}
.dep{{display:flex;align-items:center;gap:6px;padding:3px 12px 3px 76px;font-size:11px;background:#eff6ff;border-top:1px solid #f0efe9}}
.dep-direct{{padding-left:60px}}
.db{{font-size:10px;padding:1px 6px;border-radius:8px}}
.db-bl{{background:#fed7aa;color:#9a3412}}.db-bby{{background:#fef3c7;color:#92400e}}
.dep-k a{{font-size:11px;font-family:monospace;color:#555;text-decoration:none}}
.dep-n{{font-size:11px;color:#777}}
.orphan{{margin-bottom:4px;border:1px solid #e0dfd8;border-radius:8px;overflow:hidden;background:#fff}}
.orphan-h{{padding:8px 12px;background:#f5f5f3;font-size:12px;font-weight:600;color:#666;cursor:pointer;display:flex;align-items:center;gap:8px}}
.no-results{{padding:32px;text-align:center;color:#aaa;font-size:14px}}
</style>
</head>
<body>
{nav_html('treeview')}
<h1>EOSPAY Jira Treeview</h1>
<div class="updated">Stand: {timestamp} &middot; automatisch generiert via GitHub Actions</div>
<div class="toolbar">
  <input type="text" id="search" placeholder="ID suchen (z.B. 96, 418)..." oninput="render()" inputmode="numeric">
  <select id="sf" onchange="render()">
    <option value="">Alle Status</option>
    <option value="TO DO">To Do</option>
    <option value="In Arbeit">In Arbeit</option>
    <option value="Test">Test</option>
    <option value="Wird \xfcberpr\xfcft">Wird \xfcberpr\xfcft</option>
    <option value="Fertig">Fertig</option>
    <option value="Abgebrochen">Abgebrochen</option>
  </select>
  <select id="spf" onchange="render()">
    <option value="">Alle Sprints</option>
    {sprint_options}
  </select>
  <button class="filter-btn active" id="btn-Story" onclick="toggleType('Story')">Stories</button>
  <button class="filter-btn active" id="btn-Sub-Task" onclick="toggleType('Sub-Task')">Sub-Tasks</button>
  <button class="filter-btn active" id="btn-Dev-Story" onclick="toggleType('Dev-Story')">Dev-Stories</button>
  <button class="filter-btn" id="btn-deps" onclick="toggleDeps()">&#128279; Deps</button>
  <button class="filter-btn" onclick="collapseAll()">&#8651; Einklappen</button>
</div>
<div class="legend">
  <span class="be">Epic</span> &rsaquo; <span class="bs">Story</span> &rsaquo; <span class="bst">Sub-Task</span> &rsaquo; <span class="bd">Dev</span>
  &nbsp;|&nbsp; Sprint: <span class="sb sc">Sx</span> abgeschlossen &nbsp;<span class="sb sf">Sx</span> geplant
</div>
<div class="stats" id="stats" style="margin-top:8px"></div>
<div id="tree"></div>
<script>
const SPRINTS={json.dumps(data["sprints"],ensure_ascii=False)};
const EPICS={json.dumps(data["epics"],ensure_ascii=False)};
const STORIES={json.dumps(data["stories"],ensure_ascii=False)};
const SUBTASKS={json.dumps(data["subtasks"],ensure_ascii=False)};
const DEVS={json.dumps(data["devs"],ensure_ascii=False)};
const STORY_SUBTASK_MAP={json.dumps(data["story_subtask_map"],ensure_ascii=False)};
const STORY_DEV_MAP={json.dumps(data["story_dev_map"],ensure_ascii=False)};
const SUBTASK_DEV_MAP={json.dumps(data["subtask_dev_map"],ensure_ascii=False)};
const TIMESTAMP="{timestamp}";
const epicKeys=new Set(EPICS.map(e=>e.key));
const imap={{}};
EPICS.forEach(e=>imap[e.key]={{...e,type:'Epic'}});
STORIES.forEach(s=>imap[s.key]=s);
SUBTASKS.forEach(s=>imap[s.key]=s);
DEVS.forEach(d=>imap[d.key]=d);
let showTypes={{'Story':true,'Sub-Task':true,'Dev-Story':true}};
let showDeps=false;
let collapsed={{}};
function sc(s){{if(s==='Fertig')return 'p-done';if(s==='In Arbeit')return 'p-ip';if(s==='Abgebrochen')return 'p-cancel';if(s==='Test')return 'p-test';if(s==='\xdcberpr\xfcft'||s.includes('\xfcberpr'))return 'p-review';return 'p-todo';}}
function sl(s){{return s==='TO DO'?'To Do':s;}}
function spBadge(key){{const sp=SPRINTS[key];if(!sp||!sp.num)return '';const c=sp.state==='active'?'sa':sp.state==='future'?'sf':'sc';return `<span class="sb ${{c}}">S${{sp.num}}</span>`;}}
function jiraLink(key){{return `https://apk.atlassian.net/browse/${{key}}`;}}
function toggleType(t){{showTypes[t]=!showTypes[t];document.getElementById('btn-'+t).classList.toggle('active',showTypes[t]);render();}}
function toggleDeps(){{showDeps=!showDeps;document.getElementById('btn-deps').classList.toggle('active',showDeps);render();}}
function toggleC(k){{collapsed[k]=!collapsed[k];render();}}
function matchesId(i,q){{
  if(!q)return true;
  const num=q.replace(/[^0-9]/g,'');
  return num!==''&&i.key==='EOSPAY-'+num;
}}
function matchesStatus(i,sf){{return !sf||i.status===sf;}}
function matchesSprint(i,spf){{
  if(!spf)return true;
  if(i.type!=='Dev-Story')return false;
  const sp=SPRINTS[i.key];
  return sp&&sp.num===spf;
}}
function depRow(lk,direct){{
  const t=imap[lk.key];
  const cls=lk.type==='blocks'?'db-bl':'db-bby';
  const lbl=lk.type==='blocks'?'blockiert':'blockiert durch';
  const dcls=direct?'dep dep-direct':'dep';
  return `<div class="${{dcls}}"><span>&#8594;</span><span class="db ${{cls}}">${{lbl}}</span><span class="dep-k"><a href="${{jiraLink(lk.key)}}" target="_blank">${{lk.key}}</a></span>${{t?`<span class="dep-n">\u2013 ${{t.summary}}</span>`:'<span class="dep-n"><i>(au\xdferhalb View)</i></span>'}}</div>`;
}}
function devHtml(d,cnt,direct){{
  if(!matchesStatus(d,document.getElementById('sf').value))return '';
  cnt.d++;
  const hasDeps=d.links&&d.links.length>0;
  const xl=(d.labels||[]).filter(l=>l!=='Dev');
  const cls=direct?'dev-row dev-row-direct':'dev-row';
  let h=`<div class="${{cls}}"><span class="bd">Dev</span>${{spBadge(d.key)}}<span class="ikey"><a href="${{jiraLink(d.key)}}" target="_blank">${{d.key}}</a></span><div class="in"><div>${{d.summary}}</div>${{xl.length?`<div class="lbs">${{xl.map(l=>`<span class="lb">${{l}}</span>`).join('')}}</div>`:''}}</div><span class="pill ${{sc(d.status)}}">${{sl(d.status)}}</span>${{hasDeps?'<span style="color:#aaa;font-size:12px">&#128279;</span>':''}}</div>`;
  if(showDeps&&hasDeps)d.links.forEach(lk=>{{h+=depRow(lk,direct);}});
  return h;
}}
function subtaskHtml(st,q,sf,spf,cnt){{
  const devKeys=(SUBTASK_DEV_MAP[st.key]||[]);
  const devChildren=showTypes['Dev-Story']?devKeys.map(k=>imap[k]).filter(d=>{{
    if(!d||!matchesStatus(d,sf))return false;
    if(spf)return matchesSprint(d,spf);
    if(q)return matchesId(d,q);
    return true;
  }}):[];
  const stVis=showTypes['Sub-Task']&&!spf&&matchesId(st,q)&&matchesStatus(st,sf);
  if(!stVis&&devChildren.length===0)return '';
  cnt.st++;
  const hasChildren=devChildren.length>0;
  const open=spf?true:!collapsed[st.key];
  const hasDeps=st.links&&st.links.length>0;
  let h=`<div class="subtask-row" onclick="${{spf?'':`toggleC('${{st.key}}')`}}">`;
  h+=hasChildren?`<span class="chev${{open?' open':''}}">&rsaquo;</span>`:`<span style="width:14px;flex-shrink:0"></span>`;
  h+=`<span class="bst">Sub-Task</span><span class="ikey"><a href="${{jiraLink(st.key)}}" target="_blank" onclick="event.stopPropagation()">${{st.key}}</a></span><div class="in"><div>${{st.summary}}</div></div><span class="pill ${{sc(st.status)}}">${{sl(st.status)}}</span>${{hasDeps?'<span style="color:#aaa;font-size:12px">&#128279;</span>':''}}</div>`;
  if(showDeps&&hasDeps&&!spf)st.links.forEach(lk=>{{h+=depRow(lk,false);}});
  if(open&&devChildren.length>0)devChildren.forEach(d=>{{h+=devHtml(d,cnt,false);}});
  return h;
}}
function storyHtml(s,q,sf,spf,cnt){{
  const stKeys=(STORY_SUBTASK_MAP[s.key]||[]);
  const devDirectKeys=(STORY_DEV_MAP[s.key]||[]);
  const sprintSubtasks=spf&&showTypes['Sub-Task']?stKeys.map(k=>imap[k]).filter(st=>{{
    if(!st)return false;
    return (SUBTASK_DEV_MAP[k]||[]).some(dk=>{{const d=imap[dk];return d&&matchesSprint(d,spf)&&matchesStatus(d,sf);}});
  }}):[];
  const sprintDirectDevs=spf&&showTypes['Dev-Story']?devDirectKeys.map(k=>imap[k]).filter(d=>d&&matchesSprint(d,spf)&&matchesStatus(d,sf)):[];
  const idMatch=!q||matchesId(s,q)||
    stKeys.some(k=>imap[k]&&matchesId(imap[k],q))||
    devDirectKeys.some(k=>imap[k]&&matchesId(imap[k],q))||
    stKeys.some(k=>(SUBTASK_DEV_MAP[k]||[]).some(dk=>imap[dk]&&matchesId(imap[dk],q)));
  const normalSubtasks=!spf&&showTypes['Sub-Task']?stKeys.map(k=>imap[k]).filter(st=>{{
    if(!st||!matchesStatus(st,sf))return false;
    if(q)return matchesId(st,q)||(SUBTASK_DEV_MAP[st.key]||[]).some(dk=>imap[dk]&&matchesId(imap[dk],q));
    return true;
  }}):[];
  const normalDirectDevs=!spf&&showTypes['Dev-Story']?devDirectKeys.map(k=>imap[k]).filter(d=>{{
    if(!d||!matchesStatus(d,sf))return false;
    return !q||matchesId(d,q);
  }}):[];
  const subtaskChildren=spf?sprintSubtasks:normalSubtasks;
  const devDirectChildren=spf?sprintDirectDevs:normalDirectDevs;
  const isDokuCancelled=s.status==='Abgebrochen'&&(s.labels||[]).includes('Doku');
  const sVis=showTypes.Story&&!spf&&idMatch&&matchesStatus(s,sf)&&!isDokuCancelled;
  if(!sVis&&subtaskChildren.length===0&&devDirectChildren.length===0)return '';
  cnt.s++;
  const hasChildren=subtaskChildren.length>0||devDirectChildren.length>0;
  const open=spf?true:!collapsed[s.key];
  const hasDeps=s.links&&s.links.length>0;
  let h=`<div class="story-row" onclick="${{spf?'':`toggleC('${{s.key}}')`}}">`;
  h+=hasChildren?`<span class="chev${{open?' open':''}}">&rsaquo;</span>`:`<span style="width:14px;flex-shrink:0"></span>`;
  h+=`<span class="bs">Story</span><span class="ikey"><a href="${{jiraLink(s.key)}}" target="_blank" onclick="event.stopPropagation()">${{s.key}}</a></span><div class="in"><div>${{s.summary}}</div>${{(s.labels||[]).length?`<div class="lbs">${{s.labels.map(l=>`<span class="lb">${{l}}</span>`).join('')}}</div>`:''}}</div><span class="pill ${{sc(s.status)}}">${{sl(s.status)}}</span>${{hasDeps?'<span style="color:#aaa;font-size:12px">&#128279;</span>':''}}</div>`;
  if(showDeps&&hasDeps&&!spf)s.links.forEach(lk=>{{h+=depRow(lk,true);}});
  if(open){{
    subtaskChildren.forEach(st=>{{h+=subtaskHtml(st,q,sf,spf,cnt);}});
    devDirectChildren.forEach(d=>{{h+=devHtml(d,cnt,true);}});
  }}
  return h;
}}
function collapseAll(){{
  Object.keys(imap).forEach(k=>{{collapsed[k]=true;}});
  collapsed['__orphan']=true;
  render();
}}
function render(){{
  const q=document.getElementById('search').value.trim();
  const sf=document.getElementById('sf').value;
  const spf=document.getElementById('spf').value;
  const cnt={{e:0,s:0,st:0,d:0}};
  const epicChildMap={{}};
  STORIES.filter(s=>s.parent&&epicKeys.has(s.parent)).forEach(s=>{{
    if(!epicChildMap[s.parent])epicChildMap[s.parent]=[];
    epicChildMap[s.parent].push(s);
  }});
  let html='';
  EPICS.forEach(ep=>{{
    const sts=epicChildMap[ep.key]||[];
    let ch='';let hv=false;
    sts.forEach(s=>{{const sh=storyHtml(s,q,sf,spf,cnt);if(sh){{ch+=sh;hv=true;}}}});
    const showEmpty=!q&&!spf&&!sf;
    if(!hv&&!showEmpty)return;
    cnt.e++;
    const open=spf?true:!collapsed[ep.key];
    const emptyClass=!hv?' epic-empty':'';
    const sprintClass=spf&&hv?' sprint-match':'';
    html+=`<div class="epic-block${{sprintClass}}"><div class="epic-row${{emptyClass}}" onclick="toggleC('${{ep.key}}')"><span class="chev${{open?' open':''}}">&rsaquo;</span><span class="be">Epic</span><span class="ikey"><a href="${{jiraLink(ep.key)}}" target="_blank" onclick="event.stopPropagation()">${{ep.key}}</a></span><span class="en" title="${{ep.summary}}">${{ep.summary}}</span><span class="pill ${{sc(ep.status)}}">${{sl(ep.status)}}</span></div>${{open&&hv?`<div>${{ch}}</div>`:''}}</div>`;
  }});
  const allPlaced=new Set([...Object.values(STORY_DEV_MAP),...Object.values(SUBTASK_DEV_MAP)].flat());
  const orphanStories=STORIES.filter(s=>!s.parent||!epicKeys.has(s.parent));
  const orphanDevs=DEVS.filter(d=>!allPlaced.has(d.key)&&(!spf||matchesSprint(d,spf))&&matchesStatus(d,sf));
  let oh='';
  orphanStories.forEach(s=>{{const sh=storyHtml(s,q,sf,spf,cnt);oh+=sh;}});
  orphanDevs.forEach(d=>{{if(!q||matchesId(d,q))oh+=devHtml(d,cnt,false);}});
  if(oh){{
    const open=spf?true:!collapsed['__orphan'];
    html+=`<div class="orphan"><div class="orphan-h" onclick="toggleC('__orphan')"><span class="chev${{open?' open':''}}">&rsaquo;</span>Ohne Epic</div>${{open?`<div>${{oh}}</div>`:''}}</div>`;
  }}
  if(!html)html='<div class="no-results">Keine Ergebnisse.</div>';
  document.getElementById('tree').innerHTML=html;
  document.getElementById('stats').innerHTML=`<strong>${{cnt.e}}</strong> Epics &nbsp;<strong>${{cnt.s}}</strong> Stories &nbsp;<strong>${{cnt.st}}</strong> Sub-Tasks &nbsp;<strong>${{cnt.d}}</strong> Dev-Stories <span class="hint">Stand: ${{TIMESTAMP}} &middot; [PM] ausgeblendet</span>`;
}}
render();
</script>
</body>
</html>"""


# ============================================================
# HTML GENERIEREN (Reporting)
# ============================================================
def generate_report_html(rd: dict, timestamp: str, budget_cfg: dict, chartjs_vendored: bool) -> str:
    chartjs_script_tag = (
        '<script src="vendor/chart.umd.min.js"></script>'
        if chartjs_vendored else
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>'
        '<!-- Vendoring fehlgeschlagen, Fallback auf CDN - siehe Build-Log -->'
    )
    sprint_nums = sorted(rd["sprint_stats"].keys(), key=lambda x: int(x))
    sprint_labels = [f"S{n}" for n in sprint_nums]

    cum = 0
    burnup_dev = []
    throughput_dev = []
    for n in sprint_nums:
        done = rd["sprint_stats"][n]["done"]
        cum += done
        burnup_dev.append(cum)
        throughput_dev.append(done)

    doku_done_flat = [rd["doku_done"]] * len(sprint_nums)
    dev_scope_flat = [rd["dev_total"]] * len(sprint_nums)

    rollup_rows = ""
    for name, r in sorted(rd["rollup"].items(), key=lambda x: -(x[1]["doku_total"] + x[1]["dev_total"])):
        total = r["doku_total"] + r["dev_total"]
        done = r["doku_done"] + r["dev_done"]
        pct = round(100 * done / total) if total else 0
        is_orphan = name == "(kein Epic zugeordnet)"
        badge = "backlog" if done == 0 else ("progress" if done < total else "done")
        if is_orphan:
            badge = "blocked"
        rollup_rows += f"""
        <tr>
          <td{' style="color:#c02b48"' if is_orphan else ''}>{name}</td>
          <td><span class="badge {badge}">{'Wird nachgepflegt' if is_orphan else ('Fertig' if pct==100 and total>0 else ('In Arbeit' if done>0 else 'Backlog'))}</span></td>
          <td>{r['doku_done']}/{r['doku_total']}</td>
          <td>{r['dev_done']}/{r['dev_total']}</td>
          <td>{done}/{total}</td>
          <td><div class="bar-outer"><div class="bar-inner" style="width:{pct}%;{'background:var(--pink);' if is_orphan else ''}"></div></div></td>
        </tr>"""

    # Budget: aus optionaler Config-Datei, sonst Platzhalter (null → Chart.js zeigt Lücke)
    budget_sprints = budget_cfg.get("sprints", sprint_labels)
    budget_plan = budget_cfg.get("plan_cumulative", [None] * len(budget_sprints))
    budget_actual = budget_cfg.get("actual_cumulative", [None] * len(budget_sprints))
    budget_currency = budget_cfg.get("currency", "EUR")
    has_budget = any(v is not None for v in budget_actual)

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EOSPAY Reporting – {timestamp}</title>
{chartjs_script_tag}
<style>
  :root{{
    --bg:#f5f5f3; --card:#fff; --card-border:#e0dfd8; --text:#1a1a1a; --muted:#8a8a86;
    --cyan:#1d6fb8; --purple:#5b21b6; --green:#166534; --amber:#92400e; --pink:#9d174d; --grid:#eceae4;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;color:var(--text);background:var(--bg);padding:16px}}
  h1{{font-size:15px;font-weight:600;margin-bottom:4px;color:#111}}
  .updated{{font-size:11px;color:#aaa;margin-bottom:14px}}
  .kpi-row{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}}
  .kpi{{background:var(--card);border:1px solid var(--card-border);border-radius:8px;padding:12px 14px}}
  .kpi .val{{font-size:20px;font-weight:700}}
  .kpi .label{{font-size:10px;letter-spacing:.04em;color:var(--muted);text-transform:uppercase;margin-top:3px}}
  .kpi .sub{{font-size:11px;color:var(--muted);margin-top:2px}}
  .grid2{{display:grid;grid-template-columns:1.2fr 1fr;gap:14px;margin-bottom:14px}}
  .panel{{background:var(--card);border:1px solid var(--card-border);border-radius:8px;padding:14px 14px 8px}}
  .panel h2{{font-size:13px;margin-bottom:2px;font-weight:600}}
  .panel .desc{{font-size:11px;color:var(--muted);margin-bottom:10px}}
  table{{width:100%;border-collapse:collapse;font-size:12px}}
  th{{text-align:left;font-size:10px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);padding:7px 8px;border-bottom:1px solid var(--card-border)}}
  td{{padding:8px;border-bottom:1px solid var(--grid)}}
  tr:last-child td{{border-bottom:none}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:600}}
  .badge.done{{background:#dcfce7;color:var(--green)}}
  .badge.progress{{background:#dbeafe;color:#1e40af}}
  .badge.blocked{{background:#fee2e2;color:#991b1b}}
  .badge.backlog{{background:#f1f0ea;color:var(--muted)}}
  .bar-outer{{background:var(--grid);border-radius:6px;height:6px;width:100%;overflow:hidden;margin-top:2px}}
  .bar-inner{{height:100%;background:var(--cyan);border-radius:6px}}
  .footnote{{font-size:11px;color:var(--muted);margin-top:14px;line-height:1.6;border-top:1px solid var(--card-border);padding-top:12px}}
  .footnote b{{color:var(--text)}}
</style>
</head>
<body>
{nav_html('report')}
<h1>EOSPAY Reporting</h1>
<div class="updated">Stand: {timestamp} &middot; automatisch generiert via GitHub Actions &middot; Datenbasis: Doku-Stories + Dev-Stories (Tasks/Defects/PM-Board ausgeklammert)</div>

<div class="kpi-row">
  <div class="kpi">
    <div class="val">{rd['doku_done'] + rd['dev_done']} / {rd['doku_total'] + rd['dev_total']}</div>
    <div class="label">Erledigt / bekannt (Doku + Dev)</div>
    <div class="sub">Doku: {rd['doku_done']}/{rd['doku_total']} &middot; Dev: {rd['dev_done']}/{rd['dev_total']}</div>
  </div>
  <div class="kpi">
    <div class="val">{(sum(throughput_dev)/len(throughput_dev)):.1f} / Sprint</div>
    <div class="label">Ø Dev-Story-Throughput</div>
    <div class="sub">{', '.join(str(v) for v in throughput_dev)} &middot; Doku nicht sprintgebunden</div>
  </div>
  <div class="kpi">
    <div class="val">{f"{budget_cfg.get('actual_cumulative', [None])[-1] or '––'} / {budget_cfg.get('plan_cumulative', [None])[-1] or '––'} {budget_currency}" if has_budget else "–– / ––"}</div>
    <div class="label">Budget verbraucht / gesamt</div>
    <div class="sub">{"aus docs/report_budget.json" if has_budget else "Platzhalter – docs/report_budget.json noch nicht gepflegt"}</div>
  </div>
  <div class="kpi">
    <div class="val">{rd['no_epic_dev'] + rd['no_epic_doku']} Items</div>
    <div class="label">Noch ohne Epic-Zuordnung</div>
    <div class="sub">{rd['inherited_count']} Dev-Stories automatisch über Doku-Verlinkung zugeordnet</div>
  </div>
</div>

<div class="grid2">
  <div class="panel">
    <h2>Burn-up nach Story-Typ</h2>
    <div class="desc">Dev-Stories kumuliert je Sprint. Doku-Stories als Referenzlinie (nicht sprintgebunden).</div>
    <canvas id="burnup" height="220"></canvas>
  </div>
  <div class="panel">
    <h2>Budget-Burn{' (Platzhalter)' if not has_budget else ''}</h2>
    <div class="desc">{'Aus docs/report_budget.json.' if has_budget else 'Noch keine Daten – docs/report_budget.json im Repo anlegen/pflegen, siehe Kommentar im Skript.'}</div>
    <canvas id="budget" height="220"></canvas>
  </div>
</div>

<div class="grid2">
  <div class="panel">
    <h2>Dev-Story-Throughput je Sprint</h2>
    <div class="desc">Abgeschlossene Dev-Stories pro Sprint – schätzungsfreier Ersatz für Velocity.</div>
    <canvas id="throughput" height="220"></canvas>
  </div>
  <div class="panel">
    <h2>Epic-Übersicht</h2>
    <div class="desc">Doku/Dev getrennt gezählt. "(kein Epic zugeordnet)" wird laufend händisch nachgepflegt.</div>
    <table>
      <tr><th>Epic</th><th>Status</th><th>Doku</th><th>Dev</th><th>Summe</th><th></th></tr>
      {rollup_rows}
    </table>
  </div>
</div>

<div class="footnote">
  <b>Warum keine einzelne %-Zahl:</b> Der Gesamt-Scope verändert sich laufend. Burn-up, Throughput und Epic-Tabelle zeigen <b>was</b> passiert, statt eine unscharfe Aussage über <b>wie fertig</b> etwas ist zu treffen.<br><br>
  <b>Epic-Zuordnung:</b> {rd['inherited_count']} Dev-Stories ohne eigenes Epic wurden automatisch über ihre Verlinkung zur zugehörigen Doku-Story (Link-Typ "Discovery – Connected", beliebige Richtung) einem Epic zugeordnet. Verbleibende Items ohne Epic werden laufend händisch nachgepflegt.<br><br>
  <b>Abgebrochen-Status:</b> {rd['dev_cancelled'] + rd['doku_cancelled']} Items haben den Status "Abgebrochen", der in Jira zur Kategorie "Done" zählt und daher in "Erledigt" mitgezählt wird (Doku: {rd['doku_cancelled']}, Dev: {rd['dev_cancelled']}).
</div>

<script>
const sprintLabels={json.dumps(sprint_labels, ensure_ascii=False)};
const gridColor='#eceae4';
Chart.defaults.color='#8a8a86';
Chart.defaults.font.size=11;

new Chart(document.getElementById('burnup'), {{
  type:'line',
  data:{{
    labels: sprintLabels,
    datasets:[
      {{label:'Dev-Story Scope bekannt ({rd["dev_total"]})', data:{json.dumps(dev_scope_flat)}, borderColor:'#5b21b6', backgroundColor:'transparent', borderWidth:2, borderDash:[5,4], pointRadius:0}},
      {{label:'Dev-Story erledigt (kumuliert)', data:{json.dumps(burnup_dev)}, borderColor:'#1d6fb8', backgroundColor:'rgba(29,111,184,0.12)', borderWidth:2, fill:true, tension:0.25, pointRadius:3}},
      {{label:'Doku-Story erledigt gesamt ({rd["doku_done"]}/{rd["doku_total"]})', data:{json.dumps(doku_done_flat)}, borderColor:'#166534', backgroundColor:'transparent', borderWidth:2, borderDash:[2,3], pointRadius:0}}
    ]
  }},
  options:{{plugins:{{legend:{{position:'bottom',labels:{{boxWidth:10,padding:12,font:{{size:10}}}}}}}}, scales:{{x:{{grid:{{color:gridColor}}}}, y:{{grid:{{color:gridColor}}}}}}}}
}});

new Chart(document.getElementById('budget'), {{
  type:'line',
  data:{{
    labels: {json.dumps(budget_sprints, ensure_ascii=False)},
    datasets:[
      {{label:'Plan (kumuliert)', data:{json.dumps(budget_plan)}, borderColor:'#8a8a86', borderDash:[5,4], borderWidth:2, pointRadius:2}},
      {{label:'Ist (kumuliert)', data:{json.dumps(budget_actual)}, borderColor:'#166534', backgroundColor:'rgba(22,101,52,0.12)', borderWidth:2, fill:true, tension:0.2, pointRadius:3}}
    ]
  }},
  options:{{plugins:{{legend:{{position:'bottom',labels:{{boxWidth:10,padding:12,font:{{size:10}}}}}}}}, scales:{{x:{{grid:{{color:gridColor}}}}, y:{{grid:{{color:gridColor}}, title:{{display:true,text:'{budget_currency}'}}}}}}}}
}});

new Chart(document.getElementById('throughput'), {{
  type:'bar',
  data:{{labels: sprintLabels, datasets:[{{label:'Erledigte Dev-Stories', data:{json.dumps(throughput_dev)}, backgroundColor:'#1d6fb8', borderRadius:4}}]}},
  options:{{plugins:{{legend:{{display:false}}}}, scales:{{x:{{grid:{{display:false}}}}, y:{{grid:{{color:gridColor}}}}}}}}
}});
</script>
</body>
</html>"""


def send_teams_notification(timestamp: str, stats: dict, pages_url: str):
    if not TEAMS_WEBHOOK_URL:
        print("Teams Webhook nicht konfiguriert – übersprungen.")
        return

    payload = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": "0076D7",
        "summary": "EOSPAY Treeview aktualisiert",
        "sections": [{
            "activityTitle": "📋 EOSPAY Jira Treeview – Wochenupdate",
            "activitySubtitle": f"Stand: {timestamp}",
            "facts": [
                {"name": "Epics", "value": str(stats["epics"])},
                {"name": "Stories", "value": str(stats["stories"])},
                {"name": "Sub-Tasks", "value": str(stats["subtasks"])},
                {"name": "Dev-Stories", "value": str(stats["devs"])},
                {"name": "Link", "value": pages_url or "–"},
            ],
        }],
        "potentialAction": ([{
            "@type": "OpenUri",
            "name": "Treeview öffnen ↗",
            "targets": [{"os": "default", "uri": pages_url}]
        }] if pages_url else [])
    }

    try:
        resp = requests.post(TEAMS_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        print("Teams-Benachrichtigung gesendet ✓")
    except Exception as e:
        print(f"Teams-Benachrichtigung fehlgeschlagen: {e}")


# ============================================================
# MAIN
# ============================================================
def main():
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    print(f"\n{'='*55}")
    print(f"EOSPAY Treeview Updater (API v3) – {timestamp}")
    print(f"{'='*55}\n")

    try:
        all_issues = fetch_all_issues()
    except requests.HTTPError as e:
        print(f"FEHLER beim Jira-Abruf: {e}")
        print(f"Response: {e.response.text[:500]}")
        sys.exit(1)

    if not all_issues:
        print("FEHLER: Keine Issues geladen – Credentials oder Projektname prüfen.")
        sys.exit(1)

    print("\nBereite Treeview-Daten auf...")
    data = prepare_data(all_issues)

    print("\nGeneriere Treeview-HTML...")
    html = generate_html(data, timestamp)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"Datei geschrieben: {OUTPUT_FILE} ({len(html):,} Zeichen)")

    print("\nBereite Reporting-Daten auf...")
    report_data = compute_report_data(all_issues)
    budget_cfg = load_budget_config()

    print("\nPrüfe Chart.js Vendoring...")
    ensure_chartjs_vendored()
    chartjs_vendored = CHARTJS_VENDOR_PATH.exists()

    print("\nGeneriere Reporting-HTML...")
    report_html = generate_report_html(report_data, timestamp, budget_cfg, chartjs_vendored)
    REPORT_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_OUTPUT_FILE.write_text(report_html, encoding="utf-8")
    print(f"Datei geschrieben: {REPORT_OUTPUT_FILE} ({len(report_html):,} Zeichen)")

    stats = {
        "epics": len(data["epics"]),
        "stories": len(data["stories"]),
        "subtasks": len(data["subtasks"]),
        "devs": len(data["devs"]),
    }
    send_teams_notification(timestamp, stats, GITHUB_PAGES_URL)

    print(f"\n✓ Fertig – {timestamp}\n")


if __name__ == "__main__":
    main()
