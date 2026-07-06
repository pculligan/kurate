import tempfile
import unittest
import json
from pathlib import Path

from phases.extraction.providers.confluence import confluence_to_repo_helpers as helpers


class FakeClient:
    def __init__(self) -> None:
        self.base_url = "https://example.atlassian.net"
        self.downloads: list[tuple[str, Path]] = []
        self.pages: dict[str, dict] = {}
        self.folders: dict[str, dict] = {}
        self.direct_children_by_node: dict[tuple[str, str], list[dict]] = {}
        self.pages_by_title: dict[tuple[str, str], dict] = {}
        self.attachments_by_page: dict[str, list[dict]] = {}

    def download_attachment(self, download_path: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"downloaded from {download_path}", encoding="utf-8")
        self.downloads.append((download_path, destination))

    def get_page(self, page_id: str, expand: str = "body.storage,version,ancestors") -> dict:
        if page_id not in self.pages:
            raise helpers.requests.HTTPError(f"page not found: {page_id}")
        return self.pages[page_id]

    def get_folder(self, folder_id: str) -> dict:
        if folder_id not in self.folders:
            raise helpers.requests.HTTPError(f"folder not found: {folder_id}")
        return self.folders[folder_id]

    def get_direct_children(self, content_id: str, content_type: str) -> list[dict]:
        return self.direct_children_by_node.get((content_id, content_type), [])

    def find_page(self, space: str, title: str) -> dict | None:
        return self.pages_by_title.get((space, title))

    def get_attachments(self, page_id: str) -> list[dict]:
        return self.attachments_by_page.get(page_id, [])


