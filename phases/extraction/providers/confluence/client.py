from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin

from .deps import requests


class ConfluenceClient:
    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.api_token = api_token

    def _auth(self):
        return (self.email, self.api_token)

    def _api(self, path: str) -> str:
        return f"{self.base_url}/wiki/rest/api{path}"

    def _raise_for_status(self, response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text.strip()
            if detail:
                raise requests.HTTPError(f"{exc}\nResponse body: {detail}", response=response) from exc
            raise

    def find_page(self, space: str, title: str) -> Optional[dict]:
        url = self._api(
            f"/content?title={requests.utils.quote(title)}&spaceKey={requests.utils.quote(space)}"
            "&expand=version,body.storage,ancestors"
        )
        response = requests.get(url, auth=self._auth())
        self._raise_for_status(response)
        results = response.json().get("results", [])
        return results[0] if results else None

    def find_pages(self, space: str, title: str) -> List[dict]:
        url = self._api(
            f"/content?title={requests.utils.quote(title)}&spaceKey={requests.utils.quote(space)}"
            "&expand=version,body.storage,ancestors"
        )
        response = requests.get(url, auth=self._auth())
        self._raise_for_status(response)
        return response.json().get("results", [])

    def create_page(self, space: str, title: str, parent_id: str, body_storage: str) -> dict:
        url = self._api("/content")
        payload = {
            "type": "page",
            "title": title,
            "ancestors": [{"id": str(parent_id)}],
            "space": {"key": space},
            "body": {"storage": {"value": body_storage, "representation": "storage"}},
        }
        response = requests.post(url, auth=self._auth(), json=payload)
        self._raise_for_status(response)
        return response.json()

    def update_page(self, page_id: str, title: str, body_storage: str, new_version: int) -> dict:
        url = self._api(f"/content/{page_id}")
        payload = {
            "id": str(page_id),
            "type": "page",
            "title": title,
            "version": {"number": new_version},
            "body": {"storage": {"value": body_storage, "representation": "storage"}},
        }
        response = requests.put(url, auth=self._auth(), json=payload)
        self._raise_for_status(response)
        return response.json()

    def get_page(self, page_id: str, expand: str = "body.storage,version,ancestors") -> dict:
        url = self._api(f"/content/{page_id}?expand={expand}")
        response = requests.get(url, auth=self._auth())
        self._raise_for_status(response)
        return response.json()

    def get_children(self, page_id: str, expand: str = "version") -> List[dict]:
        results: List[dict] = []
        start = 0
        while True:
            url = self._api(f"/content/{page_id}/child/page?limit=200&start={start}&expand={expand}")
            response = requests.get(url, auth=self._auth())
            self._raise_for_status(response)
            payload = response.json()
            batch = payload.get("results", [])
            results.extend(batch)
            if payload.get("size", 0) + payload.get("start", 0) >= payload.get("totalSize", 0):
                break
            start += payload.get("size", 0)
            if not payload.get("size", 0):
                break
        return results

    def list_all_descendants(self, root_id: str) -> List[dict]:
        out = []
        queue = [root_id]
        while queue:
            pid = queue.pop(0)
            children = self.get_children(pid, expand="version")
            for child in children:
                out.append(child)
                queue.append(child["id"])
        return out

    def upload_attachment(self, page_id: str, file_path: Path) -> dict:
        url = self._api(f"/content/{page_id}/child/attachment")
        headers = {"X-Atlassian-Token": "nocheck"}
        with open(file_path, "rb") as handle:
            files = {"file": (file_path.name, handle, "application/octet-stream")}
            response = requests.post(url, auth=self._auth(), files=files, headers=headers)
        self._raise_for_status(response)
        return response.json()

    def find_attachment(self, page_id: str, filename: str) -> Optional[dict]:
        url = self._api(
            f"/content/{page_id}/child/attachment?filename={requests.utils.quote(filename)}&expand=version"
        )
        response = requests.get(url, auth=self._auth())
        self._raise_for_status(response)
        results = response.json().get("results", [])
        return results[0] if results else None

    def update_attachment(self, page_id: str, attachment_id: str, file_path: Path) -> dict:
        url = self._api(f"/content/{page_id}/child/attachment/{attachment_id}/data")
        headers = {"X-Atlassian-Token": "nocheck"}
        with open(file_path, "rb") as handle:
            files = {"file": (file_path.name, handle, "application/octet-stream")}
            data = {"minorEdit": "true"}
            response = requests.post(url, auth=self._auth(), files=files, data=data, headers=headers)
        self._raise_for_status(response)
        return response.json()

    def upsert_attachment(self, page_id: str, file_path: Path) -> dict:
        existing = self.find_attachment(page_id, file_path.name)
        if existing:
            return self.update_attachment(page_id, existing["id"], file_path)
        return self.upload_attachment(page_id, file_path)

    def get_attachments(self, page_id: str) -> List[dict]:
        results: List[dict] = []
        start = 0
        while True:
            url = self._api(f"/content/{page_id}/child/attachment?limit=200&start={start}&expand=version,metadata")
            response = requests.get(url, auth=self._auth())
            self._raise_for_status(response)
            payload = response.json()
            batch = payload.get("results", [])
            results.extend(batch)
            if payload.get("size", 0) + payload.get("start", 0) >= payload.get("totalSize", 0):
                break
            start += payload.get("size", 0)
            if not payload.get("size", 0):
                break
        return results

    def download_attachment(self, download_path: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = urljoin(f"{self.base_url}/wiki/", download_path.lstrip("/"))
        response = requests.get(url, auth=self._auth(), stream=True)
        self._raise_for_status(response)
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    handle.write(chunk)

    def _analytics_count(self, page_id: str, endpoint: str, from_date: date) -> int:
        url = self._api(
            f"/analytics/content/{requests.utils.quote(str(page_id))}/{endpoint}"
            f"?fromDate={from_date.isoformat()}"
        )
        response = requests.get(url, auth=self._auth())
        self._raise_for_status(response)
        payload = response.json()
        return int(payload.get("count", 0))

    def get_view_count(self, page_id: str, from_date: date) -> int:
        return self._analytics_count(page_id, "views", from_date)

    def get_unique_viewer_count(self, page_id: str, from_date: date) -> int:
        return self._analytics_count(page_id, "viewers", from_date)
