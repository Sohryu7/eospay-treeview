"""
EOSPAY Jira Treeview Updater – GitHub Actions Version
======================================================
API: Jira REST API v3  →  /rest/api/3/search/jql
Paginierung: nextPageToken
Typen: Epic, Story, Sub-Task, Dev-Story
Hierarchie: Epic → Story → Sub-Task → Dev-Story
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

JIRA_BASE_URL    = "https://apk.atlassian.net"
JIRA_EMAIL       = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN   = os.environ["JIRA_API_TOKEN"]
JIRA_PROJECT     = "EOSPAY"
PM_TEAM_ID       = "586f2bf4-7e68-4e18-a519-989b592bdf4d"
PM_EPIC_PREFIX   = "[PM]"

TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL", "")
GITHUB_PAGES_URL  = os.environ.get("GITHUB_PAGES_URL", "")

OUTPUT_FILE = Path("docs/index.html")

FIELDS = [
    "summary", "status", "issuetype", "parent", "subtasks",
    "issuelinks", "labels", "customfield_10001", "customfield_10020"
]

TARGET_TYPES = {"Epic", "Story", "Sub-Task", "Dev-Story"}


# ============================================================
# JIRA API v3  –  /rest/api/3/search/jql  mit nextPageToken
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
            "jql":        jql,
            "fields":     ",".join(fields),
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

        # nextPageToken kann im Body oder als 'nextPageToken' key stehen
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
        name  = sp.get("name", "")
        state = sp.get("state", "")
        m = re.search(r"Sprint\s+(\d+)", name)
        if m:
            sprint_info = {"num": m.group(1), "state": state}
            break

    # Issue links → blocked-by / blocks
    links = []
    for lk in f.get("issuelinks", []):
        ltype = lk.get("type", {})
        # "is blocked by" → inward; "blocks" → outward
        # We normalise to blocked-by / blocks
        if "inwardIssue" in lk:
            links.append({"type": "blocked-by", "key": lk["inwardIssue"]["key"]})
        if "outwardIssue" in lk:
            links.append({"type": "blocks", "key": lk["outwardIssue"]["key"]})

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
# DATEN AUFBEREITEN
# ============================================================

def prepare_data(all_issues: list) -> dict:
    imap = {i["key"]: i for i in all_issues}

    pm_story_keys = {
        i["key"] for i in all_issues
        if i["type"] == "Story" and i["team_id"] == PM_TEAM_ID
    }

    epics = [
        {"key": i["key"], "status": i["status"], "summary": i["summary"]}
        for i in all_issues
        if i["type"] == "Epic" and not i["summary"].startswith(PM_EPIC_PREFIX)
    ]
    epic_keys = set(e["key"] for e in epics)

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

    # Story → [Sub-Task keys]  (via subtask_keys field on story)
    story_subtask_map = {}
    for i in all_issues:
        if i["type"] == "Story" and i["key"] not in pm_story_keys:
            if i["subtask_keys"]:
                story_subtask_map[i["key"]] = i["subtask_keys"]

    # Dev-Story resolution:
    # Priority 1: blocked-by Sub-Task  → place under that Sub-Task
    # Priority 2: blocked-by Story     → place directly under that Story
    story_dev_map   = {}   # story_key   → [dev_key]
    subtask_dev_map = {}   # subtask_key → [dev_key]

    for d in devs:
        placed = False
        for lk in d["links"]:
            if lk["type"] == "blocked-by":
                target = imap.get(lk["key"])
                if not target:
                    continue
                if target["type"] == "Sub-Task":
                    subtask_dev_map.setdefault(lk["key"], []).append(d["key"])
                    placed = True
                    break
                if target["type"] == "Story" and lk["key"] not in pm_story_keys:
                    story_dev_map.setdefault(lk["key"], []).append(d["key"])
                    placed = True
                    break

    print(f"\n  Epics (ohne [PM]):          {len(epics)}")
    print(f"  Stories (ohne PM-Team):     {len(stories)}")
    print(f"  Sub-Tasks:                  {len(subtasks)}")
    print(f"  Dev-Stories:                {len(devs)}")
    print(f"  PM-Stories ausgeblendet:    {len(pm_story_keys)}")
    print(f"  Dev via Sub-Task platziert: {sum(len(v) for v in subtask_dev_map.values())}")
    print(f"  Dev via Story platziert:    {sum(len(v) for v in story_dev_map.values())}")
    unplaced = len(devs) - sum(len(v) for v in subtask_dev_map.values()) - sum(len(v) for v in story_dev_map.values())
    print(f"  Dev ohne Zuordnung:         {unplaced}")

    return {
        "epics":            epics,
        "stories":          stories,
        "subtasks":         subtasks,
        "devs":             devs,
        "sprints":          sprints,
        "pm_exclude":       list(pm_story_keys),
        "story_subtask_map": story_subtask_map,
        "story_dev_map":     story_dev_map,
        "subtask_dev_map":   subtask_dev_map,
    }


# ============================================================
# HTML GENERIEREN
# ============================================================

def generate_html(data: dict, timestamp: str) -> str:
    sprint_nums = sorted(
        set(v["num"] for v in data["sprints"].values() if v["num"]),
        key=lambda x: int(x)
    )
    sprint_options = "\n    ".join(
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
.toolbar input{{flex:1;min-width:160px;font-size:13px;padding:6px 10px;border:1px solid #ccc;border-radius:6px;background:#fff}}
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
<h1>EOSPAY Jira Treeview</h1>
<div class="updated">Stand: {timestamp} &middot; automatisch generiert via GitHub Actions</div>
<div class="toolbar">
  <input type="text" id="search" placeholder="Suche ID, Name, Label, Sprint..." oninput="render()">
  <select id="sf" onchange="render()">
    <option value="">Alle Status</option>
    <option value="TO DO">To Do</option>
    <option value="In Arbeit">In Arbeit</option>
    <option value="Test">Test</option>
    <option value="Wird überprüft">Wird überprüft</option>
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
</div>
<div class="legend">
  <span class="be">Epic</span> &rsaquo; <span class="bs">Story</span> &rsaquo; <span class="bst">Sub-Task</span> &rsaquo; <span class="bd">Dev</span>
  &nbsp;|&nbsp; Sprint: <span class="sb sc">Sx</span> abgeschlossen &nbsp;<span class="sb sf">Sx</span> geplant
</div>
<div class="stats" id="stats" style="margin-top:8px"></div>
<div id="tree"></div>
<script>
const PM_EXCLUDE=new Set({json.dumps(data["pm_exclude"],ensure_ascii=False)});
const SPRINTS={json.dumps(data["sprints"],ensure_ascii=False)};
const EPICS={json.dumps(data["epics"],ensure_ascii=False)};
const RAW_STORIES={json.dumps(data["stories"],ensure_ascii=False)};
const SUBTASKS={json.dumps(data["subtasks"],ensure_ascii=False)};
const DEVS={json.dumps(data["devs"],ensure_ascii=False)};
const STORY_SUBTASK_MAP={json.dumps(data["story_subtask_map"],ensure_ascii=False)};
const STORY_DEV_MAP={json.dumps(data["story_dev_map"],ensure_ascii=False)};
const SUBTASK_DEV_MAP={json.dumps(data["subtask_dev_map"],ensure_ascii=False)};
const TIMESTAMP="{timestamp}";

const STORIES=RAW_STORIES.filter(s=>!PM_EXCLUDE.has(s.key));
const epicKeys=new Set(EPICS.map(e=>e.key));
const imap={{}};
EPICS.forEach(e=>imap[e.key]={{...e,type:'Epic'}});
STORIES.forEach(s=>imap[s.key]=s);
SUBTASKS.forEach(s=>imap[s.key]=s);
DEVS.forEach(d=>imap[d.key]=d);

let showTypes={{'Story':true,'Sub-Task':true,'Dev-Story':true}};
let showDeps=false;
let collapsed={{}};

function sc(s){{if(s==='Fertig')return 'p-done';if(s==='In Arbeit')return 'p-ip';if(s==='Abgebrochen')return 'p-cancel';if(s==='Test')return 'p-test';if(s==='Wird überprüft')return 'p-review';return 'p-todo';}}
function sl(s){{return s==='TO DO'?'To Do':s;}}
function spBadge(key){{const sp=SPRINTS[key];if(!sp||!sp.num)return '';const c=sp.state==='active'?'sa':sp.state==='future'?'sf':'sc';return `<span class="sb ${{c}}">S${{sp.num}}</span>`;}}
function jiraLink(key){{return `https://apk.atlassian.net/browse/${{key}}`;}}
function toggleType(t){{showTypes[t]=!showTypes[t];document.getElementById('btn-'+t).classList.toggle('active',showTypes[t]);render();}}
function toggleDeps(){{showDeps=!showDeps;document.getElementById('btn-deps').classList.toggle('active',showDeps);render();}}
function toggleC(k){{collapsed[k]=!collapsed[k];render();}}

function matchesFilter(i,q,sf,spf){{
  if(sf&&i.status!==sf)return false;
  if(spf){{if(i.type!=='Dev-Story')return false;const sp=SPRINTS[i.key];if(!sp||sp.num!==spf)return false;}}
  if(!q)return true;
  const ql=q.toLowerCase();
  const spn=SPRINTS[i.key]?'sprint '+SPRINTS[i.key].num:'';
  return i.key.toLowerCase().includes(ql)||i.summary.toLowerCase().includes(ql)||(i.labels||[]).some(l=>l.toLowerCase().includes(ql))||spn.includes(ql);
}}

function depRow(lk,direct){{
  const t=imap[lk.key];
  const cls=lk.type==='blocks'?'db-bl':'db-bby';
  const lbl=lk.type==='blocks'?'blockiert':'blockiert durch';
  const dcls=direct?'dep dep-direct':'dep';
  return `<div class="${{dcls}}"><span>&#8594;</span><span class="db ${{cls}}">${{lbl}}</span><span class="dep-k"><a href="${{jiraLink(lk.key)}}" target="_blank">${{lk.key}}</a></span>${{t?`<span class="dep-n">– ${{t.summary}}</span>`:'<span class="dep-n"><i>(außerhalb View)</i></span>'}}</div>`;
}}

function devHtml(d,cnt,direct){{
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
  const devChildren=showTypes['Dev-Story']?devKeys.map(k=>imap[k]).filter(d=>d&&matchesFilter(d,q,sf,spf)):[];
  const stVis=showTypes['Sub-Task']&&(spf?false:matchesFilter(st,q,sf,''));
  if(!stVis&&devChildren.length===0)return '';
  cnt.st++;
  const hasChildren=devChildren.length>0;
  const open=!collapsed[st.key];
  const hasDeps=st.links&&st.links.length>0;
  let h=`<div class="subtask-row" onclick="toggleC('${{st.key}}')">`;
  h+=hasChildren?`<span class="chev${{open?' open':''}}">&rsaquo;</span>`:`<span style="width:14px;flex-shrink:0"></span>`;
  h+=`<span class="bst">Sub-Task</span><span class="ikey"><a href="${{jiraLink(st.key)}}" target="_blank" onclick="event.stopPropagation()">${{st.key}}</a></span><div class="in"><div>${{st.summary}}</div></div><span class="pill ${{sc(st.status)}}">${{sl(st.status)}}</span>${{hasDeps?'<span style="color:#aaa;font-size:12px">&#128279;</span>':''}}</div>`;
  if(showDeps&&hasDeps&&!spf)st.links.forEach(lk=>{{h+=depRow(lk,false);}});
  if(open&&devChildren.length>0)devChildren.forEach(d=>{{h+=devHtml(d,cnt,false);}});
  return h;
}}

function storyHtml(s,q,sf,spf,cnt){{
  const stKeys=(STORY_SUBTASK_MAP[s.key]||[]);
  const devDirectKeys=(STORY_DEV_MAP[s.key]||[]);
  const subtaskChildren=showTypes['Sub-Task']?stKeys.map(k=>imap[k]).filter(st=>{{
    if(!st)return false;
    if(spf)return (SUBTASK_DEV_MAP[k]||[]).some(dk=>{{const d=imap[dk];return d&&matchesFilter(d,q,sf,spf);}});
    return matchesFilter(st,q,sf,'');
  }}):[];
  const devDirectChildren=showTypes['Dev-Story']?devDirectKeys.map(k=>imap[k]).filter(d=>d&&matchesFilter(d,q,sf,spf)):[];
  const sVis=showTypes.Story&&(spf?false:matchesFilter(s,q,sf,''));
  if(!sVis&&subtaskChildren.length===0&&devDirectChildren.length===0)return '';
  cnt.s++;
  const hasChildren=subtaskChildren.length>0||devDirectChildren.length>0;
  const open=!collapsed[s.key];
  const hasDeps=s.links&&s.links.length>0;
  let h=`<div class="story-row" onclick="toggleC('${{s.key}}')">`;
  h+=hasChildren?`<span class="chev${{open?' open':''}}">&rsaquo;</span>`:`<span style="width:14px;flex-shrink:0"></span>`;
  h+=`<span class="bs">Story</span><span class="ikey"><a href="${{jiraLink(s.key)}}" target="_blank" onclick="event.stopPropagation()">${{s.key}}</a></span><div class="in"><div>${{s.summary}}</div>${{(s.labels||[]).length?`<div class="lbs">${{s.labels.map(l=>`<span class="lb">${{l}}</span>`).join('')}}</div>`:''}}</div><span class="pill ${{sc(s.status)}}">${{sl(s.status)}}</span>${{hasDeps?'<span style="color:#aaa;font-size:12px">&#128279;</span>':''}}</div>`;
  if(showDeps&&hasDeps&&!spf)s.links.forEach(lk=>{{h+=depRow(lk,true);}});
  if(open){{
    subtaskChildren.forEach(st=>{{h+=subtaskHtml(st,q,sf,spf,cnt);}});
    devDirectChildren.forEach(d=>{{h+=devHtml(d,cnt,true);}});
  }}
  return h;
}}

function render(){{
  const q=document.getElementById('search').value.trim();
  const sf=document.getElementById('sf').value;
  const spf=document.getElementById('spf').value;
  const cnt={{e:0,s:0,st:0,d:0}};

  const epicChildMap={{}};
  STORIES.filter(s=>s.parent&&epicKeys.has(s.parent)).forEach(s=>{{if(!epicChildMap[s.parent])epicChildMap[s.parent]=[];epicChildMap[s.parent].push(s);}});

  let html='';
  EPICS.forEach(ep=>{{
    const sts=epicChildMap[ep.key]||[];
    let ch='';let hv=false;
    sts.forEach(s=>{{const sh=storyHtml(s,q,sf,spf,cnt);if(sh){{ch+=sh;hv=true;}}}});
    if(!hv)return;
    cnt.e++;
    const open=!collapsed[ep.key];
    html+=`<div class="epic-block"><div class="epic-row" onclick="toggleC('${{ep.key}}')"><span class="chev${{open?' open':''}}">&rsaquo;</span><span class="be">Epic</span><span class="ikey"><a href="${{jiraLink(ep.key)}}" target="_blank" onclick="event.stopPropagation()">${{ep.key}}</a></span><span class="en" title="${{ep.summary}}">${{ep.summary}}</span><span class="pill ${{sc(ep.status)}}">${{sl(ep.status)}}</span></div>${{open?`<div>${{ch}}</div>`:''}}</div>`;
  }});

  const allPlaced=new Set([...Object.values(STORY_DEV_MAP),...Object.values(SUBTASK_DEV_MAP)].flat());
  const orphanStories=STORIES.filter(s=>(!s.parent||!epicKeys.has(s.parent))&&!spf);
  const orphanDevs=DEVS.filter(d=>!allPlaced.has(d.key)&&showTypes['Dev-Story']&&matchesFilter(d,q,sf,spf));
  if(orphanStories.length||orphanDevs.length){{
    const open=!collapsed['__orphan'];
    let oh='';
    orphanStories.forEach(s=>{{oh+=storyHtml(s,q,sf,spf,cnt);}});
    orphanDevs.forEach(d=>{{oh+=devHtml(d,cnt,false);}});
    if(oh)html+=`<div class="orphan"><div class="orphan-h" onclick="toggleC('__orphan')"><span class="chev${{open?' open':''}}">&rsaquo;</span>Ohne Epic</div>${{open?`<div>${{oh}}</div>`:''}}</div>`;
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
# TEAMS BENACHRICHTIGUNG
# ============================================================

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
                {"name": "Epics",       "value": str(stats["epics"])},
                {"name": "Stories",     "value": str(stats["stories"])},
                {"name": "Sub-Tasks",   "value": str(stats["subtasks"])},
                {"name": "Dev-Stories", "value": str(stats["devs"])},
                {"name": "Link",        "value": pages_url or "–"},
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

    print("\nBereite Daten auf...")
    data = prepare_data(all_issues)

    print("\nGeneriere HTML...")
    html = generate_html(data, timestamp)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"Datei geschrieben: {OUTPUT_FILE}  ({len(html):,} Zeichen)")

    stats = {
        "epics":    len(data["epics"]),
        "stories":  len(data["stories"]),
        "subtasks": len(data["subtasks"]),
        "devs":     len(data["devs"]),
    }
    send_teams_notification(timestamp, stats, GITHUB_PAGES_URL)

    print(f"\n✓ Fertig – {timestamp}\n")


if __name__ == "__main__":
    main()
