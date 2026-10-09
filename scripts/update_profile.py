#!/usr/bin/env python3
"""Refresh public portfolio metrics and render the profile's light/dark artwork."""

import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import textwrap
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portfolio


ROOT = Path(__file__).resolve().parents[1]
OWNER = "jeremykenedy"
API = "https://api.github.com"
PACKAGIST = "https://packagist.org"
NPM = "https://registry.npmjs.org"
NPM_PUBLISHERS = ("developernator", "jeremykenedy")
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
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
    if parsed.scheme != "https" or host not in {"api.github.com", "github.com", "packagist.org", "registry.npmjs.org", "api.npmjs.org"}:
        raise ValueError(f"Unexpected API URL: {url}")
    headers = {"User-Agent": "jeremykenedy-profile (+https://github.com/jeremykenedy/jeremykenedy)"}
    if host == "github.com":
        headers["Accept"] = "application/json"
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
    metrics.update(portfolio.collect(fetch_json, json.loads((ROOT / "data/showcase.json").read_text()), metrics))
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


def world_graphic(theme, x=568, y=34, scale=1):
    t = THEMES[theme]
    body = [f'<g transform="translate({x} {y}) scale({scale})">',
            '<style>@keyframes routes{to{stroke-dashoffset:-100}}'
            '.route-motion{animation:routes 16s linear infinite}'
            '@media(prefers-reduced-motion:reduce){.route-motion{animation:none;stroke-dasharray:none;opacity:.3}}</style>',
            f'<ellipse cx="96" cy="67" rx="92" ry="59" fill="{t["panel"]}" stroke="{t["border"]}"/>']
    for rx, ry in [(91, 26), (44, 59), (72, 59)]:
        body.append(f'<ellipse cx="96" cy="67" rx="{rx}" ry="{ry}" fill="none" stroke="{t["border"]}" stroke-width=".7"/>')
    continents = [
        'M22 39L34 25L53 23L62 34L75 33L68 46L54 48L48 59L40 61L34 51L25 49Z',
        'M49 64L63 71L73 83L67 96L63 109L56 114L52 99L48 88L43 76Z',
        'M76 19L84 15L90 22L85 30L79 29Z',
        'M95 34L103 28L112 31L115 41L106 46L98 42L91 45Z',
        'M94 51L111 49L126 66L119 84L109 103L101 96L99 79L88 63Z',
        'M114 33L127 23L151 26L172 40L171 54L157 60L150 74L139 67L134 52L120 47Z',
        'M144 78L159 82L165 86L157 88Z',
        'M153 98L170 94L181 104L172 114L156 110Z',
    ]
    for path in continents:
        body.append(f'<path d="{path}" fill="{t["blue"]}" fill-opacity=".14" stroke="{t["blue"]}" stroke-opacity=".55" stroke-width=".7"/>')
    destinations = [(101, 37, 67, 3), (167, 47, 104, -3), (64, 98, 23, 83),
                    (111, 94, 75, 49), (153, 77, 106, 22), (167, 104, 109, 42)]
    for index, (end_x, end_y, control_x, control_y) in enumerate(destinations):
        path = f'M28 43 Q{control_x} {control_y} {end_x} {end_y}'
        body += [f'<path d="{path}" fill="none" stroke="{t["purple"]}" stroke-opacity=".3"/>',
                 f'<path class="route-motion" d="{path}" pathLength="100" fill="none" stroke="{t["purple"]}" stroke-width="1.8" stroke-linecap="round" stroke-dasharray="1 24" style="animation-delay:-{index * 2}s"/>',
                 f'<circle cx="{end_x}" cy="{end_y}" r="2.8" fill="{t["bg"]}" stroke="{t["blue"]}" stroke-width="1.2"/>']
    body += [f'<circle cx="28" cy="43" r="6" fill="{t["purple"]}" fill-opacity=".16"/>',
             f'<circle cx="28" cy="43" r="3" fill="{t["purple"]}"/>',
             text(7, 32, 'Portland', 8, t['text'], 600), '</g>']
    return body


def banner_pills(theme, x, y, mobile=False):
    t = THEMES[theme]
    size, gap = (8.2, 8) if mobile else (10, 10)
    widths = (111, 132, 93) if mobile else (130, 150, 114)
    body = []
    for label, width, color, light_fill in zip(
            ['TEAM LEADERSHIP', 'SYSTEM ARCHITECTURE', 'OPEN SOURCE'], widths,
            [t['purple'], t['blue'], '#4ade80' if theme == 'dark' else '#166534'],
            ['#f5f3ff', '#eff6ff', '#f0fdf4']):
        body += [rect(x, y, width, 26, '#1a1a1a' if theme == 'dark' else light_fill, color, 13, 'stroke-width=".7"'),
                 text(x + width / 2, y + 17, label, size, color, 600, 'text-anchor="middle"')]
        x += width + gap
    return body


