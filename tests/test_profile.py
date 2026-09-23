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
        self.assertEqual(len(svgs), 28)
        for name, content in svgs.items():
            root = ET.fromstring(content)
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg", name)
            self.assertNotIn("<script", content)
            self.assertNotIn("foreignObject", content)
        self.assertIn("&amp; &lt;examples&gt;", svgs["art/languages-dark.svg"])
        self.assertEqual(output["README.md"].count("<!-- METRICS:START -->"), 1)


if __name__ == "__main__":
    unittest.main()
