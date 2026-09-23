#!/usr/bin/env python3
"""Refresh public portfolio metrics and render the profile's light/dark artwork."""

import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import textwrap
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
OWNER = "jeremykenedy"
API = "https://api.github.com"
PACKAGIST = "https://packagist.org"
NPM = "https://registry.npmjs.org"
NPM_PUBLISHERS = ("developernator", "jeremykenedy")
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
PROJECTS = [
    {
        "repo": "ai-platform", "title": "AI Platform", "category": "APPLIED AI / PLATFORM ARCHITECTURE",
        "lines": ["Self-hosted inference, persistent memory,", "and real-time conversations."],
        "stack": "Vue · PostgreSQL · Redis · Docker", "accent": "purple",
    },
    {
        "repo": "PandaVentOS", "title": "PandaVentOS", "category": "EMBEDDED SYSTEMS / HARDWARE",
        "lines": ["Open firmware, live printer telemetry,", "and a web interface in 24 languages."],
        "stack": "ESP32 · ESP-IDF · Web UI", "accent": "blue",
    },
    {
        "repo": "claude-rules-mcp-server", "title": "MCP Developer Tooling", "category": "DEVELOPER EXPERIENCE / TYPESCRIPT",
        "lines": ["Searchable engineering rules and skills", "over authenticated HTTP and stdio."],
        "stack": "TypeScript · Node.js · MCP · Docker", "accent": "blue",
    },
    {
        "repo": "laravel-auth", "title": "Application Foundations", "category": "IDENTITY / FULL-STACK ENGINEERING",
        "lines": ["Authentication, social sign-in, account", "recovery, and user management."],
        "stack": "PHP · JavaScript · Laravel", "accent": "purple",
    },
    {
        "repo": "laravel-roles", "title": "Access Control", "category": "AUTHORIZATION / REUSABLE SYSTEMS",
        "lines": ["Roles and permissions with a management", "interface for application teams."],
        "stack": "PHP · Laravel · RBAC", "accent": "purple",
    },
    {
        "repo": "laravel-logger", "title": "Application Observability", "category": "OPERATIONS / DEVELOPER TOOLING",
        "lines": ["Activity logging, configurable events,", "and a dashboard for investigation."],
        "stack": "PHP · Laravel · Activity logging", "accent": "blue",
    },
]
THEMES = {
    "dark": {"bg": "#101318", "panel": "#161b22", "border": "#303640", "text": "#f0f3f8",
             "muted": "#a5afbf", "purple": "#a78bfa", "blue": "#60a5fa", "track": "#252b37"},
    "light": {"bg": "#ffffff", "panel": "#f8fafc", "border": "#e5e7eb", "text": "#0f172a",
              "muted": "#526176", "purple": "#6d28d9", "blue": "#2563eb", "track": "#e8ecf3"},
}
LANGUAGE_COLORS = ["#8b5cf6", "#3b82f6", "#22c55e", "#f59e0b", "#ec4899", "#64748b"]