def banner(theme):
    t = THEMES[theme]
    background = 'url(#background)' if theme == 'dark' else '#fff'
    body = ['<defs><linearGradient id="background" x2="1" y2="1"><stop stop-color="#0c0a09"/><stop offset="1" stop-color="#1c1917"/></linearGradient></defs>',
            rect(1, 1, 798, 198, background, t['border'], 16),
            rect(32, 32, 24, 3, t['purple'], radius=1),
            text(68, 38, 'PEOPLE. PLATFORMS. PRODUCTS.', 10, t['muted'], 600, 'letter-spacing="1.5"'),
            text(31, 82, 'Jeremy Kenedy', 40, t['text'], 700, 'letter-spacing="-.5"'),
            text(32, 111, 'Engineering leader. Architect. Hands-on builder.', 14, t['muted']),
            *banner_pills(theme, 32, 130),
            text(32, 180, 'PORTLAND, OR  /  BUILDING WITH PURPOSE', 9, t['muted'], 500, 'letter-spacing="1.2"'),
            *world_graphic(theme)]
    return svg(800, 200, 'Jeremy Kenedy', 'Engineering leader. Architect. Hands-on builder. Illustrated routes from Portland to the world.', body)


def mobile_banner(theme):
    t = THEMES[theme]
    body = [rect(1, 1, 390, 340, t['bg'], t['border'], 16),
            rect(20, 24, 24, 3, t['purple'], radius=1),
            text(54, 29, 'PEOPLE. PLATFORMS. PRODUCTS.', 9, t['muted'], 600, 'letter-spacing="1.2"'),
            text(19, 76, 'Jeremy Kenedy', 36, t['text'], 700, 'letter-spacing="-.5"'),
            text(20, 104, 'Engineering leader. Architect. Hands-on builder.', 12.2, t['muted']),
            *banner_pills(theme, 20, 125, mobile=True),
            *world_graphic(theme, 88, 165, 1.12),
            text(196, 319, 'PORTLAND, OR  /  BUILDING WITH PURPOSE', 9, t['muted'], 500, 'text-anchor="middle" letter-spacing=".9"')]
    return svg(392, 342, 'Jeremy Kenedy', 'Engineering leader. Architect. Hands-on builder. Illustrated routes from Portland to the world.', body)


def impact(metrics, theme, mobile=False):
    t = THEMES[theme]
    values = [
        ('Package downloads', compact(metrics['package_downloads']), 'Across Packagist packages'),
        ('GitHub stars', compact(metrics['stars']), 'Public source repositories'),
        ('Community forks', compact(metrics['forks']), 'Of public source repositories'),
        ('Published projects', metrics['publications']['counts']['unique_projects'], 'Packages, apps, and tools'),
    ]
    width, height = (392, 246) if mobile else (800, 143)
    body = [rect(1, 1, width - 2, height - 2, t['bg'], t['border'], 14)]
    for index, (label, value, note) in enumerate(values):
        x = (20 + index % 2 * 190) if mobile else (24 + index * 198)
        y = index // 2 * 116 if mobile else 0
        if not mobile and index:
            body.append(f'<path d="M{x-13} 27 V115" stroke="{t["border"]}"/>')
        body += [text(x, y + 33, label, 11, t['muted'], 500),
                 text(x, y + 79, value, 36, t['purple'] if index % 2 == 0 else t['blue'], 700),
                 text(x, y + 104, note, 8.5, t['muted'])]
    return svg(width, height, 'Open source adoption', '; '.join(f'{label}: {value}' for label, value, _ in values), body)


def wrapped_text(value, x, y, width, size, color, max_lines, line_height, weight=400):
    max_word = max((len(word) for word in value.split()), default=1)
    fitted = min(size, width / (max_word * .65))
    lines = textwrap.wrap(value, max(1, int(width / (fitted * .53))), break_long_words=False,
                          max_lines=max_lines, placeholder='...')
    return [text(x, y + index * line_height, line, round(fitted, 2), color, weight)
            for index, line in enumerate(lines)]


def card_pill(label, x, y, width, theme, color=None):
    t = THEMES[theme]
    color = color or t['purple']
    size = min(9.5, (width - 10) / (len(label) * .55))
    return [rect(x, y, width, 20, t['panel'], t['border'], 5),
            text(x + width / 2, y + 13.5, label, round(size, 2), color, 500, 'text-anchor="middle"')]


