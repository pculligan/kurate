from __future__ import annotations

from datetime import date
from pathlib import Path
import time
from typing import List, Optional
from urllib.parse import urljoin

from .deps import requests

DEFAULT_TIMEOUT = (10, 120)
DEFAULT_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class ConfluenceClient:
    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.api_token = api_token

    def _auth(self):
        return (self.email, self.api_token)

    def _api(self, path: str) -> str:
        return f"{self.base_url}/wiki/rest/api{path}"

    def _api_v2(self, path: str) -> str:
        return f"{self.base_url}/wiki/api/v2{path}"

    def _raise_for_status(self, response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text.strip()
            if detail:
                raise requests.HTTPError(f"{exc}\nResponse body: {detail}", response=response) from exc
            raise

    def _request(self, method: str, url: str, *, retries: int = DEFAULT_RETRIES, **kwargs):
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                response = requests.request(method, url, **kwargs)
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < retries:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                return response
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt >= retries:
                    raise
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        if last_exc:
            raise last_exc
        raise RuntimeError("Request retry loop exited without a response")

    def _post_file(self, url: str, file_path: Path, *, data: Optional[dict] = None, retries: int = DEFAULT_RETRIES):
        headers = {"X-Atlassian-Token": "nocheck"}
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                with open(file_path, "rb") as handle:
                    files = {"file": (file_path.name, handle, "application/octet-stream")}
                    response = self._request(
                        "POST",
                        url,
                        auth=self._auth(),
                        files=files,
                        data=data,
                        headers=headers,
                        retries=1,
                    )
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < retries:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                return response
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt >= retries:
                    raise
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        if last_exc:
            raise last_exc
        raise RuntimeError("File upload retry loop exited without a response")

    def find_page(self, space: str, title: str) -> Optional[dict]:
        url = self._api(
            f"/content?title={requests.utils.quote(title)}&spaceKey={requests.utils.quote(space)}"
            "&expand=version,body.storage,ancestors"
        )
        response = self._request("GET", url, auth=self._auth())
        self._raise_for_status(response)
        results = response.json().get("results", [])
        return results[0] if results else None

    def find_pages(self, space: str, title: str) -> List[dict]:
        url = self._api(
            f"/content?title={requests.utils.quote(title)}&spaceKey={requests.utils.quote(space)}"
            "&expand=version,body.storage,ancestors"
        )
        response = self._request("GET", url, auth=self._auth())
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
        response = self._request("POST", url, auth=self._auth(), json=payload, retries=1)
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
        response = self._request("PUT", url, auth=self._auth(), json=payload, retries=1)
        self._raise_for_status(response)
        return response.json()

    def get_page(self, page_id: str, expand: str = "body.storage,version,ancestors") -> dict:
        url = self._api(f"/content/{page_id}?expand={expand}")
        response = self._request("GET", url, auth=self._auth())
        self._raise_for_status(response)
        return response.json()

    def get_children(self, page_id: str, expand: str = "version") -> List[dict]:
        results: List[dict] = []
        start = 0
        while True:
            url = self._api(f"/content/{page_id}/child/page?limit=200&start={start}&expand={expand}")
            response = self._request("GET", url, auth=self._auth())
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

    def _get_cursor_results(self, initial_url: str) -> List[dict]:
        results: List[dict] = []
        url = initial_url
        while url:
            response = self._request("GET", url, auth=self._auth())
            self._raise_for_status(response)
            payload = response.json()
            results.extend(payload.get("results", []))
            next_url = payload.get("_links", {}).get("next")
            if next_url and str(next_url).startswith("http"):
                url = str(next_url)
            elif next_url and str(next_url).startswith("/"):
                url = urljoin(self.base_url, str(next_url))
            elif next_url:
                url = urljoin(f"{self.base_url}/wiki/api/v2/", str(next_url))
            else:
                url = ""
        return results

    def get_folder(self, folder_id: str) -> dict:
        url = self._api_v2(f"/folders/{requests.utils.quote(str(folder_id))}")
        response = self._request("GET", url, auth=self._auth())
        self._raise_for_status(response)
        return response.json()

    def get_direct_children(self, content_id: str, content_type: str) -> List[dict]:
        normalized_type = content_type.strip().lower()
        if normalized_type == "page":
            path = f"/pages/{requests.utils.quote(str(content_id))}/direct-children?limit=200"
        elif normalized_type == "folder":
            path = f"/folders/{requests.utils.quote(str(content_id))}/direct-children?limit=200"
        else:
            return []
        return self._get_cursor_results(self._api_v2(path))

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
        response = self._post_file(url, file_path, retries=1)
        self._raise_for_status(response)
        return response.json()

    def find_attachment(self, page_id: str, filename: str) -> Optional[dict]:
        url = self._api(
            f"/content/{page_id}/child/attachment?filename={requests.utils.quote(filename)}&expand=version"
        )
        response = self._request("GET", url, auth=self._auth())
        self._raise_for_status(response)
        results = response.json().get("results", [])
        return results[0] if results else None

    def update_attachment(self, page_id: str, attachment_id: str, file_path: Path) -> dict:
        url = self._api(f"/content/{page_id}/child/attachment/{attachment_id}/data")
        response = self._post_file(url, file_path, data={"minorEdit": "true"})
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
            response = self._request("GET", url, auth=self._auth())
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
        response = self._request("GET", url, auth=self._auth(), stream=True)
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
        response = self._request("GET", url, auth=self._auth())
        self._raise_for_status(response)
        payload = response.json()
        return int(payload.get("count", 0))

    def get_view_count(self, page_id: str, from_date: date) -> int:
        return self._analytics_count(page_id, "views", from_date)

    def get_unique_viewer_count(self, page_id: str, from_date: date) -> int:
        return self._analytics_count(page_id, "viewers", from_date)
