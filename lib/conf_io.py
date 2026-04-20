from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .auth import missing_dependency_message, resolve_api_key
from .client import ConfluenceClient
from .config import identity_config_path, resolve_identity_settings
from .constants import MAP_FILENAME
from .deps import markdown, requests
from .reports import (
    project_report_path,
    write_confluence_to_repo_report,
    write_project_report,
    write_repo_to_confluence_report,
)

LOG = logging.getLogger("conf_io")


def _resolve_identity(identity_overrides: Dict[str, Optional[str]]) -> Dict[str, object]:
    identity_path = (
        Path(identity_overrides["identity_config"]).expanduser().resolve()
        if identity_overrides.get("identity_config")
        else identity_config_path()
    )
    identity = resolve_identity_settings(
        identity_overrides.get("base_url"),
        identity_overrides.get("email"),
        identity_path,
    )
    return {"identity": identity, "identity_path": identity_path}


def sync_to_confluence(project: Dict[str, Any], identity_overrides: Dict[str, Optional[str]], helpers: Any) -> int:
    source = Path(project["source"]).resolve()
    report_path = project_report_path("publish_report", project.get("name"), project.get("project_path"))
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dry_run": bool(project.get("dry_run", False)),
        "source": str(source),
        "space": project["space"],
        "parent": str(project["parent"]),
        "created_pages": [],
        "updated_pages": [],
        "skipped_pages": [],
        "uploaded_attachments": [],
        "bad_links": [],
        "zombies": [],
        "conflicts": [],
        "warnings": [],
    }

    def finalize(code: int = 0) -> int:
        write_repo_to_confluence_report(report_path, report)
        helpers.LOG.info("Wrote run report to %s", report_path)
        return code

    if not source.exists() or not source.is_dir():
        helpers.LOG.error("Source folder does not exist or is not a directory: %s", source)
        report["conflicts"].append(f"Source folder does not exist or is not a directory: {source}")
        return finalize(2)

    try:
        identity_info = _resolve_identity(identity_overrides)
    except ModuleNotFoundError as exc:
        message = missing_dependency_message(exc)
        helpers.LOG.error(message)
        report["conflicts"].append(message)
        return finalize(3)
    except ValueError as exc:
        helpers.LOG.error("Could not load identity config: %s", exc)
        report["conflicts"].append(f"Could not load identity config: {exc}")
        return finalize(3)

    identity = identity_info["identity"]
    identity_path = Path(identity_info["identity_path"])
    base_url = str(identity["base_url"])
    email = str(identity["email"])
    api_key = str(identity["api_key"])
    if identity["config_loaded"]:
        report["warnings"].append(f"Loaded identity config from {identity_path}")

    token = resolve_api_key(api_key)
    if not token:
        helpers.LOG.error("Confluence API key not found in identity config")
        report["conflicts"].append("Confluence API key not found in identity config")
        return finalize(3)

    if not email:
        helpers.LOG.warning("No email provided; you should pass --email for Confluence API auth")
        report["warnings"].append("No email provided; pass --email for Confluence API auth")

    client = ConfluenceClient(base_url=base_url, email=email, api_token=token)
    ignore = helpers.load_ignore_patterns(project.get("excludes", []))
    existing_page_map = helpers.load_page_map(source)
    if existing_page_map:
        report["warnings"].append(f"Loaded {len(existing_page_map)} page-id mappings from {source / MAP_FILENAME}")

    md_files = helpers.collect_markdown_files(source, ignore)
    if not md_files:
        helpers.LOG.info("No markdown files found under %s", source)
        report["warnings"].append(f"No markdown files found under {source}")
        return finalize(0)

    pages = {}
    for path in md_files:
        title = helpers.parse_title(path)
        if not title:
            helpers.LOG.warning("Skipping %s: no leading H1 title found", path)
            report["warnings"].append(f"Skipped {path}: no leading H1 title found")
            continue
        pages[path] = {"title": title, "path": path}

    local_title_conflicts = helpers.find_local_title_conflicts(pages)
    if local_title_conflicts:
        conflict_message = helpers.format_local_title_conflicts(local_title_conflicts)
        helpers.LOG.error(conflict_message)
        report["conflicts"].append(conflict_message)
        return finalize(4)

    root_page_id = str(project["parent"])
    try:
        target_root_page = client.get_page(root_page_id)
    except requests.HTTPError as exc:
        conflict_message = f"Cannot access target parent/root page {root_page_id}: {exc}"
        helpers.LOG.error(conflict_message)
        report["conflicts"].append(conflict_message)
        return finalize(4)
    root_page_title = target_root_page.get("title", f"page-{root_page_id}")

    dir_readmes = {}
    for path in list(pages.keys()):
        directory = path.parent
        readme = directory / "readme.md"
        if readme in pages:
            dir_readmes[directory] = readme
        elif directory not in dir_readmes:
            dir_readmes[directory] = None

    local_to_pageid = {}
    dir_pageid: Dict[Path, str] = {source: root_page_id}
    dirs = sorted(set(path.parent for path in pages.keys()), key=lambda value: len(str(value)))

    for directory in dirs:
        if directory == source:
            continue
        readme = directory / "readme.md"
        title = pages[readme]["title"] if readme in pages else (directory.name if directory.name else "root")
        chosen_parent = dir_pageid.get(directory.parent, root_page_id)
        mapped_page_id = None
        if readme in pages:
            mapped_entry = existing_page_map.get(helpers.relative_source_path(readme, source), {})
            mapped_page_id = mapped_entry.get("page_id")

        existing = None
        if mapped_page_id:
            try:
                existing = client.get_page(mapped_page_id)
            except requests.HTTPError as exc:
                report["warnings"].append(
                    f"Mapped page id {mapped_page_id} for {helpers.relative_source_path(readme, source)} was not usable: {exc}"
                )
        matches = client.find_pages(project["space"], title) if existing is None else []
        if existing is None:
            existing = helpers.choose_page_for_parent(matches, chosen_parent)

        if existing:
            page_id = existing["id"]
        elif matches:
            conflict_message = helpers.format_page_conflict(title, matches, chosen_parent)
            helpers.LOG.error(conflict_message)
            report["conflicts"].append(conflict_message)
            return finalize(4)
        elif not project.get("dry_run"):
            helpers.LOG.info("Creating page '%s' under parent %s", title, chosen_parent)
            created = client.create_page(project["space"], title, chosen_parent, "<p>Placeholder</p>")
            page_id = created["id"]
            report["created_pages"].append(f"{title} (parent {chosen_parent}, id={page_id})")
        else:
            page_id = "dryrun-" + title
            report["created_pages"].append(f"Would create {title} under parent {chosen_parent}")

        dir_pageid[directory] = page_id

    for path in pages.keys():
        parent_dir = path.parent
        parent_page_id = dir_pageid.get(parent_dir, root_page_id)
        if path.name.lower() == "readme.md":
            local_to_pageid[path] = dir_pageid[parent_dir]
            continue

        title = pages[path]["title"]
        mapped_entry = existing_page_map.get(helpers.relative_source_path(path, source), {})
        mapped_page_id = mapped_entry.get("page_id")
        existing = None
        if mapped_page_id:
            try:
                existing = client.get_page(mapped_page_id)
            except requests.HTTPError as exc:
                report["warnings"].append(
                    f"Mapped page id {mapped_page_id} for {helpers.relative_source_path(path, source)} was not usable: {exc}"
                )
        matches = client.find_pages(project["space"], title) if existing is None else []
        if existing is None:
            existing = helpers.choose_page_for_parent(matches, parent_page_id)
        page_id = existing["id"] if existing else None
        if page_id:
            local_to_pageid[path] = page_id
            continue
        if matches:
            conflict_message = helpers.format_page_conflict(title, matches, parent_page_id)
            helpers.LOG.error(conflict_message)
            report["conflicts"].append(conflict_message)
            return finalize(4)
        if not project.get("dry_run"):
            helpers.LOG.info("Creating child page '%s' under %s", title, parent_page_id)
            created = client.create_page(project["space"], title, parent_page_id, "<p>Placeholder</p>")
            page_id = created["id"]
            report["created_pages"].append(f"{title} (child of {parent_page_id}, id={page_id})")
        else:
            page_id = "dryrun-" + title
            report["created_pages"].append(f"Would create {title} under parent {parent_page_id}")
        local_to_pageid[path] = page_id

    bad_links = []
    for path, meta in pages.items():
        page_id = local_to_pageid[path]
        title = meta["title"]
        helpers.LOG.info("Processing %s -> page %s", path, page_id)
        markdown_text = path.read_text(encoding="utf-8")
        markdown_links = helpers.extract_markdown_links(markdown_text)
        body_markdown = helpers.strip_leading_title_heading(markdown_text, title)
        html = markdown.markdown(body_markdown, extensions=["fenced_code", "tables"])
        soup = helpers.BeautifulSoup(html, "html.parser")

        helpers.replace_mermaid_blocks(
            soup=soup,
            page_path=path,
            page_title=title,
            page_id=page_id,
            client=client,
            report=report,
            dry_run=bool(project.get("dry_run", False)),
        )

        for img in soup.find_all("img"):
            src = img.get("src")
            if not src or helpers.is_remote(src):
                continue
            img_path = (path.parent / src).resolve()
            if not img_path.exists():
                continue
            if not project.get("dry_run"):
                client.upsert_attachment(page_id, img_path)
                report["uploaded_attachments"].append(f"{img_path.name} -> {title} ({page_id})")
                new_tag = soup.new_tag("ac:image")
                ri = soup.new_tag("ri:attachment")
                ri.attrs["ri:filename"] = img_path.name
                new_tag.append(ri)
                img.replace_with(new_tag)
            else:
                img.replace_with(f"[image:{img_path.name}]")

        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            if not href:
                continue
            if href.endswith(".md") or ".md#" in href:
                parts = href.split("#", 1)
                filepart = parts[0]
                frag = parts[1] if len(parts) > 1 else None
                target = (path.parent / filepart).resolve()
                target_id = local_to_pageid.get(target)
                if target_id:
                    if frag:
                        anchor["href"] = f"{base_url}/wiki/pages/viewpage.action?pageId={target_id}#{frag}"
                    else:
                        link_text = anchor.get_text(" ", strip=True)
                        new_tag = helpers.build_confluence_page_link(
                            soup,
                            link_text=link_text,
                            title=pages[target]["title"],
                            space=project["space"],
                        )
                        anchor.replace_with(new_tag)
                else:
                    link_text = anchor.get_text(" ", strip=True)
                    bad_links.append(
                        {
                            "source": str(path),
                            "line": helpers.consume_link_line(markdown_links, href, link_text),
                            "text": link_text,
                            "href": href,
                            "resolved_target": str(target),
                        }
                    )
                    report["bad_links"] = bad_links

        body_storage = str(soup)
        body_hash = helpers.content_fingerprint(body_storage)
        manifest_key = helpers.relative_source_path(path, source)
        mapped_entry = existing_page_map.setdefault(manifest_key, {})
        mapped_entry["page_id"] = str(page_id)

        if project.get("dry_run"):
            helpers.LOG.info("Dry-run: would update page %s (id=%s)", title, page_id)
            report["updated_pages"].append(f"Would update {title} ({page_id})")
            continue

        existing = client.get_page(page_id)
        existing_version = existing.get("version", {}).get("number", 1)
        page_title_for_update = existing.get("title", root_page_title) if path == source / "readme.md" else title
        if (
            mapped_entry.get("page_id") == str(page_id)
            and mapped_entry.get("content_hash") == body_hash
            and mapped_entry.get("confluence_version") == existing_version
        ):
            helpers.LOG.info("Skipping update for %s (manifest hash and version unchanged)", title)
            report["skipped_pages"].append(f"{title} ({page_id}) unchanged via manifest hash/version")
            mapped_entry["content_hash"] = body_hash
            mapped_entry["confluence_version"] = existing_version
            continue
        existing_body = existing.get("body", {}).get("storage", {}).get("value", "")
        if helpers.normalize_html(existing_body) == helpers.normalize_html(body_storage):
            helpers.LOG.info("Skipping update for %s (unchanged)", title)
            report["skipped_pages"].append(f"{title} ({page_id}) unchanged")
            mapped_entry["content_hash"] = body_hash
            mapped_entry["confluence_version"] = existing_version
        else:
            helpers.LOG.info("Updating page %s (id=%s) to version %s", title, page_id, existing_version + 1)
            client.update_page(page_id, page_title_for_update, body_storage, existing_version + 1)
            report["updated_pages"].append(f"{page_title_for_update} ({page_id}) -> version {existing_version + 1}")
            mapped_entry["content_hash"] = body_hash
            mapped_entry["confluence_version"] = existing_version + 1

    all_desc = client.list_all_descendants(root_page_id)
    local_ids = set(local_to_pageid.values())
    zombies = [page for page in all_desc if page["id"] not in local_ids]
    report["zombies"] = [
        {"title": zombie.get("title"), "url": f"{base_url}/wiki/pages/viewpage.action?pageId={zombie['id']}"}
        for zombie in zombies
    ]
    report["bad_links"] = bad_links

    if not project.get("dry_run"):
        path_map = {}
        for path, page_id in local_to_pageid.items():
            key = helpers.relative_source_path(path, source)
            entry = existing_page_map.get(key, {})
            path_map[key] = {
                "page_id": str(page_id),
                "content_hash": str(entry.get("content_hash", "")),
                "confluence_version": entry.get("confluence_version"),
            }
        helpers.write_page_map(source, project["space"], root_page_id, path_map)
        report["warnings"].append(f"Wrote page-id mapping manifest to {source / MAP_FILENAME}")
        committed, git_message = helpers.commit_page_map(source)
        if committed:
            helpers.LOG.info(git_message)
        else:
            helpers.LOG.warning(git_message)
        report["warnings"].append(git_message)
    else:
        report["warnings"].append("Dry-run mode: did not update page-id mapping manifest")

    if zombies:
        helpers.LOG.info("Found %d zombie pages", len(zombies))
    else:
        helpers.LOG.info("No zombie pages detected")

    if bad_links:
        helpers.LOG.warning("Found %d unresolved markdown links", len(bad_links))
    else:
        helpers.LOG.info("No unresolved markdown links detected")

    return finalize(0)


