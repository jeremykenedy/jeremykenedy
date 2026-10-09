"""Collect public evidence for authored projects and upstream contributions."""

import base64
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
import re
from urllib.error import HTTPError
from urllib.parse import quote


OWNER = "jeremykenedy"
API = "https://api.github.com"
INVENTORY_QUERY = '''query($cursor:String) {
 user(login:"jeremykenedy") {
  repositories(first:15, after:$cursor, privacy:PUBLIC, isFork:false, ownerAffiliations:OWNER,
   orderBy:{field:PUSHED_AT,direction:DESC}) {
   totalCount pageInfo {hasNextPage endCursor}
   nodes {name url description isFork isPrivate isArchived isEmpty pushedAt stargazerCount forkCount
    licenseInfo {spdxId name}
    languages(first:6, orderBy:{field:SIZE,direction:DESC}) {nodes {name}}
    fundingLinks {platform url}
    releases(first:1, orderBy:{field:CREATED_AT,direction:DESC}) {totalCount nodes {tagName publishedAt isDraft url}}
    defaultBranchRef {name target {... on Commit {history(first:30) {totalCount nodes {committedDate messageHeadline}}}}}
   }
  }
 }
}'''


def fetch_inventory(fetch):
    repositories, cursor = [], None
    while True:
        result = fetch(f"{API}/graphql", {"query": INVENTORY_QUERY, "variables": {"cursor": cursor}})
        if result.get("errors"):
            raise ValueError("Could not verify the public project inventory")
        page = result["data"]["user"]["repositories"]
        repositories.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            if len(repositories) != page["totalCount"]:
                raise ValueError("The project inventory is incomplete")
            return repositories
        cursor = page["pageInfo"]["endCursor"]


def activity_date(repo):
    branch = repo.get("defaultBranchRef")
    if not branch:
        return None
    commits = branch["target"]["history"]["nodes"]
    return next((item["committedDate"] for item in commits
                 if not re.search(r"funding|sponsor", item["messageHeadline"], re.IGNORECASE)), None)


def select_projects(repositories, config, today):
    cutoff = today - timedelta(days=365.25 * config["stale_after_years"])
    selected = []
    for repo in repositories:
        if repo["isPrivate"] or repo["isFork"] or repo["isArchived"] or repo["isEmpty"]:
            continue
        if repo["name"] in config["excluded"]:
            continue
        license_id = (repo.get("licenseInfo") or {}).get("spdxId")
        updated = activity_date(repo)
        if not license_id or license_id == "NOASSERTION" or not updated:
            continue
        date = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if date < cutoff and repo["stargazerCount"] < config["legacy_star_threshold"]:
            continue
        selected.append({**repo, "activity_date": updated})
    return selected


def order_projects(projects, featured, today):
    def rank(project):
        date = datetime.fromisoformat(project["updated"].replace("Z", "+00:00"))
        if project["name"] in featured and (today - date).days <= 90:
            return (0, featured.index(project["name"]), 0)
        return (1, -date.timestamp(), -project["stars"])
    ordered = sorted(projects, key=rank)
    return sorted(ordered, key=lambda project: (project['stars'] < 10, -project['stars'] if project['stars'] >= 10 else 0))


def readme_text(fetch, name):
    try:
        data = fetch(f"{API}/repos/{OWNER}/{name}/readme")
    except HTTPError as error:
        if error.code != 404:
            raise
        return ""
    return base64.b64decode(data["content"]).decode(errors="replace")


def made_with_laravel(readme):
    match = re.search(r'https://madewithlaravel\.com/p/[^\s"<>\)]+', readme)
    return unescape(match.group(0)) if match else None


def project_copy(repo, overrides):
    name = repo["name"]
    languages = [item["name"] for item in repo["languages"]["nodes"]]
    readme_hint = repo["description"] or "Open source software and tools."
    description = readme_hint.split(". ")[0].rstrip(".") + "."
    is_tv = "screensaver" in description.lower() or "tv" in name.lower()
    if is_tv:
        description = re.sub(r" for Fire TV.*", " for your TV.", description)
    title = name.replace("-", " ").title()
    return {"title": title, "description": description,
            "category": "Creative graphics" if is_tv else "Software and tools",
            "stack": " / ".join(languages[:3]), **overrides.get(name, {})}


def download_info(repo, readme, package_sources, packages, releases):
    names = package_sources.get(repo["name"].lower(), [])
    if names:
        return {"count": sum(packages[name] for name in names), "label": "Packagist downloads",
                "url": f"https://packagist.org/packages/{names[0]}"}
    if re.search(r'shields\.io/github/downloads/', readme, re.IGNORECASE):
        return {"count": sum(asset["download_count"] for release in releases for asset in release.get("assets", [])),
                "label": "Release downloads", "url": repo["url"] + "/releases"}
    return None