class ConfluenceToRepoHelpersTests(unittest.TestCase):
    def make_context(self) -> dict:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        output_dir = Path(tempdir.name)
        report = {
            "output": str(output_dir),
            "downloaded_attachments": [],
            "attachment_cache_hits": [],
            "unsupported_content": [],
            "unresolved_links": [],
            "warnings": [],
        }
        page = {
            "id": "123",
            "title": "Sample Page",
            "markdown_path": output_dir / "readme.md",
            "space_key": "IDD",
            "attachments": [],
            "cached_attachments_by_filename": {},
            "report": report,
        }
        return {
            "client": FakeClient(),
            "page": page,
            "pages_by_id": {"123": page},
            "title_to_id": {"Sample Page": "123"},
            "base_url": "https://example.atlassian.net",
            "report": report,
        }

    def test_emoticon_renders_without_registering_unsupported_content(self) -> None:
        context = self.make_context()
        markdown = helpers.storage_to_markdown(
            '<p>Hello <ac:emoticon ac:emoji-fallback="🎯" ac:name="dart"></ac:emoticon></p>',
            context,
        )
        self.assertIn("Hello 🎯", markdown)
        self.assertEqual([], context["report"]["unsupported_content"])

    def test_inline_comment_marker_keeps_wrapped_text(self) -> None:
        context = self.make_context()
        markdown = helpers.storage_to_markdown(
            '<p><ac:inline-comment-marker ac:ref="comment-1">Customer</ac:inline-comment-marker> ID</p>',
            context,
        )
        self.assertIn("Customer ID", markdown)
        self.assertEqual([], context["report"]["unsupported_content"])

    def test_warning_tip_and_excerpt_render_as_labeled_blocks(self) -> None:
        context = self.make_context()
        markdown = helpers.storage_to_markdown(
            """
            <ac:structured-macro ac:name="warning"><ac:rich-text-body><p>Out of scope</p></ac:rich-text-body></ac:structured-macro>
            <ac:structured-macro ac:name="tip"><ac:rich-text-body><p>Do this first</p></ac:rich-text-body></ac:structured-macro>
            <ac:structured-macro ac:name="excerpt"><ac:rich-text-body><p>Reusable summary</p></ac:rich-text-body></ac:structured-macro>
            """,
            context,
        )
        self.assertIn("**Warning:** Out of scope", markdown)
        self.assertIn("**Tip:** Do this first", markdown)
        self.assertIn("**Excerpt:** Reusable summary", markdown)
        self.assertEqual([], context["report"]["unsupported_content"])

    def test_expand_plain_text_renders_as_labeled_code_block(self) -> None:
        context = self.make_context()
        markdown = helpers.storage_to_markdown(
            """
            <ac:structured-macro ac:name="expand">
              <ac:parameter ac:name="title">Sample Payload</ac:parameter>
              <ac:plain-text-body><![CDATA[{ "hello": "world" }]]></ac:plain-text-body>
            </ac:structured-macro>
            """,
            context,
        )
        self.assertIn("**Expand: Sample Payload:**", markdown)
        self.assertIn("```json", markdown)
        self.assertIn('{ "hello": "world" }', markdown)
        self.assertEqual([], context["report"]["unsupported_content"])

    def test_attachments_macro_downloads_and_links_page_attachments(self) -> None:
        context = self.make_context()
        context["page"]["attachments"] = [
            {
                "filename": "diagram.png",
                "download": "/download/attachments/123/diagram.png",
                "id": "att-1",
                "version": 1,
                "local_filename": "diagram.png",
            }
        ]
        markdown = helpers.storage_to_markdown(
            '<ac:structured-macro ac:name="attachments"></ac:structured-macro>',
            context,
        )
        self.assertIn("**Attachments:**", markdown)
        self.assertIn("- [diagram.png](diagram.png)", markdown)
        self.assertTrue((Path(context["report"]["output"]) / "diagram.png").exists())
        self.assertEqual([], context["report"]["unsupported_content"])

    def test_excerpt_include_renders_excerpt_from_loaded_page(self) -> None:
        context = self.make_context()
        context["pages_by_id"]["456"] = {
            "id": "456",
            "title": "Source Page",
            "body_storage": (
                '<ac:structured-macro ac:name="excerpt">'
                '<ac:rich-text-body><p>Shared excerpt body</p></ac:rich-text-body>'
                "</ac:structured-macro>"
            ),
        }
        context["title_to_id"]["Source Page"] = "456"
        markdown = helpers.storage_to_markdown(
            (
                '<ac:structured-macro ac:name="excerpt-include">'
                '<ac:link><ri:page ri:content-id="456" /></ac:link>'
                "</ac:structured-macro>"
            ),
            context,
        )
        self.assertIn("Shared excerpt body", markdown)
        self.assertEqual([], context["report"]["unsupported_content"])

    def test_excerpt_include_can_fetch_source_page(self) -> None:
        context = self.make_context()
        context["client"].pages["789"] = {
            "id": "789",
            "title": "Fetched Source Page",
            "body": {
                "storage": {
                    "value": (
                        '<ac:structured-macro ac:name="excerpt">'
                        '<ac:rich-text-body><p>Fetched excerpt body</p></ac:rich-text-body>'
                        "</ac:structured-macro>"
                    )
                }
            },
        }
        markdown = helpers.storage_to_markdown(
            (
                '<ac:structured-macro ac:name="excerpt-include">'
                '<ac:link><ri:page ri:content-id="789" /></ac:link>'
                "</ac:structured-macro>"
            ),
            context,
        )
        self.assertIn("Fetched excerpt body", markdown)
        self.assertEqual([], context["report"]["unsupported_content"])

    def test_excerpt_include_can_resolve_title_and_named_excerpt(self) -> None:
        context = self.make_context()
        context["client"].pages_by_title[("IDD", "Diagrams")] = {
            "id": "999",
            "title": "Diagrams",
            "space": {"key": "IDD"},
            "body": {
                "storage": {
                    "value": (
                        '<ac:structured-macro ac:name="excerpt">'
                        '<ac:parameter ac:name="name">Other Diagram</ac:parameter>'
                        '<ac:rich-text-body><p>Wrong excerpt</p></ac:rich-text-body>'
                        "</ac:structured-macro>"
                        '<ac:structured-macro ac:name="excerpt">'
                        '<ac:parameter ac:name="name">CMP Compliance Diagram</ac:parameter>'
                        '<ac:rich-text-body><p>Resolved named excerpt</p></ac:rich-text-body>'
                        "</ac:structured-macro>"
                    )
                }
            },
        }
        markdown = helpers.storage_to_markdown(
            (
                '<ac:structured-macro ac:name="excerpt-include">'
                '<ac:parameter ac:name=""><ac:link><ri:page ri:content-title="Diagrams" /></ac:link></ac:parameter>'
                '<ac:parameter ac:name="name">CMP Compliance Diagram</ac:parameter>'
                "</ac:structured-macro>"
            ),
            context,
        )
        self.assertIn("Resolved named excerpt", markdown)
        self.assertNotIn("Wrong excerpt", markdown)
        self.assertEqual([], context["report"]["unsupported_content"])

    def test_assign_paths_reuses_existing_folderized_page_path_from_map(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        output_dir = Path(tempdir.name)
        existing_folder = output_dir / "stable-child"
        existing_folder.mkdir(parents=True)
        (existing_folder / "readme.md").write_text("# Existing\n", encoding="utf-8")
        (output_dir / "confluence-map.json").write_text(
            json.dumps(
                {
                    "root_page_id": "root",
                    "pages": {
                        "readme.md": "root",
                        "stable-child/readme.md": "child",
                    },
                }
            ),
            encoding="utf-8",
        )
        pages_by_id = {
            "root": {"id": "root", "title": "Root", "attachments": []},
            "child": {"id": "child", "title": "Child", "attachments": [{"filename": "asset.png"}]},
        }
        children_by_id = {"root": ["child"], "child": []}

        helpers.assign_paths(pages_by_id, children_by_id, "root", output_dir)

        self.assertEqual(existing_folder.resolve(), pages_by_id["child"]["folder_path"])
        self.assertEqual((existing_folder / "readme.md").resolve(), pages_by_id["child"]["markdown_path"])

    def test_assign_paths_reuses_existing_file_page_path_from_map(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        output_dir = Path(tempdir.name)
        existing_file = output_dir / "stable-child.md"
        existing_file.write_text("# Existing\n", encoding="utf-8")
        (output_dir / "confluence-map.json").write_text(
            json.dumps(
                {
                    "root_page_id": "root",
                    "pages": {
                        "readme.md": "root",
                        "stable-child.md": "child",
                    },
                }
            ),
            encoding="utf-8",
        )
        pages_by_id = {
            "root": {"id": "root", "title": "Root", "attachments": []},
            "child": {"id": "child", "title": "Child", "attachments": []},
        }
        children_by_id = {"root": ["child"], "child": []}

        helpers.assign_paths(pages_by_id, children_by_id, "root", output_dir)

        self.assertEqual(existing_file.parent.resolve(), pages_by_id["child"]["folder_path"])
        self.assertEqual(existing_file.resolve(), pages_by_id["child"]["markdown_path"])

    def test_collect_pages_recurses_through_confluence_folders(self) -> None:
        client = FakeClient()
        client.pages = {
            "root": {
                "id": "root",
                "title": "Root",
                "body": {"storage": {"value": "<p>Root body</p>"}},
                "space": {"key": "IDD"},
                "version": {"number": 1},
            },
            "leaf": {
                "id": "leaf",
                "title": "Leaf",
                "body": {"storage": {"value": "<p>Leaf body</p>"}},
                "space": {"key": "IDD"},
                "version": {"number": 2},
            },
        }
        client.folders = {
            "folder": {
                "id": "folder",
                "type": "folder",
                "title": "Folder Node",
                "version": {"number": 1},
            }
        }
        client.direct_children_by_node = {
            ("root", "page"): [{"id": "folder", "type": "folder", "title": "Folder Node"}],
            ("folder", "folder"): [{"id": "leaf", "type": "page", "title": "Leaf"}],
        }

        pages_by_id, children_by_id, excluded, unavailable = helpers.collect_pages(
            client,
            "root",
            recurse=True,
            excluded_page_ids=set(),
            default_space_key="IDD",
        )

        self.assertEqual([], excluded)
        self.assertEqual([], unavailable)
        self.assertEqual(["folder"], children_by_id["root"])
        self.assertEqual(["leaf"], children_by_id["folder"])
        self.assertTrue(pages_by_id["folder"]["export_suppressed"])
        self.assertEqual("folder", pages_by_id["folder"]["content_type"])
        self.assertEqual("page", pages_by_id["leaf"]["content_type"])

    def test_collect_pages_can_target_folder_root(self) -> None:
        client = FakeClient()
        client.folders = {
            "folder": {
                "id": "folder",
                "type": "folder",
                "title": "Folder Root",
                "version": {"number": 1},
            }
        }
        client.pages = {
            "leaf": {
                "id": "leaf",
                "title": "Leaf",
                "body": {"storage": {"value": "<p>Leaf body</p>"}},
                "space": {"key": "IDD"},
                "version": {"number": 2},
            },
        }
        client.direct_children_by_node = {
            ("folder", "folder"): [{"id": "leaf", "type": "page", "title": "Leaf"}],
        }

        pages_by_id, children_by_id, excluded, unavailable = helpers.collect_pages(
            client,
            "folder",
            recurse=True,
            excluded_page_ids=set(),
            default_space_key="IDD",
        )

        self.assertEqual([], excluded)
        self.assertEqual([], unavailable)
        self.assertEqual(["leaf"], children_by_id["folder"])
        self.assertTrue(pages_by_id["folder"]["export_suppressed"])
        self.assertEqual("Folder Root", pages_by_id["folder"]["title"])
        self.assertEqual("Leaf", pages_by_id["leaf"]["title"])

    def test_collect_pages_treats_non_page_content_payload_as_folder(self) -> None:
        client = FakeClient()
        client.pages = {
            "folder": {
                "id": "folder",
                "type": "folder",
                "title": "Folder Root From Content API",
            },
            "leaf": {
                "id": "leaf",
                "title": "Leaf",
                "body": {"storage": {"value": "<p>Leaf body</p>"}},
                "space": {"key": "IDD"},
                "version": {"number": 2},
            },
        }
        client.folders = {
            "folder": {
                "id": "folder",
                "type": "folder",
                "title": "Folder Root",
                "version": {"number": 1},
            }
        }
        client.direct_children_by_node = {
            ("folder", "folder"): [{"id": "leaf", "type": "page", "title": "Leaf"}],
        }

        pages_by_id, children_by_id, excluded, unavailable = helpers.collect_pages(
            client,
            "folder",
            recurse=True,
            excluded_page_ids=set(),
            default_space_key="IDD",
        )

        self.assertEqual([], excluded)
        self.assertEqual([], unavailable)
        self.assertEqual(["leaf"], children_by_id["folder"])
        self.assertEqual("folder", pages_by_id["folder"]["content_type"])
        self.assertEqual("Leaf", pages_by_id["leaf"]["title"])

    def test_collect_pages_skips_unavailable_child_page(self) -> None:
        client = FakeClient()
        client.pages = {
            "root": {
                "id": "root",
                "title": "Root",
                "body": {"storage": {"value": "<p>Root body</p>"}},
                "space": {"key": "IDD"},
                "version": {"number": 1},
            },
        }
        client.direct_children_by_node = {
            ("root", "page"): [{"id": "missing", "type": "page", "title": "Missing"}],
        }

        pages_by_id, children_by_id, excluded, unavailable = helpers.collect_pages(
            client,
            "root",
            recurse=True,
            excluded_page_ids=set(),
            default_space_key="IDD",
        )

        self.assertEqual([], excluded)
        self.assertEqual(["missing (page)"], unavailable)
        self.assertEqual([], children_by_id["root"])
        self.assertEqual(["root"], list(pages_by_id))


if __name__ == "__main__":
    unittest.main()
