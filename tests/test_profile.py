"""Checks for the counts and rendering that are published on the profile."""

from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock
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

    def test_credentials_are_only_sent_to_github_api(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = '{}'
        with patch.object(profile, 'build_opener', return_value=opener), \
                patch.dict(profile.os.environ, {'GITHUB_TOKEN': 'test-token'}):
            for host in ['github.com', 'packagist.org', 'api.npmjs.org']:
                profile.fetch_json(f'https://{host}/test')
                request = opener.open.call_args.args[0]
                self.assertNotIn('Authorization', request.headers)
            profile.fetch_json('https://api.github.com/test')
            self.assertEqual(opener.open.call_args.args[0].headers['Authorization'], 'Bearer test-token')

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
        self.assertEqual(len(svgs), 2 * (36 + len(metrics['showcase']) * 6))
        for name, content in svgs.items():
            root = ET.fromstring(content)
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg", name)
            self.assertNotIn("<script", content)
            self.assertNotIn("foreignObject", content)
        self.assertIn("&amp; &lt;examples&gt;", svgs["art/languages-dark.svg"])
        self.assertEqual(output["README.md"].count("<!-- METRICS:START -->"), 1)

    def test_cards_keep_external_text_safe_and_project_actions_separate(self):
        metrics = json.loads((ROOT / 'data/metrics.json').read_text())
        project = deepcopy(metrics['showcase'][0])
        project['title'] = 'A & <script>test</script>'
        metrics['showcase'] = [project]
        output = profile.render(metrics)
        art = output[f'art/project-{project["name"]}-182-dark.svg']
        self.assertIn('A &amp; &lt;script&gt;test&lt;/script&gt;', art)
        self.assertNotIn('<script>', art)
        self.assertIn(project['url'] + '/stargazers', output['README.md'])
        self.assertIn(project['url'] + '?sponsor=1', output['README.md'])

    def test_artwork_versions_change_with_content_and_preserve_theme_fragments(self):
        markup = '<img src="art/example.svg#gh-dark-mode-only">'
        first = profile.version_artwork(markup, {'art/example.svg': 'first'})
        second = profile.version_artwork(first, {'art/example.svg': 'second'})
        self.assertNotEqual(first, second)
        self.assertIn('#gh-dark-mode-only', second)
        self.assertEqual(second, profile.version_artwork(second, {'art/example.svg': 'second'}))
        self.assertNotIn('?v=', second)


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.today = datetime(2026, 10, 9, tzinfo=timezone.utc)
        self.config = {'stale_after_years': 5, 'legacy_star_threshold': 100, 'excluded': {'detached-fork': 'Upstream origin'}}

    def repository(self, name='example', **overrides):
        return {'name': name, 'isPrivate': False, 'isFork': False, 'isArchived': False, 'isEmpty': False,
                'stargazerCount': 1, 'licenseInfo': {'spdxId': 'MIT'},
                'defaultBranchRef': {'target': {'history': {'nodes': [
                    {'messageHeadline': 'Add funding options', 'committedDate': '2026-10-08T00:00:00Z'},
                    {'messageHeadline': 'Improve application behavior', 'committedDate': '2026-10-01T00:00:00Z'},
                ]}}}, **overrides}

    def test_showcase_excludes_private_forks_archived_unlicensed_and_known_derivatives(self):
        repos = [self.repository(), self.repository('private', isPrivate=True), self.repository('fork', isFork=True),
                 self.repository('archived', isArchived=True), self.repository('unlicensed', licenseInfo=None),
                 self.repository('empty', isEmpty=True), self.repository('detached-fork')]
        selected = profile.portfolio.select_projects(repos, self.config, self.today)
        self.assertEqual([r['name'] for r in selected], ['example'])
        self.assertEqual(selected[0]['activity_date'], '2026-10-01T00:00:00Z')

    def test_funding_only_updates_do_not_revive_stale_projects_but_adoption_can_keep_them(self):
        repo = self.repository()
        repo['defaultBranchRef']['target']['history']['nodes'][1]['committedDate'] = '2016-10-01T00:00:00Z'
        popular = {**repo, 'name': 'popular', 'stargazerCount': 100}
        selected = profile.portfolio.select_projects([repo, popular], self.config, self.today)
        self.assertEqual([r['name'] for r in selected], ['popular'])

    def test_only_recent_flagships_are_promoted_then_activity_sets_order(self):
        projects = [{'name': name, 'updated': date + 'T00:00:00Z', 'stars': 0} for name, date in [
            ('old-flagship', '2018-01-01'), ('recent', '2026-10-08'), ('flagship', '2026-10-01'), ('older', '2025-01-01')]]
        ordered = profile.portfolio.order_projects(projects, ['old-flagship', 'flagship'], self.today)
        self.assertEqual([p['name'] for p in ordered], ['flagship', 'recent', 'older', 'old-flagship'])

    def test_ten_or_more_stars_lead_in_descending_order_without_reordering_lower_star_projects(self):
        projects = [{'name': name, 'stars': stars, 'updated': date + 'T00:00:00Z'} for name, stars, date in [
            ('flagship', 1, '2026-10-01'), ('nine-stars', 9, '2026-10-08'),
            ('ten-stars', 10, '2026-10-07'), ('most-stars', 1000, '2018-01-01'),
            ('middle', 100, '2026-10-02'), ('older-low', 5, '2025-01-01')]]
        ordered = profile.portfolio.order_projects(projects, ['flagship'], self.today)
        self.assertEqual([p['name'] for p in ordered], [
            'most-stars', 'middle', 'ten-stars', 'flagship', 'nine-stars', 'older-low'])

    def test_public_contributions_deduplicate_and_preserve_merge_status(self):
        def pr(identifier, repository='upstream/project', state='open', merged=None):
            return {'id': identifier, 'repository_url': 'https://api.github.com/repos/' + repository,
                    'state': state, 'pull_request': {'merged_at': merged}, 'title': 'Fix behavior',
                    'html_url': f'https://github.com/{repository}/pull/{identifier}', 'updated_at': '2026-10-09'}
        merged = pr(1, state='closed', merged='2026-10-09')
        result = profile.portfolio.summarize_contributions([
            merged, merged, pr(2), pr(3, state='closed'), pr(4, 'jeremykenedy/own'), pr(5, 'excluded/project')],
            ['excluded/project'])
        self.assertEqual((result['pull_requests'], result['repositories'], result['merged'], result['open']), (3, 1, 1, 1))
        self.assertEqual(result['projects'][0]['closed'], 1)

    def test_contribution_search_explicitly_limits_visibility_and_rejects_partial_results(self):
        def fetch(url):
            self.assertIn('is%3Apublic', url)
            return {'incomplete_results': True, 'total_count': 1, 'items': []}
        with self.assertRaises(ValueError):
            profile.portfolio.fetch_contributions(fetch, [])

    def test_npm_badge_does_not_claim_limited_history_is_lifetime_downloads(self):
        def fetch(url):
            self.assertTrue(url.endswith('/example'))
            return {'downloads': [{'downloads': 12}, {'downloads': 8}], 'start': '2025-04-09', 'end': '2026-10-08'}
        result = profile.portfolio.npm_downloads(fetch, 'https://img.shields.io/npm/dt/example.svg')
        self.assertEqual(result['count'], 20)
        self.assertEqual(result['period'], '18mo')


if __name__ == "__main__":
    unittest.main()