def npm_downloads(fetch, readme):
    match = re.search(r'shields\.io/npm/(dt|d18m|dy|dm|dw)/([\w@/.-]+)', readme)
    if not match:
        return None
    interval, package = match.groups()
    package = package.removesuffix('.svg')
    periods = {'dy': ('last-year', 'year'), 'dm': ('last-month', 'month'), 'dw': ('last-week', 'week')}
    if interval in periods:
        period, label = periods[interval]
        data = fetch(f'https://api.npmjs.org/downloads/point/{period}/{quote(package, safe="@")}')
        count = data['downloads']
    else:
        data = fetch(f'https://api.npmjs.org/downloads/range/1000-01-01:3000-01-01/{quote(package, safe="@")}')
        count = sum(day['downloads'] for day in data['downloads'])
        label = '18mo'
    return {'count': count, 'label': 'npm downloads', 'period': label,
            'url': f'https://www.npmjs.com/package/{package}', 'start': data['start'], 'end': data['end']}


def fetch_releases(fetch, name):
    releases, page = [], 1
    while True:
        batch = fetch(f"{API}/repos/{OWNER}/{name}/releases?per_page=100&page={page}")
        releases.extend(item for item in batch if not item["draft"] and item["published_at"])
        if len(batch) < 100:
            return releases
        page += 1


def collect_project(fetch, repo, config, package_sources, packages):
    name = repo["name"]
    readme = readme_text(fetch, name)
    sidebar = fetch(f"https://github.com/{OWNER}/{name}/_sidebar")
    if not isinstance(sidebar, dict) or not {'usedBy', 'contributors'}.issubset(sidebar):
        raise ValueError(f'Could not verify the public repository sidebar for {name}')
    releases = fetch_releases(fetch, name)
    latest = max(releases, key=lambda item: item["published_at"], default=None)
    used_by = sidebar.get("usedBy")
    contributors = sidebar.get("contributors")
    return {
        "name": name, "url": repo["url"], **project_copy(repo, config["copy"]),
        "updated": repo["activity_date"], "stars": repo["stargazerCount"], "forks": repo["forkCount"],
        "license": repo["licenseInfo"]["spdxId"], "branch": repo["defaultBranchRef"]["name"],
        "commits": repo["defaultBranchRef"]["target"]["history"]["totalCount"],
        "contributors": contributors["contributorCount"] if contributors else None,
        "used_by": used_by["dependentCount"] if used_by else None,
        "releases": len(releases),
        "latest_release": {"tag": latest["tag_name"], "date": latest["published_at"], "url": latest["html_url"]} if latest else None,
        "downloads": download_info(repo, readme, package_sources, packages, releases) or npm_downloads(fetch, readme),
        "made_with_laravel": made_with_laravel(readme), "funding": repo["fundingLinks"],
    }


def summarize_contributions(items, excluded):
    projects = defaultdict(lambda: {"merged": 0, "open": 0, "closed": 0, "pull_requests": []})
    seen = set()
    for item in items:
        repository = item["repository_url"].split("/repos/")[-1]
        if item["id"] in seen or repository in excluded or repository.lower().startswith(OWNER + "/"):
            continue
        seen.add(item["id"])
        state = "merged" if item["pull_request"].get("merged_at") else item["state"]
        project = projects[repository]
        project[state] += 1
        project["pull_requests"].append({"title": item["title"], "url": item["html_url"], "state": state,
                                         "updated": item["updated_at"]})
    rows = [{"name": name, "url": f"https://github.com/{name}", **value} for name, value in projects.items()]
    rows.sort(key=lambda row: max(item["updated"] for item in row["pull_requests"]), reverse=True)
    return {"projects": rows, "repositories": len(rows), "pull_requests": len(seen),
            "merged": sum(row["merged"] for row in rows), "open": sum(row["open"] for row in rows)}


def fetch_contributions(fetch, excluded):
    items, page = [], 1
    query = quote(f"is:pr is:public author:{OWNER} -user:{OWNER}")
    while True:
        data = fetch(f"{API}/search/issues?q={query}&per_page=100&page={page}&sort=updated")
        if data["incomplete_results"] or data["total_count"] > 1000:
            raise ValueError("Public contribution search is incomplete")
        items.extend(data["items"])
        if len(items) >= data["total_count"]:
            return summarize_contributions(items, excluded)
        if not data["items"]:
            raise ValueError("Public contribution search returned an empty page")
        page += 1


def collect(fetch, config, metrics):
    today = datetime.now(timezone.utc)
    repositories = fetch_inventory(fetch)
    selected = select_projects(repositories, config, today)
    sources = defaultdict(list)
    for project in metrics["publications"]["projects"]:
        for listing in project["publications"]:
            if listing["channel"] == "packagist":
                sources[project["repository"].split("/")[-1]].append(listing["name"])
    with ThreadPoolExecutor(max_workers=3) as pool:
        projects = list(pool.map(lambda repo: collect_project(fetch, repo, config, sources, metrics["packages"]), selected))
    commits = fetch(f"{API}/search/commits?q=author:{OWNER}+is:public&per_page=1")
    if commits["incomplete_results"]:
        raise ValueError("Public commit search is incomplete")
    return {"showcase": order_projects(projects, config["featured"], today),
            "contributions": fetch_contributions(fetch, config["excluded_contributions"]),
            "public_authored_commits": commits["total_count"]}
