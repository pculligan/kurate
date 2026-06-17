import unittest
from pathlib import Path

from phases.extraction.providers.confluence import repo_to_confluence_helpers as helpers


class RepoToConfluenceHelpersTests(unittest.TestCase):
    def test_find_directory_readme_accepts_uppercase_readme(self) -> None:
        root = Path("/tmp/source")
        readme = root / "feedback" / "README.md"
        pages = {
            readme: {"title": "Feedback - Rev 02"},
            root / "feedback" / "notes.md": {"title": "Notes"},
        }

        self.assertEqual(readme, helpers.find_directory_readme(pages, root / "feedback"))

    def test_find_directory_readme_prefers_lowercase_when_both_exist(self) -> None:
        root = Path("/tmp/source")
        lower = root / "feedback" / "readme.md"
        upper = root / "feedback" / "README.md"
        pages = {
            upper: {"title": "Upper"},
            lower: {"title": "Lower"},
        }

        self.assertEqual(lower, helpers.find_directory_readme(pages, root / "feedback"))


if __name__ == "__main__":
    unittest.main()