def project_card(project, metrics, theme, width=182):
    t = THEMES[theme]
    narrow = width < 120
    height, start = (422, 206) if narrow else (386, 170)
    margin = 7 if narrow else 10
    usable = width - margin * 2
    body = [rect(0, 0, width, height, t['bg'], radius=8),
            text(margin, 16, project['category'], min(8.5, usable / (len(project['category']) * .54)), t['blue'], 600),
            *wrapped_text(project['title'], margin, 41, usable, 14 if narrow else 17, t['text'], 3 if narrow else 2, 18 if narrow else 21, 700),
            *wrapped_text(project['description'], margin, 101 if narrow else 92, usable, 10.2 if narrow else 11,
                          t['muted'], 5 if narrow else 3, 13),
            *wrapped_text(project['stack'], margin, start - 34, usable, 8.5, t['purple'], 2, 11, 500)]
    rows = [('Stars', compact(project['stars'])), ('Contributors', project['contributors']),
            ('Releases', project['releases']), ('Commits', compact(project['commits']))]
    if project['used_by'] is not None:
        rows.append(('Used by', compact(project['used_by'])))
    for index, (label, value) in enumerate(rows):
        if value is not None:
            body += [text(margin, start + index * 17, label, 9.5, t['muted']),
                     text(width - margin, start + index * 17, value, 10.5, t['text'], 600, 'text-anchor="end"')]
    latest = project['latest_release']
    body += [text(margin, start + 88, 'Latest ' + (latest['tag'] if latest else 'unreleased'), min(8.5, usable / (len('Latest ' + (latest['tag'] if latest else 'unreleased')) * .57)), t['text'], 500),
             text(margin, start + 104, 'Updated ' + project['updated'][:10], 8, t['muted']),
             *card_pill(project['license'] + ' license', margin, start + 116, usable, theme)]
    if project['downloads']:
        label = compact(project['downloads']['count']) + ' downloads'
        if project['downloads'].get('period'):
            label += '/' + project['downloads']['period']
        body.extend(card_pill(label, margin, start + 141, usable, theme, t['blue']))
    if project['made_with_laravel']:
        body.extend(card_pill('Made with Laravel', margin, start + 166, usable, theme, '#f87171' if theme == 'dark' else '#b91c1c'))
    body.append(text(margin, height - 8, 'Free and open source', min(8, usable / 10.5), t['muted']))
    description = f"{project['title']}. {project['description']} {project['stack']}. "
    description += f"{project['stars']} stars; {project['contributors']} contributors; {project['releases']} releases; {project['commits']} commits. {project['license']} license."
    return svg(width, height, project['title'], description, body)


def action_badge(label, theme, width=100, height=28):
    t = THEMES[theme]
    return svg(width, height, label, label, [
        rect(1, 1, width - 2, height - 2, t['panel'], t['border'], 6),
        text(width / 2, height / 2 + 3.5, label, 10.5 if width > 75 else 9, t['blue'], 600, 'text-anchor="middle"'),
    ])


CARD_SIZES = [(1280, 182, 182), (1216, 230, 182), (1152, 210, 182), (1012, 164, 144),
              (896, 146, 144), (820, 120, 100), (768, 104, 100), (680, 270, 182),
              (640, 250, 182), (560, 210, 182), (480, 170, 144), (430, 144, 144),
              (390, 124, 100), (360, 110, 100)]


def themed_image_link(target, stem, alt, width, height=None, responsive=False):
    links = []
    for theme in THEMES:
        fragment = f'#gh-{theme}-mode-only'
        sources = ''
        if responsive:
            for breakpoint, display_width, asset_width in CARD_SIZES:
                sources += (f'<source media="(min-width: {breakpoint}px)" '
                            f'srcset="art/{stem}-{asset_width}-{theme}.svg" width="{display_width}">')
        suffix = '-100' if responsive else ''
        height_attr = f' height="{height}"' if height else ''
        links.append(f'<a href="{escape(target, quote=True)}{fragment}"><picture>{sources}'
                     f'<img src="art/{stem}{suffix}-{theme}.svg{fragment}" alt="{escape(alt, quote=True)}" '
                     f'width="{width}"{height_attr}></picture></a>')
    return ''.join(links)


def showcase_markup(metrics):
    cards = []
    for project in metrics['showcase']:
        card = themed_image_link(project['url'], 'project-' + project['name'],
                                 f"{project['title']}: {project['description']}", 90, responsive=True)
        star = themed_image_link(project['url'] + '/stargazers', 'action-star', 'Star ' + project['name'], 36, 26)
        sponsor = themed_image_link(project['url'] + '?sponsor=1', 'action-sponsor', 'Sponsor ' + project['name'], 50, 26) if project['funding'] else ''
        cards.append(f'<table align="left"><tr><td align="center">{card}<br>{star}{sponsor}</td></tr></table>')
    return '\n\n' + '\n'.join(cards) + '\n<br clear="all">\n\n'


