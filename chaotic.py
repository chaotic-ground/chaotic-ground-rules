#!/usr/bin/env python3
"""README.md의 규칙을 chaotic-ground 조직의 모든 저장소에 적용합니다.

환경 변수:
  CHAOTIC_TOKEN  조직 저장소에 푸시하고 협업자 권한을 바꿀 수 있는 토큰
  DRY_RUN        1이면 아무것도 바꾸지 않고 할 일만 출력합니다
"""

import base64
import calendar
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import yaml

ORG = "chaotic-ground"
API = "https://api.github.com"
TOKEN = os.environ["CHAOTIC_TOKEN"]
DRY_RUN = os.environ.get("DRY_RUN") == "1"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "champions.json")
NOW = dt.datetime.now(dt.timezone.utc)


def api(method, path, body=None, params=None):
    url = path if path.startswith("http") else API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req) as res:
            raw = res.read()
            return (json.loads(raw) if raw else None), res.headers
    except urllib.error.HTTPError as e:
        if e.code == 404 or (e.code == 409 and "empty" in e.read().decode().lower()):
            return None, e.headers
        raise


def get(path, **params):
    return api("GET", path, params=params or None)[0]


def get_all(path, **params):
    params.setdefault("per_page", 100)
    url, items = API + path + "?" + urllib.parse.urlencode(params), []
    while url:
        page, headers = api("GET", url)
        items += page or []
        m = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link") or "")
        url = m.group(1) if m else None
    return items


def parse_period(text):
    """"3 months", "2 weeks", "10 days" 같은 기간을 받아 그만큼 이전 시각을 돌려줍니다."""
    m = re.fullmatch(r"\s*(\d+)\s*(day|week|month|year)s?\s*", str(text))
    if not m:
        sys.exit(f"알 수 없는 기간: {text!r}")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "day":
        return NOW - dt.timedelta(days=n)
    if unit == "week":
        return NOW - dt.timedelta(weeks=n)
    months = n * (12 if unit == "year" else 1)
    y, mo = divmod(NOW.year * 12 + NOW.month - 1 - months, 12)
    d = min(NOW.day, calendar.monthrange(y, mo + 1)[1])
    return NOW.replace(year=y, month=mo + 1, day=d)


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def fork_network(repo):
    """원본과 모든 포크, 포크의 포크를 돌려줍니다."""
    root = repo["source"]["full_name"] if repo.get("fork") else repo["full_name"]
    queue, seen = [get(f"/repos/{root}")], {}
    while queue:
        r = queue.pop()
        if r is None or r["id"] in seen:
            continue
        seen[r["id"]] = r
        if r.get("forks_count", 0) > 0:
            queue += get_all(f"/repos/{r['full_name']}/forks")
    return list(seen.values())


def last_commit_time(r):
    commits = get(f"/repos/{r['full_name']}/commits", sha=r["default_branch"], per_page=1)
    if not commits:
        return None
    return parse_time(commits[0]["commit"]["committer"]["date"])


def candidates(network, since):
    """1. 후보: archived가 아니고 기본 브랜치에 최근 N 안의 커밋이 있는 저장소."""
    result = []
    for r in network:
        if r.get("archived"):
            continue
        # 푸시가 N보다 오래됐으면 커밋도 N보다 오래됐으므로 API 호출을 아낍니다.
        if r.get("pushed_at") and parse_time(r["pushed_at"]) < since:
            continue
        t = last_commit_time(r)
        if t and t >= since:
            result.append((r, t))
    return result


def is_bot(user):
    return user.get("type") == "Bot" or user["login"].endswith("[bot]")


def maintainers(champion, since, min_commits):
    """5. 메인테이너: 최근 N 동안 커밋 K개 이상인 사람과 개인 계정 소유자."""
    counts = {}
    for c in get_all(f"/repos/{champion['full_name']}/commits",
                     sha=champion["default_branch"], since=since.isoformat()):
        author = c.get("author")
        if author and not is_bot(author):
            counts[author["login"]] = counts.get(author["login"], 0) + 1
    people = {login for login, n in counts.items() if n >= min_commits}
    if champion["owner"]["type"] == "User":
        people.add(champion["owner"]["login"])
    return people


