import os, re, json, base64, textwrap
from typing import List, Dict
import requests
from github import Github

GITHUB_REPO = os.getenv("GITHUB_REPOSITORY")
PR_NUMBER   = os.getenv("GITHUB_REF", "").split("/")[-1]  # fallback
GITHUB_TOKEN= os.getenv("GITHUB_TOKEN")

JIRA_BASE   = os.getenv("JIRA_BASE_URL").rstrip("/")
JIRA_EMAIL  = os.getenv("JIRA_EMAIL")
JIRA_TOKEN  = os.getenv("JIRA_API_TOKEN")
JIRA_AC_FIELD_ID = os.getenv("JIRA_AC_FIELD_ID")  # e.g. customfield_12345

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

def gh():
    return Github(GITHUB_TOKEN).get_repo(GITHUB_REPO)

def get_pr():
    # More robust: read from env GITHUB_EVENT_PATH
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        with open(event_path) as f:
            ev = json.load(f)
        return gh().get_pull(ev["number"])
    # fallback parse number from ref
    return gh().get_pull(int(PR_NUMBER))

def extract_issue_keys(pr, changed_commits):
    texts = [pr.title or "", pr.body or "", pr.head.ref or ""]
    for c in changed_commits:
        texts.append(c.commit.message or "")
    keys = set()
    for t in texts:
        for m in ISSUE_KEY_RE.findall(t):
            keys.add(m)
    return sorted(keys)

def jira_get_issue(key):
    url = f"{JIRA_BASE}/rest/api/3/issue/{key}"
    r = requests.get(url, auth=(JIRA_EMAIL, JIRA_TOKEN))
    r.raise_for_status()
    return r.json()

def get_acceptance_criteria(issue_json) -> List[str]:
    fields = issue_json.get("fields", {})
    # 1) dedicated field
    if JIRA_AC_FIELD_ID and fields.get(JIRA_AC_FIELD_ID):
        raw = fields[JIRA_AC_FIELD_ID]
        text = raw if isinstance(raw, str) else str(raw)
    else:
        # 2) parse from description (wiki/ADF → fallback plain text)
        desc = fields.get("description")
        text = ""
        if isinstance(desc, dict) and "content" in desc:
            # simplistic ADF flatten:
            def walk(n):
                if isinstance(n, dict):
                    t = n.get("text","")
                    parts = [t]
                    for c in n.get("content",[]):
                        parts.append(walk(c))
                    return "\n".join([p for p in parts if p])
                elif isinstance(n, list):
                    return "\n".join(walk(x) for x in n)
                return ""
            text = walk(desc)
        elif isinstance(desc, str):
            text = desc or ""
    # split bullets/checklist-like lines into criteria
    lines = [l.strip("-*[] ").strip() for l in text.splitlines()]
    candidates = [l for l in lines if len(l) > 0]
    # heuristic: keep lines under an "Acceptance Criteria" section if present
    ac = []
    in_ac = False
    for l in candidates:
        if re.search(r"(?i)^acceptance\s*criteria", l):
            in_ac = True
            continue
        if in_ac and re.match(r"(?i)^(given|when|then|and|or)\b", l):
            ac.append(l)
        elif in_ac and (l.startswith("-") or l.startswith("*") or len(l) > 0):
            ac.append(l)
        # stop when a new section header appears
        if in_ac and re.match(r"^#+\s|\w+\s*:\s*$", l):
            break
    if not ac and candidates:
        # fallback: take bullet-ish lines
        ac = [l for l in candidates if re.match(r"^(\*|-|\[\s?[x ]\])", l, re.I) or len(l) <= 140]
    # dedupe
    seen, dedup = set(), []
    for c in ac:
        if c not in seen:
            seen.add(c); dedup.append(c)
    return dedup[:20]  # keep it bounded

def get_pr_diff_and_files(pr):
    files = list(pr.get_files())
    diff_summary = []
    for f in files:
        diff_summary.append(f"* {f.filename} (+{f.additions}/-{f.deletions})")
    # Fetch raw unified diff
    headers = {"Accept": "application/vnd.github.v3.diff",
               "Authorization": f"token {GITHUB_TOKEN}"}
    diff_url = pr.url
    diff = requests.get(diff_url, headers=headers).text
    return "\n".join(diff_summary), diff[:200000]  # bound size

