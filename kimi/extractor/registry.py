"""Small host-based registry for the built-in Kimi-DLP extractors."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse


class UnsupportedSiteError(ValueError):
    """The URL is not handled by a registered extractor."""


@dataclass(frozen=True, slots=True)
class ExtractorSpec:
    name: str
    hosts: frozenset[str]
    factory: Callable[..., Any]


class ExtractorRegistry:
    """Resolve a supported URL to an extractor without changing extractor APIs."""

    def __init__(self) -> None:
        self._specs: dict[str, ExtractorSpec] = {}
        self._host_map: dict[str, str] = {}

    def register(
        self,
        name: str,
        hosts: set[str] | frozenset[str] | tuple[str, ...] | list[str],
        factory: Callable[..., Any],
        *,
        replace: bool = False,
    ) -> None:
        key = str(name).strip().lower()
        host_set = frozenset(str(host).strip().lower().rstrip(".") for host in hosts if str(host).strip())
        if not key or not host_set or not callable(factory):
            raise ValueError("name, at least one host, and a callable factory are required")
        if key in self._specs and not replace:
            raise ValueError(f"Extractor already registered: {key}")
        conflicts = [host for host in host_set if host in self._host_map and self._host_map[host] != key]
        if conflicts:
            raise ValueError(f"Hosts already registered: {', '.join(sorted(conflicts))}")
        if key in self._specs:
            for host, owner in list(self._host_map.items()):
                if owner == key:
                    self._host_map.pop(host, None)
        spec = ExtractorSpec(key, host_set, factory)
        self._specs[key] = spec
        for host in host_set:
            self._host_map[host] = key

    def unregister(self, name: str) -> bool:
        key = str(name).strip().lower()
        spec = self._specs.pop(key, None)
        if spec is None:
            return False
        for host in spec.hosts:
            if self._host_map.get(host) == key:
                self._host_map.pop(host, None)
        return True

    def platform_for_url(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise UnsupportedSiteError("URL is required.")
        try:
            parsed = urlparse(value.strip())
        except ValueError as exc:
            raise UnsupportedSiteError("Invalid URL.") from exc
        host = (parsed.hostname or "").lower().rstrip(".")
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            raise UnsupportedSiteError("Only credential-free HTTP(S) URLs are supported.")
        try:
            port = parsed.port
        except ValueError as exc:
            raise UnsupportedSiteError("Invalid URL port.") from exc
        if port not in (None, 443 if scheme == "https" else 80):
            raise UnsupportedSiteError("Non-standard ports are not supported.")
        platform = self._host_map.get(host)
        if platform is None:
            raise UnsupportedSiteError(f"No extractor registered for host: {host}")
        return platform

    def create_for_url(self, value: str, **kwargs: Any) -> Any:
        platform = self.platform_for_url(value)
        return self._specs[platform].factory(**kwargs)

    def registered_sites(self) -> dict[str, tuple[str, ...]]:
        return {name: tuple(sorted(spec.hosts)) for name, spec in sorted(self._specs.items())}


def create_default_registry() -> ExtractorRegistry:
    """Create a registry for the bundled YouTube and Instagram extractors."""
    from .instagram import InstagramExtractor
    from .youtube import YouTubeExtractor

    registry = ExtractorRegistry()
    registry.register(
        "instagram",
        {"instagram.com", "www.instagram.com", "m.instagram.com", "applink.instagram.com", "instagr.am", "www.instagr.am"},
        InstagramExtractor,
    )
    registry.register(
        "youtube",
        {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"},
        YouTubeExtractor,
    )
    return registry


__all__ = ["ExtractorRegistry", "ExtractorSpec", "UnsupportedSiteError", "create_default_registry"]
