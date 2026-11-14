"""Versioned prompt templates. Variables are written {{UPPER_CASE}}; version = sha256 of the template.

Other {{...}} text (e.g. the {{row:column}} answer placeholders described to the model) is left as is.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Prompt:
    name: str
    template: str

    @property
    def version(self) -> str:
        return f"{self.name}@{hashlib.sha256(self.template.encode()).hexdigest()[:10]}"

    def render(self, **values: str) -> str:
        out = self.template
        for k, v in values.items():
            out = out.replace("{{" + k + "}}", v)
        left = re.findall(r"\{\{[A-Z_]+\}\}", out)   # template variables are UPPER_CASE
        if left:
            raise ValueError(f"prompt {self.name}: unfilled placeholder(s) {left}")
        return out


def load(name: str) -> Prompt:
    return Prompt(name, (_DIR / f"{name}.md").read_text(encoding="utf-8"))
