"""Checks for the counts and rendering that are published on the profile."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("profile", ROOT / "scripts/update_profile.py")
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


class ProfileTests(unittest.TestCase):
    def repository(self, **overrides):
        return {"id": 1, "name": "example", "owner": {"login": "jeremykenedy"}, "private": False,
                "fork": False, "stargazers_count": 7, "forks_count": 3, "language": "Python", **overrides}

    def test_totals_exclude_forks_private_and_other_owners_and_deduplicate(self):
        original = self.repository()
        summary = profile.summarize_repositories([
            original, original, self.repository(id=2, fork=True, stargazers_count=1000),
            self.repository(id=3, private=True), self.repository(id=4, owner={"login": "someone-else"}),
            self.repository(id=5, archived=True, language=None),
        ])
        self.assertEqual(summary["original_repositories"], 2)
        self.assertEqual(summary["stars"], 14)
        self.assertEqual(summary["forks"], 6)
        self.assertEqual(summary["languages"], {"Python": 1})

    def test_pagination_fetches_beyond_first_hundred(self):
        with patch.object(profile, "fetch_json", side_effect=[[self.repository()] * 100, [self.repository(id=101)]]) as fetch:
            self.assertEqual(len(profile.fetch_repositories()), 101)
            self.assertIn("page=2", fetch.call_args.args[0])

    def test_small_languages_are_included_in_other(self):
        counts = {f"language-{i}": i for i in range(1, 14)}
        groups = profile.language_groups(counts)
        self.assertEqual(len(groups), 6)
        self.assertEqual(sum(count for _, count in groups), sum(counts.values()))
        self.assertEqual(groups[-1][0], "Other")

    def test_source_url_variants_identify_the_same_project(self):
        for url in ["https://github.com/JeremyKenedy/Example", "git+https://github.com/jeremykenedy/example.git",
                    "git@github.com:jeremykenedy/example.git", "https://github.com/jeremykenedy/example/"]:
            self.assertEqual(profile.repository_identity(url), "jeremykenedy/example")
        with self.assertRaises(ValueError):
            profile.repository_identity("https://example.com/not-a-github-project")

    def test_api_requests_reject_untrusted_schemes_and_hosts_before_network_access(self):
        with patch.object(profile, "build_opener") as opener:
            for url in ["file:///etc/passwd", "http://api.github.com/user", "https://example.com/data"]:
                with self.assertRaises(ValueError):
                    profile.fetch_json(url)
            opener.assert_not_called()

    def test_api_redirects_cannot_forward_credentials(self):
        request = profile.Request("https://api.github.com/user", headers={"Authorization": "Bearer test-token"})
        redirect = profile.NoRedirects().redirect_request(request, None, 302, "Found", {}, "https://example.com")
        self.assertIsNone(redirect)

    def test_publications_deduplicate_distributions_and_skip_tap_as_extra_project(self):
        def entry(channel, name, repository):
            return {"channel": channel, "name": name, "repository": f"jeremykenedy/{repository}"}

        package = entry("packagist", "jeremykenedy/library", "library")
        audit = profile.summarize_publications([
            package, package, entry("npm", "library-js", "library"),
            entry("github_release", "library", "library"),
            entry("packagist", "jeremykenedy/registry-only", "registry-only"),
            entry("homebrew", "homebrew-tool/Formula/tool.rb", "tool"),
            entry("github_release", "homebrew-tool", "homebrew-tool"),
            entry("github_release", "tool", "tool"),
            entry("github_release", "firmware", "firmware"),
        ])
        self.assertEqual(audit["counts"], {
            "packagist": 2, "npm": 1, "homebrew": 1, "github_release_repositories": 4,
            "github_packages": 0, "registry_listings": 4, "registry_projects": 3,
            "additional_github_projects": 1, "unique_projects": 4,
        })
        self.assertEqual(len(audit["projects"]), 4)
        self.assertNotIn("jeremykenedy/homebrew-tool", [item["repository"] for item in audit["projects"]])

    def test_publication_audit_includes_prereleases_but_not_drafts_private_or_forks(self):
        repo = self.repository(html_url="https://github.com/jeremykenedy/example")

        def response(url, payload=None):
            if "/releases?" in url:
                self.assertIn("/example/releases?", url)
                return [
                    {"draft": True, "published_at": None},
                    {"draft": False, "prerelease": True, "published_at": "2026-09-23T00:00:00Z",
                     "html_url": "https://github.com/jeremykenedy/example/releases/tag/v1-beta"},
                ]
            if "/-/v1/search?" in url:
                return {"objects": [], "total": 0}
            if url.endswith("/graphql"):
                return {"data": {"user": {"packages": {"totalCount": 0}}}}
            self.fail(f"Unexpected endpoint: {url}")

        with patch.object(profile, "fetch_json", side_effect=response):
            audit = profile.fetch_publications([repo, self.repository(private=True), self.repository(fork=True)], {})
        self.assertEqual(audit["counts"]["unique_projects"], 1)
        self.assertEqual(audit["counts"]["github_release_repositories"], 1)

    def test_partial_api_failure_does_not_write_any_assets(self):
        with patch.object(profile, "fetch_metrics", side_effect=RuntimeError("upstream unavailable")), \
                patch("sys.argv", ["update_profile.py"]), patch.object(Path, "write_text") as write:
            with self.assertRaises(RuntimeError):
                profile.main()
            write.assert_not_called()

    def test_rendered_assets_are_valid_svg_and_escape_upstream_values(self):
        metrics = json.loads((ROOT / "data/metrics.json").read_text())
        metrics = deepcopy(metrics)
        metrics["languages"]['C++ & <examples>'] = 500
        output = profile.render(metrics)
        svgs = {name: content for name, content in output.items() if name.endswith(".svg")}
        self.assertEqual(len(svgs), 52)
        for name, content in svgs.items():
            root = ET.fromstring(content)
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg", name)
            self.assertNotIn("<script", content)
            self.assertNotIn("foreignObject", content)
        self.assertIn("&amp; &lt;examples&gt;", svgs["art/languages-dark.svg"])
        self.assertEqual(output["README.md"].count("<!-- METRICS:START -->"), 1)


if __name__ == "__main__":
    unittest.main()
