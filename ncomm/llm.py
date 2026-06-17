"""DeepSeek client: turn a working-tree diff into Conventional Commits groups.

The DeepSeek API is OpenAI-compatible, so this is a single chat-completions
call constrained to JSON output. The model returns one or more `CommitGroup`s;
ncomm then stages each group's explicit file list and commits it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List

import httpx

from .config import Config
from .gitops import Changes

SYSTEM_PROMPT = """\
You are ncomm, a tool that turns a git working-tree diff into well-formed \
Conventional Commits.

Your job:
- Read the diff bundle and decide how many commits it should become. Group \
changes that belong to one logical change together. Aim for FEWER, \
higher-quality commits — do not split trivially. Usually 1-3 commits.
- Each group must be independently committable: list the EXACT file paths \
(including new files) that belong to it. Every changed file must appear in \
exactly one group. Do not omit files. Do not invent files not in the bundle.
- type ∈ feat | fix | docs | style | refactor | perf | test | build | ci | \
chore | revert. Pick the most accurate.
- scope is optional, lowercase, one short word (e.g. auth, api, deps). Omit if \
unclear — do not force one.
- summary: imperative mood, ≤ 50 chars, no trailing period, in the user's \
language. Lowercase first word unless a proper noun.
- body: 1-3 lines explaining WHAT and WHY (not the diff). Blank line after \
summary. May be empty for trivial changes.
- rationale: one short sentence per group explaining why these files go \
together (shown to the user for review).

Hard rules:
- Output ONLY a JSON object: {"groups": [ {group}, ... ]}.
- Never include markdown fences or backticks.
- If no_group is true, return exactly ONE group covering all changes.
"""

JSON_SCHEMA_HINT = """\
{
  "groups": [
    {
      "type": "feat",
      "scope": "auth",
      "summary": "add OAuth2 login flow",
      "body": "Wires the OAuth2 device-code grant into the login view.",
      "files": ["src/auth/oauth.py", "src/auth/__init__.py", "tests/test_oauth.py"],
      "rationale": "OAuth2 implementation plus its test, one feature unit."
    }
  ]
}
"""


@dataclass
class CommitGroup:
    type: str
    summary: str
    scope: str = ""
    body: str = ""
    files: List[str] = field(default_factory=list)
    rationale: str = ""

    @property
    def header(self) -> str:
        """Rendered Conventional Commits header, e.g. `feat(auth): add OAuth2 login flow`."""
        scope = f"({self.scope})" if self.scope else ""
        return f"{self.type}{scope}: {self.summary}"

    @property
    def message(self) -> str:
        """Full commit message: header + blank + body."""
        if self.body:
            return f"{self.header}\n\n{self.body}"
        return self.header


class LLMError(RuntimeError):
    pass


def _user_message(changes: Changes, *, no_group: bool, lang: str) -> str:
    lines = [
        f"Repository language for messages: {lang}",
        f"no_group: {str(no_group).lower()}",
        "",
        changes.diff_bundle,
    ]
    return "\n".join(lines)


def parse_groups(raw: object) -> List[CommitGroup]:
    """Validate the model's JSON into CommitGroup objects."""
    if not isinstance(raw, dict):
        raise LLMError("Model response was not a JSON object.")
    raw_groups = raw.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise LLMError("Model response has no 'groups' array.")
    groups: List[CommitGroup] = []
    for item in raw_groups:
        if not isinstance(item, dict):
            continue
        gtype = str(item.get("type", "")).strip().lower()
        summary = str(item.get("summary", "")).strip()
        if not gtype or not summary:
            raise LLMError(f"Group missing type or summary: {item}")
        files = item.get("files", [])
        if not isinstance(files, list) or not files:
            raise LLMError(f"Group '{summary}' has no files.")
        # Sanitize scope: drop empty / null-literal / placeholder values the
        # model sometimes emits (None, null, "-") so they don't render as
        # `fix(None): ...`. A blank scope simply renders as `fix: ...`.
        raw_scope = str(item.get("scope", "")).strip()
        if raw_scope.lower() in {"", "none", "null", "-", "n/a"}:
            raw_scope = ""
        groups.append(
            CommitGroup(
                type=gtype,
                summary=summary,
                scope=raw_scope,
                body=str(item.get("body", "")).strip(),
                files=[str(f).strip() for f in files if str(f).strip()],
                rationale=str(item.get("rationale", "")).strip(),
            )
        )
    if not groups:
        raise LLMError("No valid groups parsed from model response.")
    return groups


def suggest_groups(
    changes: Changes,
    cfg: Config,
    *,
    no_group: bool = False,
    lang: str = "en",
    timeout: float = 45.0,
) -> List[CommitGroup]:
    if not cfg.has_key:
        raise LLMError("No API key configured.")
    if changes.is_empty:
        raise LLMError("No changes to commit.")

    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": "Expected output shape:\n" + JSON_SCHEMA_HINT},
            {"role": "user", "content": _user_message(changes, no_group=no_group, lang=lang)},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = httpx.post(
            f"{cfg.base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"Request to DeepSeek failed: {exc}") from exc

    if resp.status_code != 200:
        detail = resp.text.strip()
        raise LLMError(f"DeepSeek returned HTTP {resp.status_code}: {detail[:300]}")

    try:
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"Could not parse DeepSeek response: {exc}") from exc

    return parse_groups(data)
