from __future__ import annotations

import base64
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class OpencodeHttpError(Exception):
    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"opencode http {status} on {url}: {body[:400]}")
        self.status = status
        self.body = body
        self.url = url


@dataclass(slots=True)
class OpencodeSessionInfo:
    id: str
    directory: str
    title: str


class OpencodeClient:
    def __init__(
        self,
        base_url: str,
        *,
        password: str | None = None,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._password = password
        self._request_timeout_seconds = request_timeout_seconds
        self._auth_header: str | None = None
        if password:
            self._auth_header = "Basic " + base64.b64encode(f":{password}".encode()).decode("ascii")

    def create_session(self, *, directory: str, title: str | None = None) -> OpencodeSessionInfo:
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        payload = self._request(
            "POST",
            "/session",
            query={"directory": directory},
            body=body,
        )
        return OpencodeSessionInfo(
            id=str(payload["id"]),
            directory=str(payload.get("directory", directory)),
            title=str(payload.get("title", title or "")),
        )

    def get_session(self, session_id: str, *, directory: str | None = None) -> dict[str, Any]:
        return self._request("GET", f"/session/{session_id}", query=self._directory_query(directory))

    def session_status(self, *, directory: str | None = None) -> dict[str, dict[str, Any]]:
        payload = self._request("GET", "/session/status", query=self._directory_query(directory))
        if not isinstance(payload, dict):
            return {}
        return payload

    def send_prompt_async(
        self,
        session_id: str,
        *,
        prompt_text: str,
        directory: str | None = None,
        agent: str | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": prompt_text}],
        }
        if agent:
            body["agent"] = agent
        self._request(
            "POST",
            f"/session/{session_id}/prompt_async",
            query=self._directory_query(directory),
            body=body,
            expect_json=False,
        )

    def abort_session(self, session_id: str, *, directory: str | None = None) -> bool:
        try:
            payload = self._request(
                "POST",
                f"/session/{session_id}/abort",
                query=self._directory_query(directory),
                body=None,
            )
        except OpencodeHttpError as exc:
            if exc.status == 404:
                return False
            raise
        return bool(payload) if payload is not None else True

    def respond_permission(
        self,
        session_id: str,
        permission_id: str,
        *,
        response: str,
        directory: str | None = None,
    ) -> bool:
        if response not in {"once", "always", "reject"}:
            msg = f"invalid permission response: {response!r}"
            raise ValueError(msg)
        try:
            payload = self._request(
                "POST",
                f"/session/{session_id}/permissions/{permission_id}",
                query=self._directory_query(directory),
                body={"response": response},
            )
        except OpencodeHttpError as exc:
            if exc.status in {404, 409}:
                logger.debug("permission %s already resolved for session %s", permission_id, session_id)
                return False
            raise
        return bool(payload) if payload is not None else True

    def stream_events(
        self,
        *,
        directory: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> Iterator[dict[str, Any]]:
        url = self._build_url("/event", self._directory_query(directory))
        request = urllib.request.Request(url, headers=self._headers(accept="text/event-stream"))
        with urllib.request.urlopen(request, timeout=None) as response:  # noqa: S310
            buffer: list[str] = []
            for raw_line in response:
                if stop_event is not None and stop_event.is_set():
                    return
                line = raw_line.decode("utf-8", errors="replace")
                if line in {"\n", "\r\n"}:
                    if not buffer:
                        continue
                    event_text = "".join(buffer).rstrip("\n")
                    buffer.clear()
                    data = self._extract_sse_data(event_text)
                    if data is None:
                        continue
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        logger.debug("ignoring non-json SSE data: %r", data[:200])
                    continue
                buffer.append(line)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        expect_json: bool = True,
    ) -> Any:
        url = self._build_url(path, query)
        headers = self._headers(content_type="application/json" if body is not None else None)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._request_timeout_seconds) as response:  # noqa: S310
                raw = response.read()
                if not expect_json or not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body_text = ""
            try:
                body_text = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            raise OpencodeHttpError(status=exc.code, body=body_text, url=url) from exc

    def _build_url(self, path: str, query: dict[str, str] | None) -> str:
        url = f"{self.base_url}{path}"
        if query:
            filtered = {k: v for k, v in query.items() if v is not None}
            if filtered:
                url = f"{url}?{urllib.parse.urlencode(filtered)}"
        return url

    def _headers(self, *, content_type: str | None = None, accept: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        if accept:
            headers["Accept"] = accept
        if self._auth_header:
            headers["Authorization"] = self._auth_header
        return headers

    def _directory_query(self, directory: str | None) -> dict[str, str]:
        if not directory:
            return {}
        return {"directory": directory}

    @staticmethod
    def _extract_sse_data(event_text: str) -> str | None:
        data_lines: list[str] = []
        for line in event_text.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return None
        return "\n".join(data_lines)
