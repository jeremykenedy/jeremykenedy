#!/usr/bin/env python3
"""Refresh public portfolio metrics and render the profile's light/dark artwork."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
OWNER = "jeremykenedy"
API = "https://api.github.com"
PACKAGIST = "https://packagist.org"
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


def fetch_json(url):
    """Keep credentials confined to GitHub; fail instead of publishing partial totals."""
    host = urlparse(url).hostname
    if host not in {"api.github.com", "packagist.org"}:
        raise ValueError(f"Unexpected API host: {host}")
    headers = {"User-Agent": "jeremykenedy-profile (+https://github.com/jeremykenedy/jeremykenedy)"}
    if host == "api.github.com":
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
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


def fetch_metrics():
    metrics = summarize_repositories(fetch_repositories())
    packages = fetch_json(f"{PACKAGIST}/packages/list.json?vendor={OWNER}")["packageNames"]
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
        ("PUBLISHED PACKAGES", len(metrics["packages"]), "In the jeremykenedy namespace"),
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


def project_card(project, metrics, theme):
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
    return svg(392, 212, project["title"], " ".join(project["lines"]) + " " + counts, body)


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
    output["README.md"] = readme[:a + len(start)] + summary + readme[b:]
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
