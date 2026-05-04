"""cloudflared config.yml parser/writer.

Uses ruamel.yaml in round-trip mode so comments and formatting survive edits.
The catch-all ingress entry (one without a `hostname` key) is always kept as
the final entry; new rules are inserted just before it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_yaml = YAML(typ="rt")
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True


@dataclass
class TunnelDoc:
    data: Any  # ruamel CommentedMap

    @property
    def ingress(self) -> list:
        return self.data["ingress"]

    def hostnames(self) -> list[str]:
        return [
            entry["hostname"]
            for entry in self.ingress
            if isinstance(entry, dict) and "hostname" in entry
        ]

    def find(self, hostname: str) -> dict | None:
        for entry in self.ingress:
            if isinstance(entry, dict) and entry.get("hostname") == hostname:
                return entry
        return None

    def add(self, hostname: str, service: str) -> None:
        if self.find(hostname) is not None:
            raise ValueError(f"ingress already has hostname {hostname!r}")
        catch_all_idx = self._catch_all_index()
        new_entry = {"hostname": hostname, "service": service}
        if catch_all_idx is None:
            self.ingress.append(new_entry)
        else:
            self.ingress.insert(catch_all_idx, new_entry)

    def update(self, hostname: str, *, service: str) -> None:
        entry = self.find(hostname)
        if entry is None:
            raise KeyError(hostname)
        entry["service"] = service

    def remove(self, hostname: str) -> bool:
        for idx, entry in enumerate(self.ingress):
            if isinstance(entry, dict) and entry.get("hostname") == hostname:
                del self.ingress[idx]
                return True
        return False

    def _catch_all_index(self) -> int | None:
        for idx, entry in enumerate(self.ingress):
            if isinstance(entry, dict) and "hostname" not in entry:
                return idx
        return None


def load(path: Path | str) -> TunnelDoc:
    with open(path) as f:
        return TunnelDoc(data=_yaml.load(f))


def save(doc: TunnelDoc, path: Path | str) -> None:
    with open(path, "w") as f:
        _yaml.dump(doc.data, f)


def loads(text: str) -> TunnelDoc:
    return TunnelDoc(data=_yaml.load(text))


def dumps(doc: TunnelDoc) -> str:
    import io

    buf = io.StringIO()
    _yaml.dump(doc.data, buf)
    return buf.getvalue()