def contribution_markup(metrics):
    data = metrics['contributions']
    lines = [f"\n\n**{data['pull_requests']} public pull requests across {data['repositories']} other projects. "
             f"{data['merged']} merged.** These figures do not include private projects or private contributions.\n",
             f"**{metrics['public_authored_commits']:,} public authored commits indexed by GitHub.** "
             "This covers indexed default-branch history, not private work.\n",
             '<details>\n<summary>Explore the full public contribution history</summary>\n',
             '| Project | Merged | Open | Example pull request |', '| :--- | ---: | ---: | :--- |']
    for project in data['projects']:
        pull = next((item for item in project['pull_requests'] if item['state'] == 'merged'), project['pull_requests'][0])
        title = escape(pull['title']).replace('|', '\\|').replace('\n', ' ').replace('[', '\\[').replace(']', '\\]')
        lines.append(f"| [{project['name']}]({project['url']}) | {project['merged']} | {project['open']} | "
                     f"[{title}]({pull['url']}) ({pull['state']}) |")
    lines += ['', '</details>', '']
    return '\n'.join(lines)


def support_markup():
    links = [('Follow Jeremy', 'follow', 'https://github.com/jeremykenedy', 120),
             ('GitHub Sponsors', 'github', 'https://github.com/sponsors/jeremykenedy', 134),
             ('Patreon', 'patreon', 'https://patreon.com/jeremykenedy', 82),
             ('Ko-fi', 'kofi', 'https://ko-fi.com/jeremykenedy', 64),
             ('PayPal', 'paypal', 'https://www.paypal.com/paypalme/jeremykenedy', 76),
             ('Buy Me a Coffee', 'coffee', 'https://www.buymeacoffee.com/jeremykenedy', 132)]
    markup = '<p align="center">\n' + '\n'.join(
        themed_image_link(url, 'support-' + name, label, width, 28) for label, name, url, width in links) + '\n</p>'
    return markup, links


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
        body += [text(24, 224, "Primary language per public source repository.", 10, t["muted"]),
                 text(24, 240, "A view of the code, not a skills rating.", 10, t["muted"])]
    else:
        body.append(text(24, 177, "Primary language per public source repository. A view of the code, not a skills rating.", 10, t["muted"]))
    description = "; ".join(f"{name}: {count} repositories" for name, count in metrics["languages"].items())
    return svg(width, height, "Languages across public source repositories", description, body)


def badge(label, value, theme, width):
    t = THEMES[theme]
    return svg(width, 26, f"{label}: {value}", f"{label}: {value}", [
        rect(1, 1, width - 2, 24, t["panel"], t["border"], 6),
        text(10, 17, label, 10, t["muted"], 500),
        text(width - 10, 17, value, 10, t["purple"], 650, 'text-anchor="end"'),
    ])


def version_artwork(readme, output):
    def reference(match):
        name = re.sub(r'\.[a-f0-9]{12}(?=\.svg$)', '', match.group(1))
        digest = sha256(output[name].encode()).hexdigest()[:12]
        versioned = name.removesuffix('.svg') + f'.{digest}.svg'
        output[versioned] = output[name]
        return versioned
    return re.sub(r'(art/[^"?#\s]+\.svg)(?:\?v=[a-f0-9]+)?', reference, readme)


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
        for project in metrics["showcase"]:
            for width in (100, 144, 182):
                output[f"art/project-{project['name']}-{width}-{theme}.svg"] = project_card(project, metrics, theme, width)
        output[f"art/action-star-{theme}.svg"] = action_badge("Star", theme, 36, 26)
        output[f"art/action-sponsor-{theme}.svg"] = action_badge("Sponsor", theme, 50, 26)
        for label, name, _, width in support_markup()[1]:
            output[f"art/support-{name}-{theme}.svg"] = action_badge(label, theme, width)
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
        f"{metrics['original_repositories']} public source repositories.</sub>\n"
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
    readme = readme[:a + len(start)] + breakdown + readme[b:]
    for name, content in [("SHOWCASE", showcase_markup(metrics)), ("CONTRIBUTIONS", contribution_markup(metrics)),
                          ("SUPPORT", "\n" + support_markup()[0] + "\n")]:
        start, end = f"<!-- {name}:START -->", f"<!-- {name}:END -->"
        if readme.count(start) != 1 or readme.count(end) != 1 or readme.index(start) >= readme.index(end):
            raise ValueError(f"README must have one ordered {name} section")
        a, b = readme.index(start), readme.index(end)
        readme = readme[:a + len(start)] + content + readme[b:]
    output["README.md"] = version_artwork(readme, output)
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
    for obsolete in (ROOT / "art").glob("*.svg"):
        generated = obsolete.name.startswith('project-') or re.search(r'\.[a-f0-9]{12}\.svg$', obsolete.name)
        if generated and str(obsolete.relative_to(ROOT)) not in output:
            obsolete.unlink()
    print(f"Updated {len(output)} files from the {metrics['updated']} public-data snapshot.")


if __name__ == "__main__":
    main()
