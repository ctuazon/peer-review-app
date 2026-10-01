"""Create / read / update / delete review prompts."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from app import PROMPTS_PATH, ensure_data_dir, load_json, save_json

GENERIC_REPO_TYPE = "generic"


@dataclass
class Prompt:
    id: str
    name: str
    repo_type: str
    content: str
    is_generic: bool = False
    # [{"path": "src/Api/**", "instructions": "..."}]: asked of the model only
    # when a changed file matches the glob.
    path_instructions: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Prompt":
        repo_type = (data.get("repo_type") or GENERIC_REPO_TYPE).strip().lower()
        if data.get("is_generic") is None:
            is_generic = repo_type == GENERIC_REPO_TYPE
        else:
            is_generic = bool(data["is_generic"])
        if is_generic:
            repo_type = GENERIC_REPO_TYPE
        return cls(
            id=str(data.get("id") or uuid.uuid4()),
            name=(data.get("name") or "Untitled").strip() or "Untitled",
            repo_type=repo_type,
            content=data.get("content") or "",
            is_generic=is_generic,
            path_instructions=[
                {"path": str(item["path"]), "instructions": str(item["instructions"])}
                for item in data.get("path_instructions") or []
                if isinstance(item, dict) and item.get("path") and item.get("instructions")
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_raw() -> dict[str, Any]:
    ensure_data_dir()
    data = load_json(PROMPTS_PATH, {"prompts": []})
    if not isinstance(data, dict):
        return {"prompts": []}
    if "prompts" not in data or not isinstance(data["prompts"], list):
        data["prompts"] = []
    return data


def list_prompts() -> list[Prompt]:
    return [Prompt.from_dict(item) for item in _load_raw()["prompts"]]


def get_prompt(prompt_id: str) -> Prompt | None:
    for prompt in list_prompts():
        if prompt.id == prompt_id:
            return prompt
    return None


def save_all(prompts: list[Prompt]) -> None:
    save_json(PROMPTS_PATH, {"prompts": [p.to_dict() for p in prompts]})


def create_prompt(
    name: str,
    repo_type: str,
    content: str,
    is_generic: bool = False,
) -> Prompt:
    prompts = list_prompts()
    prompt = Prompt.from_dict(
        {
            "id": str(uuid.uuid4()),
            "name": name,
            "repo_type": repo_type,
            "content": content,
            "is_generic": is_generic,
        }
    )
    prompts.append(prompt)
    save_all(prompts)
    return prompt


def update_prompt(
    prompt_id: str,
    *,
    name: str | None = None,
    repo_type: str | None = None,
    content: str | None = None,
    is_generic: bool | None = None,
) -> Prompt:
    prompts = list_prompts()
    for i, prompt in enumerate(prompts):
        if prompt.id != prompt_id:
            continue
        data = prompt.to_dict()
        if name is not None:
            data["name"] = name
        if repo_type is not None:
            data["repo_type"] = repo_type
        if content is not None:
            data["content"] = content
        if is_generic is not None:
            data["is_generic"] = is_generic
        updated = Prompt.from_dict(data)
        prompts[i] = updated
        save_all(prompts)
        return updated
    raise KeyError(f"Prompt not found: {prompt_id}")


def delete_prompt(prompt_id: str) -> None:
    prompts = list_prompts()
    filtered = [p for p in prompts if p.id != prompt_id]
    if len(filtered) == len(prompts):
        raise KeyError(f"Prompt not found: {prompt_id}")
    save_all(filtered)


def move_prompt(prompt_id: str, direction: int) -> list[Prompt]:
    """
    Move a prompt up (direction < 0) or down (direction > 0) in the saved order.
    Returns the updated prompt list.
    """
    if direction == 0:
        return list_prompts()
    prompts = list_prompts()
    index = next((i for i, p in enumerate(prompts) if p.id == prompt_id), None)
    if index is None:
        raise KeyError(f"Prompt not found: {prompt_id}")
    target = index + (1 if direction > 0 else -1)
    if target < 0 or target >= len(prompts):
        return prompts
    prompts[index], prompts[target] = prompts[target], prompts[index]
    save_all(prompts)
    return prompts


def move_prompt_to_index(prompt_id: str, new_index: int) -> list[Prompt]:
    """Move a prompt to an absolute index in the saved order."""
    prompts = list_prompts()
    index = next((i for i, p in enumerate(prompts) if p.id == prompt_id), None)
    if index is None:
        raise KeyError(f"Prompt not found: {prompt_id}")
    if new_index < 0:
        new_index = 0
    if new_index >= len(prompts):
        new_index = len(prompts) - 1
    if index == new_index:
        return prompts
    prompt = prompts.pop(index)
    prompts.insert(new_index, prompt)
    save_all(prompts)
    return prompts


def infer_repo_type(repo_name: str) -> str:
    """Best-effort map from repo name to a prompt repo_type key."""
    name = repo_name.lower()
    # Prefer exact prompt matches later; this is only a hint for filtering.
    tokens = re_split_tokens(name)
    return tokens[0] if tokens else GENERIC_REPO_TYPE


def re_split_tokens(name: str) -> list[str]:
    import re

    parts = re.split(r"[-_\s]+", name.lower())
    return [p for p in parts if p]


def prompts_for_repo(repo_name: str) -> list[Prompt]:
    """
    Return prompts applicable to a repo:
    - generic prompts
    - prompts whose repo_type matches the repo name or a token within it
    Ordered: matching repo-type prompts first, then generic.
    """
    all_prompts = list_prompts()
    repo = repo_name.lower()
    tokens = set(re_split_tokens(repo))
    tokens.add(repo)

    matched: list[Prompt] = []
    generic: list[Prompt] = []
    for prompt in all_prompts:
        rt = prompt.repo_type.lower()
        if prompt.is_generic or rt == GENERIC_REPO_TYPE:
            generic.append(prompt)
        elif rt in tokens or rt == repo or rt in repo:
            matched.append(prompt)

    return matched + generic


class PromptCycler:
    """Cycle through applicable prompts for a given repo."""

    def __init__(self, repo_name: str = "") -> None:
        self.repo_name = repo_name
        self._index = 0
        self.prompts: list[Prompt] = []
        self.refresh()

    def refresh(self) -> None:
        current_id = self.current.id if self.prompts else None
        if self.repo_name:
            self.prompts = prompts_for_repo(self.repo_name)
        else:
            self.prompts = list_prompts()
        if not self.prompts:
            self._index = 0
            return
        if current_id:
            for i, p in enumerate(self.prompts):
                if p.id == current_id:
                    self._index = i
                    return
        self._index = min(self._index, len(self.prompts) - 1)

    @property
    def current(self) -> Prompt | None:
        if not self.prompts:
            return None
        return self.prompts[self._index]

    def next(self) -> Prompt | None:
        if not self.prompts:
            return None
        self._index = (self._index + 1) % len(self.prompts)
        return self.current

    def prev(self) -> Prompt | None:
        if not self.prompts:
            return None
        self._index = (self._index - 1) % len(self.prompts)
        return self.current

    def set_by_id(self, prompt_id: str) -> Prompt | None:
        for i, prompt in enumerate(self.prompts):
            if prompt.id == prompt_id:
                self._index = i
                return prompt
        return None

    def label(self) -> str:
        if not self.prompts:
            return "No prompts yet"
        prompt = self.current
        assert prompt is not None
        kind = "generic" if prompt.is_generic else prompt.repo_type
        return f"{self._index + 1}/{len(self.prompts)} — {prompt.name} [{kind}]"
