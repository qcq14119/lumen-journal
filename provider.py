"""Small Images API client; retry decisions belong to the durable job runner."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import aiohttp

from .store import AlbumError, MAX_BYTES


class ProviderError(AlbumError):
    def __init__(
        self,
        message: str,
        *,
        unknown: bool = False,
        retry_after: int = 0,
        terminal: bool = False,
        stage: str = "provider",
        code: str = "",
        http_status: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.unknown = unknown
        self.retry_after = retry_after
        self.terminal = terminal
        self.retryable = retryable
        self.diagnostic = {"stage": stage, "code": code, "http_status": http_status}


class PublicResolver(aiohttp.resolver.DefaultResolver):
    """Validate resolved addresses in the connector to prevent DNS rebinding."""

    def __init__(self, provider_host: str = ""):
        super().__init__()
        self.provider_host = provider_host

    async def resolve(self, host, port=0, family=0):
        addresses = await super().resolve(host, port, family)

        # TUN proxies use benchmarking addresses for synthetic DNS. Only the
        # explicitly configured provider may use this range, never an arbitrary
        # download or redirect destination.
        def permitted(address):
            ip = ipaddress.ip_address(address["host"])
            synthetic = ip in ipaddress.ip_network(
                "198.18.0.0/15"
            ) or ip in ipaddress.ip_network("2001:2::/48")
            return ip.is_global or (host == self.provider_host and synthetic)

        if not addresses or any(not permitted(a) for a in addresses):
            raise OSError("Private network targets are not allowed")
        return addresses


def validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
        ):
            raise ValueError
        try:
            if not ipaddress.ip_address(parts.hostname).is_global:
                raise ValueError
        except ValueError as exc:
            if parts.hostname.replace(".", "").isdigit() or ":" in parts.hostname:
                raise ValueError from exc
        if parts.hostname == "localhost" or parts.hostname.endswith(
            (".localhost", ".local")
        ):
            raise ValueError
    except ValueError as exc:
        raise AlbumError("图片服务和下载地址必须是公开的 HTTPS 地址") from exc
    return url


def parse_result(data: dict, task_id: str = "") -> tuple[str, str]:
    """Read image or asynchronous task responses without returning full payloads.

    Args:
        data: Decoded response object.
        task_id: Existing task id used to select an item in a task query.

    Returns:
        A (kind, value) pair: base64, url, pending, failed, or unsupported.
    """
    if not isinstance(data, dict):
        return "unsupported", "图片服务返回了无法识别的结构"
    own_id = data.get("task_id") or data.get("id")
    if task_id and own_id and str(own_id) != task_id:
        return "unsupported", "查询结果不属于当前任务"
    if data.get("error"):
        return "failed", "图片服务拒绝了任务，请在服务端检查任务记录"
    state = str(data.get("status", data.get("state", ""))).lower()
    if state in ("failed", "failure", "error", "cancelled", "canceled"):
        return "failed", "图片生成失败"
    if isinstance(data.get("b64_json"), str):
        return "base64", data["b64_json"]
    for key in ("url", "image_url"):
        if isinstance(data.get(key), str) and data[key].startswith(
            ("https://", "data:image/")
        ):
            return (
                ("base64", data[key].split(",", 1)[1])
                if data[key].startswith("data:")
                else ("url", data[key])
            )
    for key in ("data", "tasks", "result", "output", "images"):
        value = data.get(key)
        if isinstance(value, dict):
            if task_id and task_id in value:
                value = value[task_id]
            parsed = parse_result(value, task_id)
            if parsed[0] != "unsupported":
                return parsed
        elif isinstance(value, list) and value:
            items = [v for v in value if isinstance(v, dict)]
            if task_id:
                matching = [
                    v
                    for v in items
                    if str(v.get("task_id", v.get("id", ""))) == task_id
                ]
                identified = [v for v in items if v.get("task_id") or v.get("id")]
                if identified and not matching:
                    return "unsupported", "查询结果不包含当前任务"
                items = matching or items
            for item in items[:1]:
                parsed = parse_result(item, task_id)
                if parsed[0] != "unsupported":
                    return parsed
            if isinstance(value[0], str) and value[0].startswith("https://"):
                return "url", value[0]
    identifier = data.get("task_id") or (
        data.get("id")
        if state
        in (
            "pending",
            "queued",
            "running",
            "processing",
            "submitted",
            "in_progress",
            "succeeded",
            "completed",
            "success",
        )
        else None
    )
    if identifier:
        return "pending", str(identifier)
    return "unsupported", "图片服务没有返回图片或可查询的任务编号"


class ImageProvider:
    def __init__(self, settings: dict):
        self.settings = dict(settings)
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        validate_url(self.settings["base_url"])
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120, connect=20),
            connector=aiohttp.TCPConnector(
                resolver=PublicResolver(urlsplit(self.settings["base_url"]).hostname),
                limit=4,
            ),
            trust_env=False,
        )
        return self

    async def __aexit__(self, *_):
        await self.session.close()

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        validate_url(url)
        try:
            async with self.session.request(
                method, url, allow_redirects=False, **kwargs
            ) as response:
                if response.status not in (200, 201, 202):
                    if response.status in (401, 403):
                        raise ProviderError(
                            "图片服务鉴权失败，请检查 API Key 和服务权限",
                            code="authentication",
                            http_status=response.status,
                        )
                    if response.status == 429:
                        delay = response.headers.get("Retry-After", "60")
                        try:
                            seconds = int(delay)
                        except ValueError:
                            try:
                                seconds = int(
                                    parsedate_to_datetime(delay).timestamp()
                                    - time.time()
                                )
                            except (TypeError, ValueError, OverflowError):
                                seconds = 60
                        raise ProviderError(
                            "图片服务限流，请稍后再试",
                            unknown=method == "GET",
                            retry_after=max(1, seconds),
                            retryable=True,
                            code="rate_limit",
                            http_status=429,
                        )
                    raise ProviderError(
                        f"图片服务返回 HTTP {response.status}",
                        unknown=method == "POST" and response.status >= 500,
                        retryable=response.status >= 500 or response.status == 408,
                        code="http_error",
                        http_status=response.status,
                    )
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_BYTES * 2:
                        raise ProviderError(
                            "图片响应超过大小限制", unknown=method == "POST"
                        )
                try:
                    result = json.loads(body)
                    if not isinstance(result, dict):
                        raise ValueError
                    return result
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ProviderError(
                        "图片服务返回非预期 JSON", unknown=method == "POST"
                    ) from exc
        except aiohttp.ClientConnectorError as exc:
            raise ProviderError(
                "无法连接图片服务，请检查网络或代理设置",
                unknown=False,
                retryable=True,
                code=type(exc).__name__,
            ) from exc
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise ProviderError(
                "图片服务连接中断或超时",
                unknown=method == "POST",
                retryable=True,
                code=type(exc).__name__,
            ) from exc

    @property
    def headers(self) -> dict:
        if not self.settings.get("api_key"):
            raise AlbumError("请先在设置中填写图片服务 API Key")
        return {"Authorization": "Bearer " + self.settings["api_key"]}

    async def models(self) -> list[str]:
        data = await self._request(
            "GET",
            self.settings["base_url"].rstrip("/") + "/models",
            headers=self.headers,
        )
        return [
            x["id"]
            for x in data.get("data", [])
            if isinstance(x, dict) and isinstance(x.get("id"), str)
        ]

    async def submit(self, prompt: str, references: list[bytes]) -> dict:
        base = self.settings["base_url"].rstrip("/")
        if not self.settings.get("model"):
            raise AlbumError("请先选择图片模型")
        values = {"model": self.settings["model"], "prompt": prompt, "n": 1}
        timeout = aiohttp.ClientTimeout(total=self.settings["task_timeout"], connect=20)
        if self.settings.get("size"):
            values["size"] = self.settings["size"]
        if references:
            form = aiohttp.FormData()
            for key, value in values.items():
                form.add_field(key, str(value))
            for i, raw in enumerate(references):
                form.add_field(
                    self.settings["reference_field"],
                    raw,
                    filename=f"reference_{i + 1}.png",
                    content_type="image/png",
                )
            return await self._request(
                "POST",
                base + "/images/edits",
                data=form,
                headers=self.headers,
                timeout=timeout,
            )
        return await self._request(
            "POST",
            base + "/images/generations",
            json=values,
            headers=self.headers,
            timeout=timeout,
        )

    async def poll(self, task_id: str) -> dict:
        base = urlsplit(self.settings["base_url"])
        return await self._request(
            "GET",
            f"{base.scheme}://{base.netloc}/api/image-tasks",
            params={"ids": task_id},
            headers=self.headers,
        )

    async def image_bytes(self, kind: str, value: str) -> bytes:
        if kind == "base64":
            if len(value) > MAX_BYTES * 1.4:
                raise AlbumError("图片超过大小限制")
            try:
                return base64.b64decode(value, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise AlbumError("图片编码无法解析") from exc
        url = value
        for _ in range(4):
            validate_url(url)
            try:
                async with self.session.get(url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        from urllib.parse import urljoin

                        url = urljoin(url, response.headers.get("Location", ""))
                        continue
                    if response.status != 200:
                        raise ProviderError(
                            f"上游已生成，图片下载返回 HTTP {response.status}",
                            stage="download",
                            code="http_error",
                            http_status=response.status,
                        )
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise ProviderError(
                                "图片超过 20 MB", stage="download", code="too_large"
                            )
                    return bytes(body)
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                code = type(exc).__name__
                reason = "网络连接中断"
                if isinstance(exc, TimeoutError):
                    reason = "连接或读取超时"
                elif isinstance(exc, aiohttp.ClientConnectorCertificateError):
                    reason = "TLS 证书校验失败"
                elif isinstance(exc, aiohttp.ClientConnectorError):
                    reason = "DNS、连接或目标地址检查失败"
                raise ProviderError(
                    f"上游已生成，下载失败：{reason}。可恢复下载原结果",
                    stage="download",
                    code=code,
                ) from exc
        raise ProviderError(
            "图片下载重定向次数过多", stage="download", code="redirect_limit"
        )