def sync_branch(org_repo, champion):
    """4. org 저장소의 기본 브랜치를 챔피언의 기본 브랜치에 맞춥니다."""
    src = get(f"/repos/{champion['full_name']}/branches/{champion['default_branch']}")
    dst = get(f"/repos/{org_repo['full_name']}/branches/{org_repo['default_branch']}")
    if dst and src["commit"]["sha"] == dst["commit"]["sha"]:
        return
    print(f"  {org_repo['default_branch']} <- {champion['full_name']}@{src['commit']['sha'][:7]}")
    if DRY_RUN:
        return
    auth = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    git = ["git", "-c", f"http.extraheader=AUTHORIZATION: basic {auth}"]
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", "--bare", d], check=True)
        if dst:
            # 공통 이력을 먼저 받아 두면 챔피언에서는 차이만 받습니다.
            subprocess.run(git + ["-C", d, "fetch", "-q", "--no-tags",
                                  f"https://github.com/{org_repo['full_name']}.git",
                                  f"refs/heads/{org_repo['default_branch']}:refs/heads/old"], check=True)
        subprocess.run(git + ["-C", d, "fetch", "-q", "--no-tags",
                              f"https://github.com/{champion['full_name']}.git",
                              f"refs/heads/{champion['default_branch']}:refs/heads/new"], check=True)
        subprocess.run(git + ["-C", d, "push", "-q", "--force",
                              f"https://github.com/{org_repo['full_name']}.git",
                              f"refs/heads/new:refs/heads/{org_repo['default_branch']}"], check=True)


def apply_maintainers(org_repo, people):
    """Maintain을 주고, 더 이상 해당하지 않는 Maintain은 Write로 내립니다."""
    name = org_repo["full_name"]
    current = {c["login"]: c["role_name"] for c in get_all(f"/repos/{name}/collaborators", affiliation="direct")}
    pending = {i["invitee"]["login"] for i in get_all(f"/repos/{name}/invitations") if i.get("invitee")}
    for login in sorted(people):
        if current.get(login) in ("maintain", "admin") or login in pending:
            continue
        print(f"  maintain: {login}")
        if not DRY_RUN:
            api("PUT", f"/repos/{name}/collaborators/{login}", {"permission": "maintain"})
    for login, role in sorted(current.items()):
        if role == "maintain" and login not in people:
            print(f"  write: {login}")
            if not DRY_RUN:
                api("PUT", f"/repos/{name}/collaborators/{login}", {"permission": "push"})


def update_description(org_repo, champion, elected_at):
    text = f"챔피언: {champion['full_name']} (선출 {elected_at[:10]})"
    if org_repo.get("description") != text:
        print(f"  description: {text}")
        if not DRY_RUN:
            api("PATCH", f"/repos/{org_repo['full_name']}", {"description": text})


def main():
    with open(os.path.join(HERE, "chaotic.yaml")) as f:
        config = yaml.safe_load(f) or {}
    defaults = config.get("defaults") or {}
    overrides = config.get("overrides") or {}
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}

    for org_repo in get_all(f"/orgs/{ORG}/repos", type="all"):
        if org_repo.get("archived"):
            continue
        org_repo = get(f"/repos/{org_repo['full_name']}")  # source 필드가 필요합니다.
        name = org_repo["name"]
        settings = {**defaults, **(overrides.get(name) or {})}
        since = parse_period(settings.get("active_within", "3 months"))
        min_commits = int(settings.get("min_commits", 3))
        print(f"{org_repo['full_name']} (N={settings.get('active_within')}, K={min_commits})")

        cands = candidates(fork_network(org_repo), since)
        previous = state.get(name)
        kept = [r for r, _ in cands if previous and r["id"] == previous["id"]]
        if kept:
            champion = kept[0]  # 3. 자격을 유지하는 동안에는 교체하지 않습니다.
            elected_at = previous["elected_at"]
        elif cands:
            # 2. 별이 가장 많은 저장소, 같으면 마지막 커밋이 더 최근인 저장소.
            champion = max(cands, key=lambda c: (c[0]["stargazers_count"], c[1]))[0]
            elected_at = NOW.isoformat(timespec="seconds")
        else:
            print("  후보가 없어 그대로 둡니다.")
            continue

        if not previous or previous["id"] != champion["id"]:
            print(f"  챔피언 선출: {champion['full_name']}")
        state[name] = {"id": champion["id"], "champion": champion["full_name"], "elected_at": elected_at}

        if champion["id"] != org_repo["id"]:
            sync_branch(org_repo, champion)
        update_description(org_repo, champion, elected_at)
        apply_maintainers(org_repo, maintainers(champion, since, min_commits))

    if not DRY_RUN:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")


if __name__ == "__main__":
    main()