def run_llm(criteria: List[str], pr_context: Dict[str, str]) -> List[Dict]:
    # Use OpenAI responses API (simple JSON style)
    import openai
    openai.api_key = OPENAI_API_KEY

    system = "You are a senior engineer acting as a strict QA reviewer. Return only JSON."
    user = textwrap.dedent(f"""
    Assess whether each Acceptance Criterion is met by this pull request.
    For each criterion, return:
    - criterion (string)
    - status ("Pass" | "Fail" | "Unclear")
    - evidence (short, quote code/test names or lines)
    - notes (brief rationale)

    Pull Request Context:
    PR Title: {pr_context["title"]}
    PR Body:
    {pr_context["body"]}

    Changed Files Summary:
    {pr_context["files"]}

    Unified Diff (truncated):
    {pr_context["diff"][:120000]}

    Acceptance Criteria:
    {json.dumps(criteria, ensure_ascii=False, indent=2)}
    """)

    # Lightweight, deterministic-ish pass
    resp = openai.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.1,
        response_format={"type":"json_object"},
        messages=[{"role":"system", "content":system},
                  {"role":"user", "content":user}]
    )
    content = resp.choices[0].message.content
    data = json.loads(content)
    # Expect either {"results":[...]} or a list
    results = data.get("results", data if isinstance(data, list) else [])
    return results

def post_pr_comment(pr, md):
    pr.create_issue_comment(md)

def set_status(pr, ok: bool):
    state = "success" if ok else "failure"
    pr.create_check_run(name="Acceptance Criteria Check", head_sha=pr.head.sha, status="completed", conclusion=("success" if ok else "failure"))

def md_table(issue_key, results):
    rows = ["| Criterion | Status | Evidence | Notes |",
            "|---|---|---|---|"]
    for r in results:
        rows.append(f"| {r.get('criterion','').replace('|','\\|')[:140]} | {r.get('status','')} | {r.get('evidence','').replace('|','\\|')[:120]} | {r.get('notes','').replace('|','\\|')[:160]} |")
    return f"### {issue_key}\n" + "\n".join(rows)

def comment_to_jira(issue_key, md):
    url = f"{JIRA_BASE}/rest/api/3/issue/{issue_key}/comment"
    payload = {"body": md}
    r = requests.post(url, auth=(JIRA_EMAIL, JIRA_TOKEN), json=payload)
    # ignore failures but don’t crash the check
    try: r.raise_for_status()
    except: pass

def main():
    pr = get_pr()
    commits = list(pr.get_commits())
    issue_keys = extract_issue_keys(pr, commits)
    if not issue_keys:
        post_pr_comment(pr, "⚠️ No Jira issue keys found in the branch/PR title/commits. Please include e.g. `PROJ-123`.")
        set_status(pr, False)
        return

    files_summary, diff = get_pr_diff_and_files(pr)
    all_ok = True
    overall_md = ["## 🤖 Acceptance Criteria Check"]
    for key in issue_keys:
        try:
            issue = jira_get_issue(key)
            criteria = get_acceptance_criteria(issue)
            if not criteria:
                overall_md.append(f"### {key}\n⚠️ Could not find Acceptance Criteria. Please ensure they’re in the AC field or description.")
                all_ok = False
                continue

            pr_ctx = {
                "title": pr.title or "",
                "body": pr.body or "",
                "files": files_summary,
                "diff": diff
            }
            results = run_llm(criteria, pr_ctx)

            # aggregate status
            statuses = [r.get("status","Unclear") for r in results]
            if any(s in ("Fail","Unclear") for s in statuses):
                all_ok = False
            overall_md.append(md_table(key, results))

            # (optional) push a succinct note back to Jira
            short = f"PR #{pr.number} AC check: " + ", ".join(statuses)
            comment_to_jira(key, short)
        except Exception as e:
            overall_md.append(f"### {key}\n❌ Error checking: `{e}`")
            all_ok = False

    post_pr_comment(pr, "\n\n".join(overall_md))
    set_status(pr, all_ok)

if __name__ == "__main__":
    main()