def _export_single_target(target: Dict[str, Any], identity_overrides: Dict[str, Optional[str]], helpers: Any) -> Dict[str, Any]:
    output_dir = Path(target["output"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = project_report_path(
        "export_report",
        target.get("project_name"),
        target.get("project_path"),
    )
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "output": str(output_dir),
        "page": str(target["id"]),
        "recurse": bool(target.get("recurse", False)),
        "metadata_outputs": list(target.get("metadata", ["none"])),
        "excluded_pages": sorted({str(page_id).strip() for page_id in target.get("excludes", []) if str(page_id).strip()}),
        "skipped_pages": [],
        "written_pages": [],
        "metadata_files_written": [],
        "downloaded_attachments": [],
        "attachment_cache_hits": [],
        "unresolved_links": [],
        "unsupported_content": [],
        "unsupported_content_counter": 0,
        "warnings": [],
    }

    def finalize(code: int = 0) -> Dict[str, Any]:
        write_confluence_to_repo_report(report_path, report)
        helpers.LOG.info("Wrote run report to %s", report_path)
        return {"exit_code": code, "report": report}

    try:
        identity_info = _resolve_identity(identity_overrides)
    except ModuleNotFoundError as exc:
        message = missing_dependency_message(exc)
        helpers.LOG.error(message)
        report["warnings"].append(message)
        return finalize(3)
    except ValueError as exc:
        helpers.LOG.error("Could not load identity config: %s", exc)
        report["warnings"].append(f"Could not load identity config: {exc}")
        return finalize(3)

    identity = identity_info["identity"]
    identity_path = Path(identity_info["identity_path"])
    base_url = str(identity["base_url"])
    email = str(identity["email"])
    api_key = str(identity["api_key"])
    if identity["config_loaded"]:
        report["warnings"].append(f"Loaded identity config from {identity_path}")

    token = resolve_api_key(api_key)
    if not token:
        helpers.LOG.error("Confluence API key not found in identity config")
        report["warnings"].append("Confluence API key not found in identity config")
        return finalize(3)
    if not email:
        helpers.LOG.warning("No email provided; you should pass --email for Confluence API auth")
        report["warnings"].append("No email provided; pass --email for Confluence API auth")

    client = ConfluenceClient(base_url, email, token)
    excluded_page_ids = set(report["excluded_pages"])
    pages_by_id, children_by_id, excluded_hits = helpers.collect_pages(
        client,
        str(target["id"]),
        bool(target.get("recurse", False)),
        excluded_page_ids,
    )
    if excluded_hits:
        report["warnings"].append(f"Skipped excluded page ids during collection: {', '.join(excluded_hits)}")
    if str(target["id"]) in excluded_page_ids:
        report["warnings"].append(f"Root page {target['id']} was excluded; nothing was exported")
        return finalize(0)

    for page_info in pages_by_id.values():
        page_info["report"] = report
        page_info["metadata_outputs"] = list(target.get("metadata", ["none"]))
    helpers.enrich_analytics(client, pages_by_id, report)
    helpers.enrich_attachments(client, pages_by_id)
    helpers.assign_paths(pages_by_id, children_by_id, str(target["id"]), output_dir)
    helpers.apply_export_cache(pages_by_id, output_dir, report)
    helpers.write_pages(client, pages_by_id, output_dir)
    if "file" in target.get("metadata", []):
        manifest_path = helpers.export_metadata_manifest_path(output_dir)
        manifest_payload = helpers.export_metadata_manifest(output_dir, pages_by_id, report)
        manifest_path.write_text(
            json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        report["metadata_files_written"].append(str(manifest_path))
    map_path = helpers.write_page_map(output_dir, str(target["id"]), pages_by_id)
    report["warnings"].append(f"Wrote page-id mapping manifest to {map_path}")
    return finalize(0)


def export_from_confluence(project: Dict[str, Any], identity_overrides: Dict[str, Optional[str]], helpers: Any) -> int:
    summary_report_path = project_report_path("project_report", project.get("name"), project.get("project_path"))
    project_report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project": project["project_path"],
        "project_name": project.get("name"),
        "activity": project["activity"],
        "successful_targets": [],
        "failed_targets": [],
        "warnings": [],
    }

    overall_code = 0
    for space_entry in project["spaces"]:
        space_key = space_entry["space"]
        for page_entry in space_entry["pages"]:
            target_with_project = dict(page_entry)
            target_with_project["project_name"] = project.get("name")
            target_with_project["project_path"] = project.get("project_path")
            target_with_project["metadata"] = list(project.get("metadata", ["none"]))
            result = _export_single_target(target_with_project, identity_overrides, helpers)
            target_summary = {
                "label": f"{space_key}:{page_entry['id']}",
                "kind": "confluence_to_repo",
                "detail": (
                    f"space={space_key} page={page_entry['id']} output={page_entry['output']} "
                    f"recurse={page_entry['recurse']} excludes={len(page_entry.get('excludes', []))} "
                    f"metadata={','.join(project.get('metadata', ['none']))}"
                ),
                "exit_code": result["exit_code"],
            }
            if result["exit_code"] == 0:
                project_report["successful_targets"].append(target_summary)
            else:
                overall_code = result["exit_code"]
                project_report["failed_targets"].append(target_summary)
            for warning in result["report"]["warnings"]:
                project_report["warnings"].append(f"{target_summary['label']}: {warning}")

    write_project_report(summary_report_path, project_report)
    LOG.info("Wrote project report to %s", summary_report_path)
    return overall_code