class NoRedirects(HTTPRedirectHandler):
    """Reject redirects so API credentials cannot be forwarded to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(url, payload=None):
    """Keep credentials confined to GitHub; fail instead of publishing partial totals."""
    parsed = urlparse(url)
    host = parsed.hostname
    if parsed.scheme != "https" or host not in {"api.github.com", "packagist.org", "registry.npmjs.org"}:
        raise ValueError(f"Unexpected API URL: {url}")
    headers = {"User-Agent": "jeremykenedy-profile (+https://github.com/jeremykenedy/jeremykenedy)"}
    if host == "api.github.com":
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        if host != "api.github.com":
            raise ValueError("Only GitHub GraphQL requests may include a payload")
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    for attempt in range(3):
        try:
            with build_opener(NoRedirects).open(Request(url, headers=headers, data=data), timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)


def fetch_repositories():
    repositories = []
    page = 1
    while True:
        batch = fetch_json(f"{API}/users/{OWNER}/repos?type=owner&per_page=100&sort=full_name&page={page}")
        if not isinstance(batch, list):
            raise ValueError("GitHub did not return a repository list")
        repositories.extend(batch)
        if len(batch) < 100:
            return repositories
        page += 1


def summarize_repositories(repositories):
    public = {
        repo["id"]: repo for repo in repositories
        if repo["owner"]["login"].lower() == OWNER and not repo["private"]
    }
    originals = [repo for repo in public.values() if not repo["fork"]]
    languages = Counter(repo["language"] for repo in originals if repo["language"])
    return {
        "original_repositories": len(originals),
        "stars": sum(repo["stargazers_count"] for repo in originals),
        "forks": sum(repo["forks_count"] for repo in originals),
        "languages": dict(sorted(languages.items(), key=lambda item: (-item[1], item[0]))),
        "repositories": {
            repo["name"]: {key: repo[key] for key in ("stargazers_count", "forks_count", "language")}
            for repo in originals if repo["name"] in {project["repo"] for project in PROJECTS}
        },
    }


def repository_identity(url):
    """Match package metadata to its source project, including git URL variants."""
    normalized = str(url).removeprefix("git+")
    if normalized.startswith("git@github.com:"):
        normalized = "https://github.com/" + normalized.split(":", 1)[1]
    parsed = urlparse(normalized)
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname != "github.com" or len(parts) < 2:
        raise ValueError(f"Cannot identify a GitHub source repository: {url}")
    return f"{parts[0]}/{parts[1].removesuffix('.git')}".lower()


def summarize_publications(entries, github_package_count=0):
    """Count distribution listings separately from unique source projects."""
    projects = {}
    counts = {name: 0 for name in ("packagist", "npm", "homebrew", "github_release_repositories")}
    seen = set()
    for entry in entries:
        key = (entry["channel"], entry["name"])
        if key in seen:
            continue
        seen.add(key)
        channel = entry["channel"]
        counts["github_release_repositories" if channel == "github_release" else channel] += 1
        # Taps distribute the underlying software; they are not extra software projects.
        if channel == "github_release" and entry["repository"].split("/")[-1].startswith("homebrew-"):
            continue
        project = projects.setdefault(entry["repository"], {"repository": entry["repository"], "publications": []})
        project["publications"].append(entry)
    registry_projects = sum(
        any(item["channel"] != "github_release" for item in project["publications"])
        for project in projects.values()
    )
    counts.update({
        "github_packages": github_package_count,
        "registry_listings": counts["packagist"] + counts["npm"] + counts["homebrew"] + github_package_count,
        "registry_projects": registry_projects,
        "additional_github_projects": len(projects) - registry_projects,
        "unique_projects": len(projects),
    })
    return {"counts": counts, "projects": [projects[key] for key in sorted(projects)]}


def fetch_public_release(repo):
    page = 1
    while True:
        releases = fetch_json(f"{API}/repos/{OWNER}/{repo['name']}/releases?per_page=30&page={page}")
        if not isinstance(releases, list):
            raise ValueError(f"Invalid release list for {repo['name']}")
        for release in releases:
            if not release["draft"] and release["published_at"]:
                return {"channel": "github_release", "name": repo["name"],
                        "repository": repository_identity(repo["html_url"]), "url": release["html_url"]}
        if len(releases) < 30:
            return None
        page += 1


def fetch_npm_publications():
    entries = []
    npm_names = set()
    for publisher in NPM_PUBLISHERS:
        offset = 0
        while True:
            result = fetch_json(f"{NPM}/-/v1/search?text=maintainer:{publisher}&size=250&from={offset}")
            batch = result["objects"]
            npm_names.update(item["package"]["name"] for item in batch)
            offset += len(batch)
            if offset >= result["total"]:
                break
            if not batch:
                raise ValueError("npm search returned an incomplete page")
    for name in sorted(npm_names):
        package = fetch_json(f"{NPM}/{quote(name, safe='')}")
        latest = package["dist-tags"]["latest"]
        if latest not in package["versions"]:
            raise ValueError(f"npm publication is missing its latest version: {name}")
        metadata = package.get("repository", package["versions"][latest].get("repository"))
        source = repository_identity(metadata["url"] if isinstance(metadata, dict) else metadata)
        entries.append({"channel": "npm", "name": name, "repository": source,
                        "url": f"https://www.npmjs.com/package/{name}"})
    return entries


def fetch_homebrew_publications(originals):
    entries = []
    for tap in (repo for repo in originals if repo["name"].startswith("homebrew-")):
        tree = fetch_json(f"{API}/repos/{OWNER}/{tap['name']}/git/trees/HEAD?recursive=1")
        if tree.get("truncated"):
            raise ValueError(f"Homebrew tap inventory was truncated: {tap['name']}")
        for item in tree["tree"]:
            if item["type"] != "blob" or not item["path"].startswith(("Formula/", "Casks/")) or not item["path"].endswith(".rb"):
                continue
            blob = fetch_json(f"{API}/repos/{OWNER}/{tap['name']}/contents/{quote(item['path'])}")
            formula = base64.b64decode(blob["content"]).decode()
            homepage = re.search(r'^\s*homepage\s+[\"\x27]([^\"\x27]+)', formula, re.MULTILINE)
            if not homepage:
                raise ValueError(f"Homebrew formula has no source homepage: {item['path']}")
            entries.append({"channel": "homebrew", "name": f"{tap['name']}/{item['path']}",
                            "repository": repository_identity(homepage.group(1)), "url": blob["html_url"]})
    return entries


def fetch_github_package_count():
    registry = fetch_json(f"{API}/graphql", {"query": f'query {{ user(login:"{OWNER}") {{ packages(first:1) {{ totalCount }} }} }}'})
    if registry.get("errors"):
        raise ValueError("GitHub Packages count could not be verified")
    github_package_count = registry["data"]["user"]["packages"]["totalCount"]
    if github_package_count:
        raise ValueError("New GitHub Packages detected; map them to source projects before refreshing the total")
    return github_package_count


def fetch_publications(repositories, packagist_packages):
    entries = [
        {"channel": "packagist", "name": name, "repository": repository_identity(package["repository"]),
         "url": f"{PACKAGIST}/packages/{name}"}
        for name, package in sorted(packagist_packages.items())
    ]
    originals = [repo for repo in repositories if not repo["private"] and not repo["fork"]
                 and repo["owner"]["login"].lower() == OWNER]
    with ThreadPoolExecutor(max_workers=4) as pool:
        entries.extend(entry for entry in pool.map(fetch_public_release, originals) if entry)
    entries.extend(fetch_npm_publications())
    entries.extend(fetch_homebrew_publications(originals))
    return summarize_publications(entries, fetch_github_package_count())


def fetch_metrics():
    repositories = fetch_repositories()
    metrics = summarize_repositories(repositories)
    packages = fetch_json(f"{PACKAGIST}/packages/list.json?vendor={OWNER}&fields[]=repository")["packages"]
    if not packages or any(not name.startswith(f"{OWNER}/") for name in packages):
        raise ValueError("Packagist returned an unexpected package list")

    def package_stats(name):
        result = fetch_json(f"{PACKAGIST}/packages/{name}/stats.json")
        downloads = result["downloads"]["total"]
        if not isinstance(downloads, int) or downloads < 0:
            raise ValueError(f"Invalid download count for {name}")
        return name, downloads

    with ThreadPoolExecutor(max_workers=4) as pool:
        downloads = dict(pool.map(package_stats, sorted(set(packages))))
    metrics.update({
        "updated": datetime.now(timezone.utc).date().isoformat(),
        "package_downloads": sum(downloads.values()),
        "packages": downloads,
        "publications": fetch_publications(repositories, packages),
    })
    for project in PROJECTS:
        if project["repo"] not in metrics["repositories"]:
            raise ValueError(f"Featured project is no longer public and original: {project['repo']}")
    return metrics


def text(x, y, value, size, color, weight=400, extra=""):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" {extra}>{escape(str(value))}</text>'


def rect(x, y, width, height, fill, stroke="none", radius=0, extra=""):
    return f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}" fill="{fill}" stroke="{stroke}" {extra}/>'


def svg(width, height, title, description, body):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">\n'
        f'<title id="title">{escape(title)}</title>\n<desc id="desc">{escape(description)}</desc>\n'
        f'<g font-family="{FONT}">\n' + "\n".join(body) + '\n</g>\n</svg>\n'
    )


def compact(number):
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 10_000:
        return f"{number / 1_000:.1f}k"
    return f"{number:,}"


def banner(theme):
    t = THEMES[theme]
    dark = theme == "dark"
    body = [
        '<defs><linearGradient id="background" x2="1" y2="1"><stop stop-color="#0c0a09"/><stop offset="1" stop-color="#1c1917"/></linearGradient>'
        '<linearGradient id="accent"><stop stop-color="#8b5cf6"/><stop offset="1" stop-color="#3b82f6"/></linearGradient></defs>',
        rect(0, 0, 800, 200, "url(#background)" if dark else "#fff", "none" if dark else "#e5e7eb", 16),
        rect(32, 33, 32, 3, "url(#accent)", radius=1),
        text(75, 39, "PEOPLE. PLATFORMS. PRODUCTS.", 10, t["muted"], 600, 'letter-spacing="1.8"'),
        text(32, 82, "Jeremy Kenedy", 40, "#fff" if dark else "#0f172a", 700, 'letter-spacing="-.5"'),
        text(33, 111, "Engineering leader. Architect. Hands-on builder.", 14, "#a8a29e" if dark else "#64748b"),
    ]
    for x, width, label, color, fill in [
        (33, 104, "TEAM LEADERSHIP", t["purple"], "#f5f3ff"),
        (145, 122, "SYSTEM ARCHITECTURE", t["blue"], "#eff6ff"),
        (275, 98, "OPEN SOURCE", "#4ade80" if dark else "#166534", "#f0fdf4"),
    ]:
        body += [rect(x, 132, width, 22, "#1a1a1a" if dark else fill, color, 11, 'stroke-width=".8" opacity=".8"'),
                 text(x + width / 2, 147, label, 10, color, 500, 'text-anchor="middle"')]
    body.append(text(33, 180, "PORTLAND, OR  /  BUILDING WITH PURPOSE", 9, t["muted"], 500, 'letter-spacing="1.4"'))
    body += [
        '<g transform="translate(568, 34)">',
        '<path d="M32 37 L109 18 L172 51 L169 113 L90 133 L26 94 Z M32 37 L90 71 L172 51 M90 71 L90 133" fill="none" stroke="url(#accent)" stroke-width="1.5"/>',
        '<path d="M109 18 L107 78 L26 94 M107 78 L169 113" fill="none" stroke="url(#accent)" stroke-opacity=".35"/>',
        '<path d="M-3 62 L48 6 M142 134 L197 78" stroke="url(#accent)" stroke-opacity=".2"/>',
    ]
    for x, y, color in [(32, 37, t["purple"]), (109, 18, t["purple"]), (172, 51, t["blue"]),
                        (90, 71, t["purple"]), (26, 94, t["blue"]), (90, 133, t["blue"]), (169, 113, t["purple"])]:
        body.append(f'<circle cx="{x}" cy="{y}" r="5" fill="{t["bg"]}" stroke="{color}" stroke-width="2"/>')
    body += [text(76, 78, "</>", 15, t["text"], 600), '</g>']
    return svg(800, 200, "Jeremy Kenedy", "Engineering leader. Architect. Hands-on builder. People, platforms, and products.", body)


def mobile_banner(theme):
    t = THEMES[theme]
    body = [rect(1, 1, 390, 179, t["bg"], t["border"], 14),
            rect(24, 24, 32, 3, t["purple"], radius=1),
            text(24, 51, "PEOPLE. PLATFORMS. PRODUCTS.", 9, t["muted"], 600, 'letter-spacing="1.4"'),
            text(23, 94, "Jeremy Kenedy", 36, t["text"], 700, 'letter-spacing="-.7"'),
            text(24, 120, "Engineering leader. Architect. Hands-on builder.", 12, t["muted"]),
            text(24, 155, "PORTLAND, OR  /  BUILDING WITH PURPOSE", 9, t["blue"], 500, 'letter-spacing=".8"')]
    return svg(392, 181, "Jeremy Kenedy", "Engineering leader. Architect. Hands-on builder.", body)


def impact(metrics, theme, mobile=False):
    t = THEMES[theme]
    values = [
        ("PACKAGE DOWNLOADS", compact(metrics["package_downloads"]), "Across Packagist packages"),
        ("GITHUB STARS", compact(metrics["stars"]), "Public repos, excluding forks"),
        ("COMMUNITY FORKS", compact(metrics["forks"]), "Of original public repos"),
        ("PUBLISHED PROJECTS", metrics["publications"]["counts"]["unique_projects"], "Packages, apps, and tools"),
    ]
    width, height = (392, 246) if mobile else (800, 143)
    body = [rect(1, 1, width - 2, height - 2, t["bg"], t["border"], 14)]
    for index, (label, value, note) in enumerate(values):
        x = (20 + index % 2 * 190) if mobile else (24 + index * 198)
        y = index // 2 * 116 if mobile else 0
        if not mobile and index:
            body.append(f'<path d="M{x-13} 27 V115" stroke="{t["border"]}"/>')
        body += [text(x, y + 33, label, 9, t["muted"], 600, 'letter-spacing=".7"'),
                 text(x, y + 79, value, 36, t["purple"] if index % 2 == 0 else t["blue"], 700, 'letter-spacing="-1.2"'),
                 text(x, y + 104, note, 8.5, t["muted"])]
    description = "; ".join(f"{label}: {value}" for label, value, _ in values)
    return svg(width, height, "Open source adoption", description, body)


def project_card(project, metrics, theme, width=392):
    t = THEMES[theme]
    accent = t[project["accent"]]
    repo = metrics["repositories"][project["repo"]]
    package = f"{OWNER}/{project['repo']}"
    counts = f"{repo['stargazers_count']:,} stars"
    if package in metrics["packages"]:
        counts += f"  /  {compact(metrics['packages'][package])} downloads"
    elif repo["stargazers_count"] >= 25:
        counts += f"  /  {repo['forks_count']:,} forks"
    else:
        counts = "Explore the code"
    if width != 392:
        narrow = width < 250
        margin = 15 if narrow else 19
        height = 320 if narrow else 284
        body = [rect(1, 1, width - 2, height - 18, t["bg"], t["border"], 12)]
        for index, line in enumerate(textwrap.wrap(project["category"], 25 if narrow else 36)):
            body.append(text(margin, 25 + index * 12, line, 7.5 if narrow else 8, accent, 600, 'letter-spacing=".4"'))
        for index, line in enumerate(textwrap.wrap(project["title"], 17 if narrow else 24)):
            body.append(text(margin, 66 + index * 24, line, 18 if narrow else 21, t["text"], 650, 'letter-spacing="-.4"'))
        for index, line in enumerate(textwrap.wrap(" ".join(project["lines"]), 26 if narrow else 33)):
            body.append(text(margin, 115 + index * 17, line, 11.5 if narrow else 12.5, t["muted"]))
        stack_y = 193 if narrow else 178
        for index, line in enumerate(textwrap.wrap(project["stack"], 29 if narrow else 38)):
            body.append(text(margin, stack_y + index * 15, line, 9 if narrow else 10, accent, 500))
        divider_y = 224 if narrow else 207
        body.append(f'<path d="M{margin} {divider_y} H{width-margin}" stroke="{t["border"]}"/>')
        for index, line in enumerate(textwrap.wrap(counts, 28 if narrow else 38)):
            body.append(text(margin, divider_y + 23 + index * 15, line, 9 if narrow else 10, t["text"], 600))
        body.append(text(margin, height - 34, project["repo"], 8 if narrow else 9, t["muted"]))
        return svg(width, height, project["title"], " ".join(project["lines"]) + " " + counts, body)
    body = [rect(1, 1, 390, 210, t["bg"], t["border"], 12),
            rect(21, 22, 3, 13, accent, radius=1),
            text(32, 32, project["category"], 8.4, accent, 600, 'letter-spacing=".7"'),
            text(21, 65, project["title"], 23, t["text"], 650, 'letter-spacing="-.6"'),
            text(21, 90, project["lines"][0], 12, t["muted"]),
            text(21, 109, project["lines"][1], 12, t["muted"]),
            text(21, 137, project["stack"], 10, accent, 500),
            f'<path d="M21 151 H371" stroke="{t["border"]}"/>',
            text(21, 175, counts, 11, t["text"], 600),
            text(21, 195, project["repo"], 9, t["muted"]),
            '<path d="M355 177 L366 166 M356 166 H366 V176" fill="none" stroke="' + accent + '" stroke-width="1.5"/>']
    return svg(392, 228, project["title"], " ".join(project["lines"]) + " " + counts, body)


def language_groups(languages):
    ordered = sorted(languages.items(), key=lambda item: (-item[1], item[0]))
    if len(ordered) > 6:
        return ordered[:5] + [("Other", sum(count for _, count in ordered[5:]))]
    return ordered


def languages_card(metrics, theme, mobile=False):
    t = THEMES[theme]
    groups = language_groups(metrics["languages"])
    total = sum(metrics["languages"].values())
    width, height = (392, 254) if mobile else (800, 200)
    body = [rect(1, 1, width - 2, height - 2, t["bg"], t["border"], 14),
            text(24, 35, "THE PUBLIC CODEBASE", 10, t["muted"], 600, 'letter-spacing="1.5"')]
    body.append(text(24 if mobile else 776, 57 if mobile else 35, f"{total} repos with a detected language", 11, t["muted"],
                     extra='' if mobile else 'text-anchor="end"'))
    x = 24.0
    for index, (name, count) in enumerate(groups):
        segment_width = (width - 48) * count / total if total else 0
        body.append(rect(round(x, 3), 76 if mobile else 54, round(segment_width, 3), 11, LANGUAGE_COLORS[index]))
        x += segment_width
        columns = 2 if mobile else 3
        col, row = index % columns, index // columns
        label_x, label_y = 24 + col * (184 if mobile else 254), (118 if mobile else 97) + row * 36
        body += [f'<circle cx="{label_x+4}" cy="{label_y-4}" r="4" fill="{LANGUAGE_COLORS[index]}"/>',
                 text(label_x + 16, label_y, name, 12, t["text"], 500),
                 text(label_x + (159 if mobile else 218), label_y, f"{count / total:.1%}" if total else "0%", 12, t["muted"], extra='text-anchor="end"')]
    if mobile:
        body += [text(24, 224, "Primary language per original public repository.", 10, t["muted"]),
                 text(24, 240, "A view of the code, not a skills rating.", 10, t["muted"])]
    else:
        body.append(text(24, 177, "Primary language per original public repository. A view of the code, not a skills rating.", 10, t["muted"]))
    description = "; ".join(f"{name}: {count} repositories" for name, count in metrics["languages"].items())
    return svg(width, height, "Languages across original public repositories", description, body)


def badge(label, value, theme, width):
    t = THEMES[theme]
    return svg(width, 26, f"{label}: {value}", f"{label}: {value}", [
        rect(1, 1, width - 2, 24, t["panel"], t["border"], 6),
        text(10, 17, label, 10, t["muted"], 500),
        text(width - 10, 17, value, 10, t["purple"], 650, 'text-anchor="end"'),
    ])


def render(metrics):
    output = {"data/metrics.json": json.dumps(metrics, indent=2, ensure_ascii=False) + "\n"}
    for theme in THEMES:
        output[f"art/banner-{theme}.svg"] = banner(theme)
        output[f"art/banner-mobile-{theme}.svg"] = mobile_banner(theme)
        output[f"art/impact-{theme}.svg"] = impact(metrics, theme)
        output[f"art/impact-mobile-{theme}.svg"] = impact(metrics, theme, mobile=True)
        output[f"art/languages-{theme}.svg"] = languages_card(metrics, theme)
        output[f"art/languages-mobile-{theme}.svg"] = languages_card(metrics, theme, mobile=True)
        output[f"art/downloads-{theme}.svg"] = badge("Packagist downloads", compact(metrics["package_downloads"]), theme, 174)
        output[f"art/stars-{theme}.svg"] = badge("GitHub stars", compact(metrics["stars"]), theme, 137)
        for project in PROJECTS:
            output[f"art/project-{project['repo']}-{theme}.svg"] = project_card(project, metrics, theme)
            output[f"art/project-{project['repo']}-compact-{theme}.svg"] = project_card(project, metrics, theme, width=270)
            output[f"art/project-{project['repo']}-narrow-{theme}.svg"] = project_card(project, metrics, theme, width=194)
    readme = (ROOT / "README.md").read_text()
    start, end = "<!-- METRICS:START -->", "<!-- METRICS:END -->"
    if readme.count(start) != 1 or readme.count(end) != 1:
        raise ValueError("README must have exactly one metrics section")
    a, b = readme.index(start), readme.index(end)
    if a >= b:
        raise ValueError("README metrics markers are out of order")
    summary = (
        f"\n<sub>Updated {metrics['updated']} UTC · {metrics['package_downloads']:,} Packagist downloads · "
        f"{metrics['stars']:,} stars · {metrics['forks']:,} forks · "
        f"{metrics['original_repositories']} original public repositories.</sub>\n"
    )
    readme = readme[:a + len(start)] + summary + readme[b:]
    start, end = "<!-- PUBLICATIONS:START -->", "<!-- PUBLICATIONS:END -->"
    if readme.count(start) != 1 or readme.count(end) != 1 or readme.index(start) >= readme.index(end):
        raise ValueError("README must have exactly one ordered publications section")
    counts = metrics["publications"]["counts"]
    breakdown = (
        "\n\n| Distribution | Published listings |\n| :--- | ---: |\n"
        f"| Packagist | {counts['packagist']} |\n"
        f"| npm | {counts['npm']} |\n"
        f"| Homebrew | {counts['homebrew']} |\n"
        f"| GitHub Packages registry | {counts['github_packages']} |\n\n"
        f"These {counts['registry_listings']} registry listings represent {counts['registry_projects']} source projects. "
        f"Another {counts['additional_github_projects']} projects have public GitHub releases, "
        f"for **{counts['unique_projects']} distinct published projects** in total. "
        f"There are {counts['github_release_repositories']} repositories with GitHub releases overall, "
        "including projects already counted in registries and distribution taps.\n\n"
    )
    a, b = readme.index(start), readme.index(end)
    output["README.md"] = readme[:a + len(start)] + breakdown + readme[b:]
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-cache", action="store_true", help="Render the committed snapshot without network access")
    args = parser.parse_args()
    metrics = json.loads((ROOT / "data/metrics.json").read_text()) if args.from_cache else fetch_metrics()
    # Collect and render everything before replacing any existing assets.
    output = render(metrics)
    for name, content in output.items():
        destination = ROOT / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(content)
        temporary.replace(destination)
    print(f"Updated {len(output)} files from the {metrics['updated']} public-data snapshot.")


if __name__ == "__main__":
    main()
