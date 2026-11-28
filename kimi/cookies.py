"""Netscape/httpx cookie loading, merging and persistence for Kimi-DLP."""
from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from copy import copy
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any

import httpx


class CookieStoreError(ValueError):
    """Invalid cookie source or cookie persistence failure."""


class CookieStore:
    """A small cookie store shared by extractors.

    Supported sources: Netscape cookie-file path/text, ``dict[str, str]``,
    ``httpx.Cookies``, ``CookieJar`` or another ``CookieStore``. Mapping
    cookies are scoped to ``default_domain``; Netscape files retain their
    original domain/path/secure/expiry/HttpOnly attributes.
    """

    def __init__(
        self,
        source: Any = None,
        *,
        default_domain: str = ".youtube.com",
        path: str | Path | None = None,
        cookies: httpx.Cookies | None = None,
    ) -> None:
        self.default_domain = default_domain
        self.path = Path(path).expanduser() if path is not None else None
        self.cookies = cookies or httpx.Cookies()
        self._http_only: set[tuple[str, str, str]] = set()
        if source is not None:
            self.load(source)

    def load(self, source: Any) -> "CookieStore":
        if isinstance(source, CookieStore):
            self._merge_jar(source.cookies.jar)
            self._http_only.update(source._http_only)
            if self.path is None and source.path is not None:
                self.path = source.path
            return self
        if isinstance(source, httpx.Cookies):
            self._merge_jar(source.jar)
            return self
        if isinstance(source, CookieJar):
            self._merge_jar(source)
            return self
        if isinstance(source, Mapping):
            for name, value in source.items():
                if isinstance(value, (dict, Cookie)):
                    if isinstance(value, Cookie):
                        self.cookies.jar.set_cookie(copy(value))
                        continue
                    cookie_value = value.get("value")
                    domain = value.get("domain") or self.default_domain
                    cookie_path = value.get("path") or "/"
                    secure = bool(value.get("secure", False))
                    expires = value.get("expires")
                    self._set_cookie(
                        str(name), str(cookie_value or ""), domain=str(domain),
                        path=str(cookie_path), secure=secure,
                        expires=int(expires) if expires is not None else None,
                        rest={"HttpOnly": None} if value.get("httponly") else {},
                    )
                    if value.get("httponly") and name:
                        self._http_only.add((str(domain), str(cookie_path), str(name)))
                else:
                    self._set_cookie(str(name), str(value), domain=self.default_domain)
            return self

        if isinstance(source, Path):
            path = source.expanduser()
            if not path.is_file():
                raise CookieStoreError(f"Cookie file does not exist: {path}")
            text = path.read_text(encoding="utf-8-sig")
            if self.path is None:
                self.path = path
        elif isinstance(source, str):
            candidate = Path(source).expanduser()
            if "\n" not in source and "\t" not in source and candidate.is_file():
                text = candidate.read_text(encoding="utf-8-sig")
                if self.path is None:
                    self.path = candidate
            else:
                text = source.lstrip("\ufeff")
        else:
            raise CookieStoreError(
                "Cookies must be a Netscape path/text, mapping, httpx.Cookies, CookieJar or CookieStore."
            )
        self._load_netscape(text)
        return self

    def _set_cookie(
        self,
        name: str,
        value: str,
        *,
        domain: str,
        path: str = "/",
        secure: bool = False,
        expires: int | None = None,
        rest: dict[str, Any] | None = None,
        domain_specified: bool = True,
    ) -> None:
        if not name:
            return
        domain = domain.strip()
        if not domain:
            domain = self.default_domain
        initial_dot = domain.startswith(".")
        cookie = Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=bool(domain_specified), domain_initial_dot=initial_dot,
            path=path or "/", path_specified=True, secure=bool(secure),
            expires=expires, discard=expires is None, comment=None,
            comment_url=None, rest=rest or {}, rfc2109=False,
        )
        self.cookies.jar.set_cookie(cookie)

    def _load_netscape(self, text: str) -> None:
        for raw in text.splitlines():
            line = raw.strip("\r\n")
            if not line:
                continue
            http_only = line.startswith("#HttpOnly_")
            if http_only:
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 7:
                continue
            domain, include_subdomains, cookie_path, secure, expiry, name, value = parts
            if not domain or not name:
                continue
            try:
                expires = int(expiry) if expiry not in {"", "0"} else None
            except ValueError:
                continue
            if expires is not None:
                import time
                if expires <= int(time.time()):
                    continue
            self._set_cookie(
                name, value, domain=domain, path=cookie_path or "/",
                secure=secure.upper() == "TRUE", expires=expires,
                rest={"HttpOnly": None} if http_only else {},
                domain_specified=(include_subdomains.upper() == "TRUE" or domain.startswith(".")),
            )
            if http_only:
                self._http_only.add((domain, cookie_path or "/", name))

    def _merge_jar(self, source: CookieJar) -> None:
        for cookie in source:
            self.cookies.jar.set_cookie(copy(cookie))

    def bind(self, target: httpx.Cookies) -> httpx.Cookies:
        """Merge this store into an existing client jar and track that jar."""
        if target is not self.cookies:
            for cookie in self.cookies.jar:
                target.jar.set_cookie(copy(cookie))
            self.cookies = target
        return target

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path).expanduser() if path is not None else self.path
        if target is None:
            raise CookieStoreError("No cookie file path configured; pass path=... to save().")
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Netscape HTTP Cookie File", "# Generated by Kimi-DLP. Keep this file private."]
        cookies = sorted(
            self.cookies.jar,
            key=lambda c: (c.domain or "", c.path or "/", c.name or ""),
        )
        for cookie in cookies:
            domain = cookie.domain or self.default_domain
            include_subdomains = "TRUE" if cookie.domain_specified else "FALSE"
            secure = "TRUE" if cookie.secure else "FALSE"
            expires = str(int(cookie.expires)) if cookie.expires is not None else "0"
            identity = (cookie.domain or "", cookie.path or "/", cookie.name or "")
            prefix = "#HttpOnly_" if identity in self._http_only else ""
            fields = (
                prefix + domain, include_subdomains, cookie.path or "/", secure,
                expires, cookie.name or "", cookie.value or "",
            )
            lines.append("\t".join(fields))
        data = ("\n".join(lines) + "\n").encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        self.path = target
        return target

    def get_for_url(self, name: str, url: str, default: Any = None) -> Any:
        """Return the most specific unexpired cookie matching a request URL."""
        from urllib.parse import urlparse
        import time

        try:
            parsed = urlparse(url)
        except ValueError:
            return default
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
        secure_request = parsed.scheme.lower() == "https"
        now = int(time.time())
        matches = []
        for cookie in self.cookies.jar:
            if cookie.name != name or (cookie.expires is not None and cookie.expires <= now):
                continue
            domain = (cookie.domain or "").lower().lstrip(".")
            if not domain:
                continue
            host_matches = host == domain or (
                cookie.domain_specified and host.endswith("." + domain)
            )
            cookie_path = cookie.path or "/"
            path_matches = path == cookie_path or (
                path.startswith(cookie_path.rstrip("/") + "/")
                if cookie_path != "/" else True
            )
            if not host_matches or not path_matches or (cookie.secure and not secure_request):
                continue
            matches.append(cookie)
        if not matches:
            return default
        matches.sort(key=lambda cookie: len(cookie.path or "/"), reverse=True)
        return matches[0].value

    def clear(self, domain: str | None = None) -> None:
        if domain is None:
            self.cookies.clear()
            self._http_only.clear()
            return
        for cookie in list(self.cookies.jar):
            if cookie.domain == domain or cookie.domain.endswith("." + domain.lstrip(".")):
                self.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)
                self._http_only.discard((cookie.domain or "", cookie.path or "/", cookie.name or ""))

    def stats(self) -> dict[str, Any]:
        items = list(self.cookies.jar)
        return {
            "count": len(items),
            "domains": sorted({cookie.domain for cookie in items if cookie.domain}),
            "persistent": sum(cookie.expires is not None for cookie in items),
            "http_only": sum(
                (cookie.domain or "", cookie.path or "/", cookie.name or "") in self._http_only
                for cookie in items
            ),
            "secure": sum(bool(cookie.secure) for cookie in items),
            "file": str(self.path) if self.path else None,
        }


__all__ = ["CookieStore", "CookieStoreError"]
