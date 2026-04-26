"""Command Center dashboard plugin.

Configurable, read-only work-health dashboard for Hermes.

Normalization model:
- source adapters scan local systems deterministically
- adapters emit normalized work_items, health_checks, inbox_items, and source diagnostics
- restart scripts are generated from normalized items without requiring an LLM
- optional future cron/LLM enrichment can cache or improve ambiguous cases
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()

STATUS_GROUPS = {
    "active": "active",
    "in-progress": "active",
    "doing": "active",
    "now": "active",
    "current": "active",
    "open": "active",
    "ongoing": "active",
    "started": "active",
    "waiting": "waiting",
    "blocked": "waiting",
    "pending": "waiting",
    "delegated": "waiting",
    "on-hold": "waiting",
    "paused": "paused",
    "backburner": "paused",
    "someday": "paused",
    "someday/maybe": "paused",
    "later": "paused",
    "icebox": "paused",
    "complete": "done",
    "completed": "done",
    "done": "done",
    "archived": "done",
    "closed": "done",
    "shipped": "done",
    "resolved": "done",
}

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


class CommandCenterConfig:
    """Small config holder.

    Keep this as a plain class instead of @dataclass because Hermes' dashboard
    plugin loader imports modules without inserting them into sys.modules first;
    dataclasses can fail under that import style.
    """

    def __init__(
        self,
        workspace_name: str = "Command Center",
        workspace_root: Path | None = None,
        projects_dir: str = "projects",
        captures_dir: str = "captures",
        wiki_path: Path | None = None,
        default_stale_after_days: int = 7,
        max_file_bytes: int = 250_000,
        max_work_items: int = 500,
        max_inbox_items: int = 12,
        expose_absolute_paths: bool = False,
        focus_window_minutes: int = 15,
        enable_project_adapter: bool = True,
        enable_wiki_adapter: bool = True,
        enable_sessions_adapter: bool = True,
        enable_git_adapter: bool = True,
        git_roots: list[Path] | None = None,
        git_max_repos: int = 60,
        git_stale_days: int = 14,
        hermes_home: Path | None = None,
        session_limit: int = 30,
        session_stale_days: int = 7,
        config_path: Path | None = None,
        config_source: str = "env/defaults",
        setup_needed: bool = True,
    ):
        self.workspace_name = workspace_name
        self.workspace_root = workspace_root
        self.projects_dir = projects_dir
        self.captures_dir = captures_dir
        self.wiki_path = wiki_path
        self.default_stale_after_days = default_stale_after_days
        self.max_file_bytes = max_file_bytes
        self.max_work_items = max_work_items
        self.max_inbox_items = max_inbox_items
        self.expose_absolute_paths = expose_absolute_paths
        self.focus_window_minutes = focus_window_minutes
        self.enable_project_adapter = enable_project_adapter
        self.enable_wiki_adapter = enable_wiki_adapter
        self.enable_sessions_adapter = enable_sessions_adapter
        self.enable_git_adapter = enable_git_adapter
        self.git_roots = git_roots or []
        self.git_max_repos = git_max_repos
        self.git_stale_days = git_stale_days
        self.hermes_home = hermes_home or Path.home() / ".hermes"
        self.session_limit = session_limit
        self.session_stale_days = session_stale_days
        self.config_path = config_path
        self.config_source = config_source
        self.setup_needed = setup_needed


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _path_from_env(*names: str) -> Path | None:
    for name in names:
        raw = os.getenv(name)
        if raw:
            return Path(raw).expanduser()
    return None


def _paths_from_env(name: str) -> list[Path]:
    raw = os.getenv(name, "")
    return [Path(part.strip()).expanduser() for part in raw.split(",") if part.strip()]


def _paths_from_config(value: Any) -> list[Path]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, list):
        parts = [str(p).strip() for p in value]
    else:
        return []
    return [Path(p).expanduser() for p in parts if p]


def _config_value(cfg: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in cfg and cfg[key] not in (None, ""):
            return cfg[key]
    return default


def _load_json_config() -> tuple[dict[str, Any], Path | None, str]:
    explicit = _path_from_env("COMMAND_CENTER_CONFIG")
    candidates = [explicit] if explicit else [Path.home() / ".hermes" / "command-center.json"]
    for path in candidates:
        if not path:
            continue
        path = path.expanduser()
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8")), path, str(path)
        except Exception:
            return {}, path, f"invalid:{path}"
    return {}, None, "env/defaults"


def _default_git_roots(workspace_root: Path | None, hermes_home: Path) -> list[Path]:
    roots: list[Path] = []
    for candidate in [
        Path.home() / "projects",
        Path.home() / "code",
        Path.home() / "src",
        workspace_root,
        hermes_home / "plugins",
        hermes_home / "hermes-agent",
    ]:
        if candidate and candidate.exists() and candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


def _load_config() -> CommandCenterConfig:
    cfg, config_path, config_source = _load_json_config()
    workspace_raw = os.getenv("COMMAND_CENTER_WORKSPACE") or os.getenv("COMMAND_CENTER_VAULT_PATH") or os.getenv("OBSIDIAN_VAULT_PATH") or _config_value(cfg, "workspace_root", "workspace", "vault_path", "COMMAND_CENTER_WORKSPACE")
    workspace_root = Path(workspace_raw).expanduser() if workspace_raw else None
    if workspace_root is None and not cfg:
        fallback = Path.home() / "vault"
        workspace_root = fallback if fallback.exists() else None

    wiki_raw = os.getenv("COMMAND_CENTER_WIKI_PATH") or os.getenv("WIKI_PATH") or _config_value(cfg, "wiki_path", "COMMAND_CENTER_WIKI_PATH")
    wiki_path = Path(wiki_raw).expanduser() if wiki_raw else None
    if wiki_path is None and not cfg:
        # Backward-compatible weak auto-detection only before setup exists.
        candidates = []
        if workspace_root:
            candidates.append(workspace_root)
        candidates.append(Path.home() / "wiki")
        for candidate in candidates:
            if (candidate / "SCHEMA.md").exists() and (candidate / "index.md").exists():
                wiki_path = candidate
                break

    hermes_raw = os.getenv("COMMAND_CENTER_HERMES_HOME") or os.getenv("HERMES_HOME") or _config_value(cfg, "hermes_home", "COMMAND_CENTER_HERMES_HOME")
    hermes_home = Path(hermes_raw).expanduser() if hermes_raw else (Path.home() / ".hermes")
    explicit_git_roots = _paths_from_env("COMMAND_CENTER_GIT_ROOTS") or _paths_from_config(_config_value(cfg, "git_roots", "COMMAND_CENTER_GIT_ROOTS"))
    git_roots = explicit_git_roots
    setup_needed = not bool(cfg or os.getenv("COMMAND_CENTER_GIT_ROOTS") or os.getenv("COMMAND_CENTER_WORKSPACE") or os.getenv("OBSIDIAN_VAULT_PATH"))

    return CommandCenterConfig(
        workspace_name=os.getenv("COMMAND_CENTER_WORKSPACE_NAME") or _config_value(cfg, "workspace_name", "COMMAND_CENTER_WORKSPACE_NAME", default="Command Center"),
        workspace_root=workspace_root,
        projects_dir=os.getenv("COMMAND_CENTER_PROJECTS_DIR") or _config_value(cfg, "projects_dir", "COMMAND_CENTER_PROJECTS_DIR", default="projects"),
        captures_dir=os.getenv("COMMAND_CENTER_CAPTURES_DIR") or _config_value(cfg, "captures_dir", "COMMAND_CENTER_CAPTURES_DIR", default="captures"),
        wiki_path=wiki_path,
        default_stale_after_days=_env_int("COMMAND_CENTER_STALE_AFTER_DAYS", int(_config_value(cfg, "default_stale_after_days", "COMMAND_CENTER_STALE_AFTER_DAYS", default=7))),
        max_file_bytes=_env_int("COMMAND_CENTER_MAX_FILE_BYTES", int(_config_value(cfg, "max_file_bytes", "COMMAND_CENTER_MAX_FILE_BYTES", default=250_000))),
        max_work_items=_env_int("COMMAND_CENTER_MAX_WORK_ITEMS", int(_config_value(cfg, "max_work_items", "COMMAND_CENTER_MAX_WORK_ITEMS", default=500))),
        max_inbox_items=_env_int("COMMAND_CENTER_MAX_INBOX_ITEMS", int(_config_value(cfg, "max_inbox_items", "COMMAND_CENTER_MAX_INBOX_ITEMS", default=12))),
        expose_absolute_paths=_env_bool("COMMAND_CENTER_EXPOSE_ABSOLUTE_PATHS", bool(_config_value(cfg, "expose_absolute_paths", "COMMAND_CENTER_EXPOSE_ABSOLUTE_PATHS", default=False))),
        focus_window_minutes=_env_int("COMMAND_CENTER_FOCUS_WINDOW_MINUTES", int(_config_value(cfg, "focus_window_minutes", "COMMAND_CENTER_FOCUS_WINDOW_MINUTES", default=15))),
        enable_project_adapter=_env_bool("COMMAND_CENTER_ENABLE_PROJECTS", bool(_config_value(cfg, "enable_projects", "COMMAND_CENTER_ENABLE_PROJECTS", default=True))),
        enable_wiki_adapter=_env_bool("COMMAND_CENTER_ENABLE_WIKI", bool(_config_value(cfg, "enable_wiki", "COMMAND_CENTER_ENABLE_WIKI", default=True))),
        enable_sessions_adapter=_env_bool("COMMAND_CENTER_ENABLE_SESSIONS", bool(_config_value(cfg, "enable_sessions", "COMMAND_CENTER_ENABLE_SESSIONS", default=True))),
        enable_git_adapter=_env_bool("COMMAND_CENTER_ENABLE_GIT", bool(_config_value(cfg, "enable_git", "COMMAND_CENTER_ENABLE_GIT", default=True))),
        git_roots=git_roots,
        git_max_repos=_env_int("COMMAND_CENTER_GIT_MAX_REPOS", int(_config_value(cfg, "git_max_repos", "COMMAND_CENTER_GIT_MAX_REPOS", default=60))),
        git_stale_days=_env_int("COMMAND_CENTER_GIT_STALE_DAYS", int(_config_value(cfg, "git_stale_days", "COMMAND_CENTER_GIT_STALE_DAYS", default=14))),
        hermes_home=hermes_home,
        session_limit=_env_int("COMMAND_CENTER_SESSION_LIMIT", int(_config_value(cfg, "session_limit", "COMMAND_CENTER_SESSION_LIMIT", default=30))),
        session_stale_days=_env_int("COMMAND_CENTER_SESSION_STALE_DAYS", int(_config_value(cfg, "session_stale_days", "COMMAND_CENTER_SESSION_STALE_DAYS", default=7))),
        config_path=config_path,
        config_source=config_source,
        setup_needed=setup_needed,
    )


def _setup_prompt(config: CommandCenterConfig) -> str:
    target = str(config.config_path or (Path.home() / ".hermes" / "command-center.json"))
    return f"""Set up Command Center for this local Hermes install. Inspect my machine read-only first, then write a tailored JSON config to `{target}`.

Goals:
1. Identify where I actually track work: vault/workspace roots, project folders, capture/inbox folders, wiki/reference folders.
2. Identify code roots/repositories that should appear in Command Center. Do not scan the whole home directory. Prefer explicit roots.
3. Decide which adapters should be enabled: markdown projects, wiki health, Hermes sessions, local git.
4. Write `{target}` using this shape:
{{
  "workspace_name": "Personal Command Center",
  "workspace_root": "/absolute/path/or/null",
  "projects_dir": "projects",
  "captures_dir": "captures",
  "wiki_path": null,
  "git_roots": ["/absolute/path/to/code"],
  "enable_projects": true,
  "enable_wiki": false,
  "enable_sessions": true,
  "enable_git": true,
  "default_stale_after_days": 7,
  "git_stale_days": 14,
  "session_stale_days": 7
}}

Rules:
- Ask me before making broad assumptions.
- Treat freeform notes as weak signals unless their conventions are clear.
- Prefer structured sources like git/session state for priority.
- After writing the config, restart the Hermes dashboard and verify `/api/plugins/command-center/data`.
"""


def _safe_rel(path: Path, root: Path | None) -> str:
    if root:
        try:
            return str(path.resolve().relative_to(root.resolve()))
        except Exception:
            pass
    return str(path)


def _path_payload(path: Path, root: Path | None, config: CommandCenterConfig) -> dict[str, str | None]:
    rel = _safe_rel(path, root)
    return {
        "path": str(path) if config.expose_absolute_paths else rel,
        "relative_path": rel,
        "absolute_path": str(path) if config.expose_absolute_paths else None,
    }


def _read_text(path: Path, config: CommandCenterConfig, warnings: list[dict[str, Any]] | None = None) -> str:
    try:
        size = path.stat().st_size
        if size > config.max_file_bytes:
            if warnings is not None:
                warnings.append({
                    "id": f"oversized:{path}",
                    "severity": "warning",
                    "title": "Skipped oversized markdown file",
                    "source": "filesystem",
                    "message": f"{path} is {size} bytes; limit is {config.max_file_bytes} bytes.",
                    "path": str(path),
                })
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        if warnings is not None:
            warnings.append({
                "id": f"read-error:{path}",
                "severity": "warning",
                "title": "Could not read file",
                "source": "filesystem",
                "message": str(exc),
                "path": str(path),
            })
        return ""


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end].strip()
    body = text[end + 4 :].lstrip("\n")
    meta: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [x.strip().strip('"\'') for x in value[1:-1].split(",") if x.strip()]
        elif value.lower() in {"true", "false"}:
            meta[key] = value.lower() == "true"
        else:
            meta[key] = value.strip('"\'')
    return meta, body


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    raw = str(value).strip().strip('"\'')
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _title_from_body(path: Path, body: str, meta: dict[str, Any] | None = None) -> str:
    meta = meta or {}
    if meta.get("title"):
        return str(meta["title"])
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("-", " ").replace("_", " ").title()


def _section(body: str, heading: str) -> str:
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(body)
    if not match:
        return ""
    start = match.end()
    next_match = re.search(r"^##\s+", body[start:], re.MULTILINE)
    end = start + next_match.start() if next_match else len(body)
    return body[start:end].strip()


def _section_any(body: str, headings: list[str]) -> str:
    for heading in headings:
        value = _section(body, heading)
        if value:
            return value
    return ""


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _extract_wikilinks(text: str) -> list[str]:
    links = []
    for match in re.findall(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]", text):
        link = match.strip()
        if link:
            links.append(link)
    return links


def _extract_next_actions(body: str) -> list[dict[str, Any]]:
    sections = ["Next actions", "Next Actions", "Tasks", "Action items", "TODO", "Todos", "Next", "Open loops", "Follow up", "Follow-up", "Waiting on"]
    action_text = "\n".join(_section(body, heading) for heading in sections if _section(body, heading))
    if not action_text:
        # Generic markdown fallback: unchecked checkboxes anywhere.
        action_text = "\n".join(line for line in body.splitlines() if re.match(r"^\s*[-*]\s+\[[ xX]\]", line))
    actions: list[dict[str, Any]] = []
    for line in action_text.splitlines():
        if not re.match(r"^\s*[-*]\s+(\[[ xX]\]\s*)?", line):
            continue
        done = bool(re.match(r"^\s*[-*]\s+\[[xX]\]", line))
        text = re.sub(r"^\s*[-*]\s+(\[[ xX]\]\s*)?", "", line).strip()
        if not text:
            continue
        estimate = None
        est_match = re.search(r"(?:_|\()\s*(\d+)\s*[–-]?\s*(\d+)?\s*min", text, re.IGNORECASE)
        if est_match:
            nums = [int(x) for x in est_match.groups() if x]
            estimate = min(nums) if nums else None
        actions.append({"text": text, "done": done, "estimate_minutes": estimate, "source": "markdown"})
    return actions


def _status_group(status: str) -> str:
    return STATUS_GROUPS.get(status.lower().strip(), "unknown")


def _health_check(check_id: str, source: str, severity: str, title: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": check_id,
        "source": source,
        "severity": severity,
        "title": title,
        "message": message,
        **extra,
    }


def _restart_script(item: dict[str, Any], action: dict[str, Any] | None, config: CommandCenterConfig, reason: str) -> dict[str, Any]:
    target = item.get("relative_path") or item.get("path") or item.get("title")
    action_text = action.get("text") if action else f"Review {item.get('title')} and choose one concrete next action."
    success = f"One concrete artifact changed or one decision captured within {config.focus_window_minutes} minutes."
    if item.get("type") == "wiki_health":
        success = "The wiki health issue is resolved or a clear follow-up note is captured."
    return {
        "id": f"restart:{item.get('id', item.get('title'))}",
        "source": item.get("source"),
        "title": item.get("title"),
        "reason": reason,
        "open": f"Open {target}.",
        "do_first": action_text,
        "success_condition": success,
        "stop_boundary": f"Stop after {config.focus_window_minutes} minutes or when the single action is done.",
        "prompt": (
            f"Continue this Command Center item.\n"
            f"Item: {item.get('title')}\n"
            f"Source: {item.get('source_label') or item.get('source')}\n"
            f"Target: {target}\n"
            f"Reason: {reason}\n"
            f"Recommended action: {action_text}\n\n"
            f"Help me make one focused {config.focus_window_minutes}-minute push. "
            "First decide whether this is a strong structured signal or a weak/freeform note signal. "
            "Then give exactly: 1) what to open/run first, 2) the smallest useful action, "
            "3) what counts as done, 4) where to stop."
        ),
        "work_item_id": item.get("id"),
    }


class Adapter:
    id = "adapter"
    label = "Adapter"
    type = "generic"

    def scan(self, config: CommandCenterConfig) -> dict[str, Any]:
        raise NotImplementedError


def _candidate_project_dirs(root: Path, configured: str) -> list[Path]:
    configured_dirs = [root / part.strip() for part in configured.split(",") if part.strip()]
    existing = [p for p in configured_dirs if p.exists()]
    if existing:
        return existing
    names = ["projects", "Projects", "work", "Work", "clients", "Clients", "areas", "Areas", "tasks", "Tasks"]
    candidates = []
    for name in names:
        path = root / name
        if path.exists() and path.is_dir():
            candidates.append(path)
    if candidates:
        return candidates
    return [root]


def _candidate_inbox_dirs(root: Path, configured: str) -> list[Path]:
    configured_dirs = [root / part.strip() for part in configured.split(",") if part.strip()]
    existing = [p for p in configured_dirs if p.exists()]
    if existing:
        return existing
    names = ["captures", "Captures", "inbox", "Inbox", "quick-notes", "Quick Notes", "quick notes"]
    return [root / name for name in names if (root / name).exists()]


class MarkdownProjectAdapter(Adapter):
    id = "markdown-projects"
    label = "Markdown Projects"
    type = "work"

    def scan(self, config: CommandCenterConfig) -> dict[str, Any]:
        diagnostics: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        inbox: list[dict[str, Any]] = []
        root = config.workspace_root
        if root is None:
            diagnostics.append(_health_check("projects:not-configured", self.id, "warning", "No workspace configured", "Set COMMAND_CENTER_WORKSPACE or OBSIDIAN_VAULT_PATH to enable markdown project scanning."))
            return self._result("warning", diagnostics, checks, items, inbox)
        project_dirs = _candidate_project_dirs(root, config.projects_dir)
        capture_dirs = _candidate_inbox_dirs(root, config.captures_dir)
        if not project_dirs:
            diagnostics.append(_health_check("projects:missing-dir", self.id, "warning", "No work-item folders found", "Command Center could not find a project/work folder. Run setup or set COMMAND_CENTER_PROJECTS_DIR."))
        else:
            for projects_dir in project_dirs:
                for path in list(projects_dir.rglob("*.md"))[: config.max_work_items]:
                    if path.name.lower() in {"readme.md", "project-template.md"}:
                        continue
                    text = _read_text(path, config, diagnostics)
                    if not text:
                        continue
                    meta, body = _parse_frontmatter(text)
                    status = str(meta.get("status") or meta.get("state") or meta.get("phase") or meta.get("progress") or "unknown").lower().strip()
                    group = _status_group(status)
                    updated = _parse_date(meta.get("last_activity") or meta.get("last_touched") or meta.get("last_reviewed") or meta.get("reviewed") or meta.get("updated") or meta.get("modified") or meta.get("created") or meta.get("due") or meta.get("deadline"))
                    if updated is None:
                        try:
                            updated = datetime.fromtimestamp(path.stat().st_mtime).date()
                        except Exception:
                            updated = None
                    days = (date.today() - updated).days if updated else None
                    stale_after = config.default_stale_after_days
                    try:
                        stale_after = int(str(meta.get("stalled_after_days") or meta.get("stale_after_days") or stale_after))
                    except ValueError:
                        pass
                    actions = [a for a in _extract_next_actions(body) if not a.get("done")]
                    title = _title_from_body(path, body, meta)
                    item = {
                        "id": f"markdown:{_safe_rel(path, root)}",
                        "source": self.id,
                        "source_label": self.label,
                        "type": "project",
                        "title": title,
                        "status": status,
                        "status_group": group,
                        "tags": meta.get("tags", []),
                        "updated_at": str(updated) if updated else None,
                        "days_since_activity": days,
                        "stale_after_days": stale_after,
                        "is_stale": bool(group == "active" and days is not None and days >= stale_after),
                        "next_actions": actions[:8],
                        "recommended_action": sorted(actions, key=lambda a: a.get("estimate_minutes") or 9999)[0] if actions else None,
                        "summary": _section_any(body, ["Current state", "Summary", "Context", "Overview", "Status"])[:700],
                        **_path_payload(path, root, config),
                    }
                    items.append(item)
                    if group == "active" and not actions:
                        checks.append(_health_check(f"no-action:{item['id']}", self.id, "warning", "Active item has no next action", f"{title} is active but has no open next action.", work_item_id=item["id"], path=item["relative_path"]))
                    if group == "active" and item["is_stale"]:
                        checks.append(_health_check(f"stale:{item['id']}", self.id, "warning", "Active item is stale", f"{title} has not moved in {days} days.", work_item_id=item["id"], path=item["relative_path"]))
                    for action in actions[:3]:
                        if not action.get("estimate_minutes"):
                            checks.append(_health_check(f"no-estimate:{item['id']}:{len(checks)}", self.id, "info", "Next action has no time estimate", f"{title}: {action['text']}", work_item_id=item["id"], path=item["relative_path"]))
                            break
                    if group == "active" and not _section_any(body, ["Definition of done", "Done when", "Outcome", "Success criteria"]):
                        checks.append(_health_check(f"no-dod:{item['id']}", self.id, "info", "Active item has no definition of done", f"{title} may be harder to finish without a clear done state.", work_item_id=item["id"], path=item["relative_path"]))
        for captures_dir in capture_dirs:
            for path in list(captures_dir.rglob("*.md"))[: config.max_inbox_items + 5]:
                if path.name.lower() == "readme.md":
                    continue
                text = _read_text(path, config, diagnostics)
                meta, body = _parse_frontmatter(text)
                updated = _parse_date(meta.get("updated") or meta.get("created"))
                if updated is None:
                    try:
                        updated = datetime.fromtimestamp(path.stat().st_mtime).date()
                    except Exception:
                        updated = None
                inbox.append({
                    "id": f"inbox:{_safe_rel(path, root)}",
                    "source": self.id,
                    "type": "capture",
                    "title": _title_from_body(path, body, meta),
                    "updated_at": str(updated) if updated else None,
                    "snippet": " ".join(line.strip() for line in body.splitlines() if line.strip())[:260],
                    **_path_payload(path, root, config),
                })
        if len([i for i in items if i["status_group"] == "active"]) > 5:
            checks.append(_health_check("active-count:high", self.id, "info", "Many active work items", "More than 5 active items are marked active. Consider pausing or reclassifying some.", count=len([i for i in items if i["status_group"] == "active"])))
        return self._result("ok" if not diagnostics else "warning", diagnostics, checks, items, inbox[: config.max_inbox_items])

    def _result(self, status: str, diagnostics: list[dict[str, Any]], checks: list[dict[str, Any]], items: list[dict[str, Any]], inbox: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "source": {"id": self.id, "label": self.label, "type": self.type, "status": status, "work_items": len(items), "inbox_items": len(inbox), "health_checks": len(checks), "diagnostics": diagnostics},
            "work_items": items,
            "inbox_items": inbox,
            "health_checks": checks,
        }


class LlmWikiAdapter(Adapter):
    id = "llm-wiki"
    label = "LLM Wiki"
    type = "knowledge"

    def scan(self, config: CommandCenterConfig) -> dict[str, Any]:
        diagnostics: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        root = config.wiki_path
        if root is None:
            diagnostics.append(_health_check("wiki:not-configured", self.id, "info", "No LLM wiki detected", "Set WIKI_PATH or COMMAND_CENTER_WIKI_PATH to enable wiki health checks."))
            return self._result("disabled", diagnostics, checks, items)
        if not root.exists():
            diagnostics.append(_health_check("wiki:missing-root", self.id, "warning", "Wiki path does not exist", f"Expected {root}."))
            return self._result("warning", diagnostics, checks, items)

        schema = root / "SCHEMA.md"
        index = root / "index.md"
        log = root / "log.md"
        for name, path in [("SCHEMA.md", schema), ("index.md", index), ("log.md", log)]:
            if not path.exists():
                checks.append(_health_check(f"wiki:missing:{name}", self.id, "critical", f"Wiki missing {name}", f"LLM wiki conventions expect {name} at the wiki root.", path=name))

        index_text = _read_text(index, config, diagnostics) if index.exists() else ""
        wiki_dirs = [root / "entities", root / "concepts", root / "comparisons", root / "queries"]
        page_paths = [p for d in wiki_dirs if d.exists() for p in d.rglob("*.md")]
        page_names: dict[str, Path] = {}
        inbound: dict[str, int] = {}
        all_links: list[tuple[Path, str]] = []
        for path in page_paths[: config.max_work_items]:
            text = _read_text(path, config, diagnostics)
            if not text:
                continue
            meta, body = _parse_frontmatter(text)
            slug = path.stem
            title = _title_from_body(path, body, meta)
            page_names[slug] = path
            page_names[title] = path
            inbound.setdefault(slug, 0)
            links = _extract_wikilinks(text)
            for link in links:
                all_links.append((path, link))
                inbound[Path(link).stem] = inbound.get(Path(link).stem, 0) + 1
            rel = _safe_rel(path, root)
            line_count = _line_count(text)
            if not meta:
                checks.append(_health_check(f"wiki:no-frontmatter:{rel}", self.id, "warning", "Wiki page missing frontmatter", f"{rel} has no YAML frontmatter.", path=rel))
            if line_count > 200:
                checks.append(_health_check(f"wiki:long-page:{rel}", self.id, "info", "Wiki page may be too long", f"{rel} is {line_count} lines; consider splitting if it is hard to scan.", path=rel))
            if meta.get("confidence") == "low":
                checks.append(_health_check(f"wiki:low-confidence:{rel}", self.id, "info", "Low-confidence wiki page", f"{rel} is marked confidence: low.", path=rel))
            if meta.get("contested") or meta.get("contradictions"):
                checks.append(_health_check(f"wiki:contested:{rel}", self.id, "warning", "Contested wiki page", f"{rel} has contested/contradiction metadata.", path=rel))
            if index_text and path.stem not in index_text and title not in index_text:
                checks.append(_health_check(f"wiki:not-indexed:{rel}", self.id, "warning", "Wiki page missing from index", f"{rel} does not appear to be listed in index.md.", path=rel))

        known_stems = {Path(p).stem for p in page_paths}
        for source_path, link in all_links:
            stem = Path(link).stem
            if stem and stem not in known_stems and not (root / f"{link}.md").exists() and not (root / link).exists():
                rel = _safe_rel(source_path, root)
                checks.append(_health_check(f"wiki:broken-link:{rel}:{stem}", self.id, "warning", "Broken wikilink", f"{rel} links to missing page [[{link}]].", path=rel, link=link))
                if len([c for c in checks if c["title"] == "Broken wikilink"]) >= 25:
                    break

        for path in page_paths[: config.max_work_items]:
            rel = _safe_rel(path, root)
            if inbound.get(path.stem, 0) == 0:
                checks.append(_health_check(f"wiki:orphan:{rel}", self.id, "info", "Orphan wiki page", f"{rel} has no inbound wikilinks from scanned wiki pages.", path=rel))
                if len([c for c in checks if c["title"] == "Orphan wiki page"]) >= 20:
                    break

        if log.exists():
            try:
                log_days = (date.today() - datetime.fromtimestamp(log.stat().st_mtime).date()).days
                if log_days > 14:
                    checks.append(_health_check("wiki:stale-log", self.id, "info", "Wiki log has not changed recently", f"log.md file mtime is {log_days} days old.", path="log.md"))
            except Exception:
                pass

        # Promote top wiki checks into restartable items.
        for check in sorted(checks, key=lambda c: (SEVERITY_ORDER.get(c["severity"], 9), c["title"]))[:8]:
            items.append({
                "id": f"wiki-health:{check['id']}",
                "source": self.id,
                "source_label": self.label,
                "type": "wiki_health",
                "title": check["title"],
                "status": check["severity"],
                "status_group": "active" if check["severity"] in {"critical", "warning"} else "waiting",
                "tags": ["wiki", "health"],
                "updated_at": None,
                "days_since_activity": None,
                "stale_after_days": None,
                "is_stale": False,
                "summary": check["message"],
                "next_actions": [{"text": self._action_for_check(check), "done": False, "estimate_minutes": 10, "source": "wiki-health"}],
                "recommended_action": {"text": self._action_for_check(check), "done": False, "estimate_minutes": 10, "source": "wiki-health"},
                "path": check.get("path") or _safe_rel(root, root),
                "relative_path": check.get("path") or ".",
                "absolute_path": None,
            })
        return self._result("ok" if not diagnostics else "warning", diagnostics, checks, items)

    def _action_for_check(self, check: dict[str, Any]) -> str:
        title = check.get("title", "Wiki health issue")
        path = check.get("path", "the wiki")
        if "missing from index" in title.lower():
            return f"Add `{path}` to index.md with a one-line summary."
        if "broken wikilink" in title.lower():
            return f"Fix or remove the broken wikilink in `{path}`."
        if "orphan" in title.lower():
            return f"Add one relevant inbound/outbound wikilink for `{path}` or decide it should stay isolated."
        if "frontmatter" in title.lower():
            return f"Add required YAML frontmatter to `{path}`."
        if "too long" in title.lower():
            return f"Review `{path}` and split one obvious subtopic if needed."
        return f"Resolve this wiki health check: {check.get('message', title)}"

    def _result(self, status: str, diagnostics: list[dict[str, Any]], checks: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "source": {"id": self.id, "label": self.label, "type": self.type, "status": status, "work_items": len(items), "inbox_items": 0, "health_checks": len(checks), "diagnostics": diagnostics},
            "work_items": items,
            "inbox_items": [],
            "health_checks": checks,
        }


class HermesSessionsAdapter(Adapter):
    id = "hermes-sessions"
    label = "Hermes Sessions"
    type = "session"

    def scan(self, config: CommandCenterConfig) -> dict[str, Any]:
        diagnostics: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        home = config.hermes_home
        db_path = home / "state.db"
        if not db_path.exists():
            diagnostics.append(_health_check("sessions:missing-db", self.id, "info", "Hermes session database not found", f"Expected {db_path}."))
            return self._result("disabled", diagnostics, checks, items)
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT id, source, model, started_at, ended_at, message_count,
                       tool_call_count, input_tokens, output_tokens, title,
                       estimated_cost_usd, api_call_count
                FROM sessions
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (config.session_limit,),
            ).fetchall()
            total_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        except Exception as exc:
            diagnostics.append(_health_check("sessions:read-error", self.id, "warning", "Could not read Hermes sessions", str(exc), path=str(db_path)))
            return self._result("warning", diagnostics, checks, items)

        now = time.time()
        stale_open = 0
        huge_sessions = 0
        for row in rows:
            started = float(row["started_at"] or 0)
            ended = row["ended_at"]
            last_ts = float(ended or started or now)
            days = int((now - last_ts) / 86400) if last_ts else None
            is_open = ended is None
            is_stale = bool(is_open and days is not None and days >= config.session_stale_days)
            if is_stale:
                stale_open += 1
            total_tokens = int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0)
            if total_tokens > 150_000:
                huge_sessions += 1
            title = row["title"] or f"Session {row['id']}"
            action_text = "Review this session and either continue it, summarize it, or close the loop."
            if is_stale:
                action_text = "Resume this stale open session and capture the next action or final summary."
            elif total_tokens > 150_000:
                action_text = "Summarize or compact this large session before continuing."
            item = {
                "id": f"session:{row['id']}",
                "source": self.id,
                "source_label": self.label,
                "type": "hermes_session",
                "title": title,
                "status": "open" if is_open else "recent",
                "status_group": "active" if is_open else "waiting",
                "tags": ["session", str(row["source"] or "unknown")],
                "updated_at": datetime.fromtimestamp(last_ts).astimezone().isoformat(timespec="seconds") if last_ts else None,
                "days_since_activity": days,
                "stale_after_days": config.session_stale_days,
                "is_stale": is_stale,
                "summary": f"{row['source'] or 'unknown'} · {row['model'] or 'model unknown'} · {row['message_count'] or 0} msgs · {row['tool_call_count'] or 0} tools · {total_tokens:,} tokens",
                "path": row["id"],
                "relative_path": row["id"],
                "absolute_path": None,
                "url": f"/sessions/{row['id']}",
                "next_actions": [{"text": action_text, "done": False, "estimate_minutes": 10, "source": "session-health"}],
                "recommended_action": {"text": action_text, "done": False, "estimate_minutes": 10, "source": "session-health"},
            }
            items.append(item)
            if is_stale:
                checks.append(_health_check(f"session:stale:{row['id']}", self.id, "warning", "Stale open Hermes session", f"{title} appears open and has not moved in {days} days.", work_item_id=item["id"], path=row["id"]))
            if total_tokens > 150_000:
                checks.append(_health_check(f"session:large:{row['id']}", self.id, "info", "Large Hermes session", f"{title} has about {total_tokens:,} tokens; summarize/compact before continuing.", work_item_id=item["id"], path=row["id"]))

        if stale_open > 5:
            checks.append(_health_check("sessions:many-stale-open", self.id, "info", "Many stale open sessions", f"{stale_open} recent scanned sessions appear stale/open."))
        try:
            db_size = db_path.stat().st_size
            if db_size > 500_000_000:
                checks.append(_health_check("sessions:large-db", self.id, "info", "Large Hermes session database", f"state.db is {db_size / 1_000_000:.1f} MB."))
        except Exception:
            pass
        status = "ok" if not diagnostics else "warning"
        source_extra = {"total_sessions": total_sessions, "stale_open": stale_open, "large_sessions": huge_sessions}
        return self._result(status, diagnostics, checks, items, source_extra)

    def _result(self, status: str, diagnostics: list[dict[str, Any]], checks: list[dict[str, Any]], items: list[dict[str, Any]], extra: dict[str, Any] | None = None) -> dict[str, Any]:
        source = {"id": self.id, "label": self.label, "type": self.type, "status": status, "work_items": len(items), "inbox_items": 0, "health_checks": len(checks), "diagnostics": diagnostics}
        if extra:
            source.update(extra)
        return {"source": source, "work_items": items, "inbox_items": [], "health_checks": checks}


def _git(args: list[str], cwd: Path, timeout: int = 4) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True, timeout=timeout)


def _find_git_repos(roots: list[Path], max_repos: int) -> list[Path]:
    repos: list[Path] = []
    seen: set[str] = set()
    skip = {".cache", ".cargo", ".git", ".hermes/state", "node_modules", "venv", ".venv", "__pycache__", "dist", "build"}
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        if (root / ".git").exists():
            key = str(root.resolve())
            if key not in seen:
                repos.append(root)
                seen.add(key)
            if len(repos) >= max_repos:
                break
            continue
        try:
            for dirpath, dirnames, _ in os.walk(root):
                current = Path(dirpath)
                dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
                if (current / ".git").exists():
                    key = str(current.resolve())
                    if key not in seen:
                        repos.append(current)
                        seen.add(key)
                    dirnames[:] = []
                    if len(repos) >= max_repos:
                        return repos
        except Exception:
            continue
    return repos


class LocalGitAdapter(Adapter):
    id = "local-git"
    label = "Local Git Repos"
    type = "code"

    def scan(self, config: CommandCenterConfig) -> dict[str, Any]:
        diagnostics: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        roots = [p for p in config.git_roots if p.exists()]
        if not roots:
            diagnostics.append(_health_check("git:no-roots", self.id, "info", "No git scan roots found", "Set COMMAND_CENTER_GIT_ROOTS to comma-separated directories."))
            return self._result("disabled", diagnostics, checks, items)
        repos = _find_git_repos(roots, config.git_max_repos)
        if not repos:
            diagnostics.append(_health_check("git:no-repos", self.id, "info", "No local git repositories found", "No .git directories were found under configured roots."))
            return self._result("disabled", diagnostics, checks, items)

        for repo in repos:
            try:
                status = _git(["status", "--porcelain=v1", "--branch"], repo).stdout.splitlines()
                branch = _git(["branch", "--show-current"], repo).stdout.strip() or "detached"
                commit = _git(["log", "-1", "--format=%ct|%cr|%s"], repo).stdout.strip()
                upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo)
                upstream_name = upstream.stdout.strip() if upstream.returncode == 0 else ""
                ahead = behind = 0
                if upstream_name:
                    counts = _git(["rev-list", "--left-right", "--count", f"{upstream_name}...HEAD"], repo).stdout.strip().split()
                    if len(counts) == 2:
                        behind, ahead = int(counts[0]), int(counts[1])
                changed = [line for line in status if line and not line.startswith("##")]
                dirty_count = len(changed)
                untracked = len([line for line in changed if line.startswith("??")])
                staged = len([line for line in changed if len(line) > 1 and line[0] != " " and not line.startswith("??")])
                modified = len([line for line in changed if len(line) > 1 and line[1] != " "])
                commit_ts = None
                commit_age = None
                commit_subject = "No commits yet"
                commit_rel = "unknown"
                if commit:
                    parts = commit.split("|", 2)
                    if len(parts) == 3:
                        commit_ts = int(parts[0])
                        commit_rel = parts[1]
                        commit_subject = parts[2]
                        commit_age = int((time.time() - commit_ts) / 86400)
                changed_names = [line[3:] for line in changed if len(line) > 3]
                todo_hits = self._todo_hits(repo, changed_names[:25])
                needs_attention = dirty_count > 0 or ahead > 0 or behind > 0 or (commit_age is not None and commit_age >= config.git_stale_days)
                rel = _safe_rel(repo, Path.home())
                action = self._action(branch, dirty_count, ahead, behind, commit_age, config.git_stale_days)
                item = {
                    "id": f"git:{rel}",
                    "source": self.id,
                    "source_label": self.label,
                    "type": "git_repo",
                    "title": repo.name,
                    "status": "needs-attention" if needs_attention else "clean",
                    "status_group": "active" if needs_attention else "waiting",
                    "tags": ["git", "code", branch],
                    "updated_at": datetime.fromtimestamp(commit_ts).astimezone().isoformat(timespec="seconds") if commit_ts else None,
                    "days_since_activity": commit_age,
                    "stale_after_days": config.git_stale_days,
                    "is_stale": bool(commit_age is not None and commit_age >= config.git_stale_days and needs_attention),
                    "summary": f"{branch} · {dirty_count} changed ({staged} staged, {modified} modified, {untracked} untracked) · ahead {ahead} / behind {behind} · last commit {commit_rel}: {commit_subject}",
                    "git": {"branch": branch, "upstream": upstream_name, "dirty_count": dirty_count, "ahead": ahead, "behind": behind, "todo_hits": todo_hits},
                    "next_actions": [{"text": action, "done": False, "estimate_minutes": 10, "source": "git-status"}],
                    "recommended_action": {"text": action, "done": False, "estimate_minutes": 10, "source": "git-status"},
                    **_path_payload(repo, Path.home(), config),
                }
                items.append(item)
                if dirty_count > 0:
                    checks.append(_health_check(f"git:dirty:{rel}", self.id, "warning", "Repo has uncommitted changes", f"{repo.name} has {dirty_count} changed files on {branch}.", work_item_id=item["id"], path=item["relative_path"]))
                if ahead > 0:
                    checks.append(_health_check(f"git:ahead:{rel}", self.id, "warning", "Repo has unpushed commits", f"{repo.name} is {ahead} commit(s) ahead of {upstream_name}.", work_item_id=item["id"], path=item["relative_path"]))
                if behind > 0:
                    checks.append(_health_check(f"git:behind:{rel}", self.id, "info", "Repo is behind upstream", f"{repo.name} is {behind} commit(s) behind {upstream_name}.", work_item_id=item["id"], path=item["relative_path"]))
                if todo_hits:
                    checks.append(_health_check(f"git:todos:{rel}", self.id, "info", "Changed files contain TODO/FIXME", f"{repo.name} has TODO/FIXME markers in changed files.", work_item_id=item["id"], path=item["relative_path"]))
            except Exception as exc:
                diagnostics.append(_health_check(f"git:error:{repo}", self.id, "warning", "Could not scan git repo", f"{repo}: {exc}", path=str(repo)))
        return self._result("ok" if not diagnostics else "warning", diagnostics, checks, items)

    def _action(self, branch: str, dirty_count: int, ahead: int, behind: int, commit_age: int | None, stale_days: int) -> str:
        if dirty_count > 0:
            return f"Review `git diff --stat` on `{branch}` and either commit, stash, or discard one small set of changes."
        if ahead > 0:
            return f"Push `{branch}` or open a PR for the {ahead} unpushed commit(s)."
        if behind > 0:
            return f"Pull/rebase `{branch}` after checking for local risk."
        if commit_age is not None and commit_age >= stale_days:
            return f"Decide whether `{branch}` is still active; archive, update README, or capture the next coding task."
        return f"No action needed; `{branch}` is clean."

    def _todo_hits(self, repo: Path, paths: list[str]) -> list[str]:
        hits: list[str] = []
        for name in paths:
            path = repo / name
            if not path.exists() or not path.is_file() or path.stat().st_size > 200_000:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if re.search(r"\b(TODO|FIXME|HACK)\b", text):
                hits.append(name)
            if len(hits) >= 8:
                break
        return hits

    def _result(self, status: str, diagnostics: list[dict[str, Any]], checks: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "source": {"id": self.id, "label": self.label, "type": self.type, "status": status, "work_items": len(items), "inbox_items": 0, "health_checks": len(checks), "diagnostics": diagnostics},
            "work_items": items,
            "inbox_items": [],
            "health_checks": checks,
        }


def _scan_all(config: CommandCenterConfig) -> dict[str, Any]:
    adapters: list[Adapter] = []
    if config.enable_project_adapter:
        adapters.append(MarkdownProjectAdapter())
    if config.enable_wiki_adapter:
        adapters.append(LlmWikiAdapter())
    if config.enable_sessions_adapter:
        adapters.append(HermesSessionsAdapter())
    if config.enable_git_adapter:
        adapters.append(LocalGitAdapter())

    sources: list[dict[str, Any]] = []
    work_items: list[dict[str, Any]] = []
    inbox_items: list[dict[str, Any]] = []
    health_checks: list[dict[str, Any]] = []
    for adapter in adapters:
        result = adapter.scan(config)
        sources.append(result["source"])
        work_items.extend(result["work_items"])
        inbox_items.extend(result["inbox_items"])
        health_checks.extend(result["health_checks"])

    work_items.sort(key=lambda item: (
        0 if item.get("status_group") == "active" else 1,
        0 if item.get("is_stale") else 1,
        item.get("days_since_activity") if item.get("days_since_activity") is not None else 9999,
        item.get("title", "").lower(),
    ))
    health_checks.sort(key=lambda c: (SEVERITY_ORDER.get(c.get("severity"), 9), c.get("source", ""), c.get("title", "")))
    inbox_items.sort(key=lambda i: i.get("updated_at") or "", reverse=True)

    restart_scripts = _build_restart_scripts(work_items, health_checks, config)
    return {
        "sources": sources,
        "work_items": work_items,
        "inbox_items": inbox_items,
        "health_checks": health_checks,
        "restart_scripts": restart_scripts,
    }


def _build_restart_scripts(work_items: list[dict[str, Any]], health_checks: list[dict[str, Any]], config: CommandCenterConfig) -> list[dict[str, Any]]:
    scripts: list[dict[str, Any]] = []

    def add_items(predicate, reason_fn, limit: int) -> None:
        for item in work_items:
            if len(scripts) >= limit:
                return
            if not predicate(item) or not item.get("recommended_action"):
                continue
            scripts.append(_restart_script(item, item.get("recommended_action"), config, reason_fn(item)))

    # Strong structured signals first: git state is factual and immediately actionable.
    add_items(
        lambda i: i.get("type") == "git_repo" and ((i.get("git") or {}).get("dirty_count", 0) > 0 or (i.get("git") or {}).get("ahead", 0) > 0),
        lambda i: "local git state needs cleanup",
        4,
    )
    # Then sessions: useful but often needs summarization rather than direct implementation.
    add_items(
        lambda i: i.get("type") == "hermes_session" and (i.get("is_stale") or "tokens" in (i.get("summary") or "")),
        lambda i: "session needs summarizing or closure",
        6,
    )
    # Then freeform markdown. These are weaker until onboarding/LLM enrichment is added.
    add_items(
        lambda i: i.get("type") == "project" and i.get("status_group") == "active",
        lambda i: "stale markdown work" if i.get("is_stale") else "markdown work with a concrete next action",
        8,
    )
    # Finally wiki health items.
    add_items(
        lambda i: i.get("type") == "wiki_health",
        lambda i: "source health issue",
        8,
    )
    return scripts[:8]


def _pick_start_here(restart_scripts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not restart_scripts:
        return None
    def score(script: dict[str, Any]) -> tuple[int, str]:
        reason = script.get("reason", "")
        if "git" in reason:
            return (0, script.get("title", ""))
        if "session" in reason:
            return (1, script.get("title", ""))
        if "markdown" in reason:
            return (2, script.get("title", ""))
        return (3, script.get("title", ""))
    return sorted(restart_scripts, key=score)[0]


def _config_payload(config: CommandCenterConfig) -> dict[str, Any]:
    return {
        "workspace_name": config.workspace_name,
        "workspace_root": str(config.workspace_root) if config.workspace_root else None,
        "projects_dir": config.projects_dir,
        "captures_dir": config.captures_dir,
        "wiki_path": str(config.wiki_path) if config.wiki_path else None,
        "default_stale_after_days": config.default_stale_after_days,
        "focus_window_minutes": config.focus_window_minutes,
        "expose_absolute_paths": config.expose_absolute_paths,
        "hermes_home": str(config.hermes_home) if config.hermes_home else None,
        "session_limit": config.session_limit,
        "session_stale_days": config.session_stale_days,
        "git_roots": [str(p) for p in config.git_roots],
        "git_max_repos": config.git_max_repos,
        "git_stale_days": config.git_stale_days,
        "config_path": str(config.config_path) if config.config_path else None,
        "config_source": config.config_source,
        "setup_needed": config.setup_needed,
    }


@router.get("/data")
async def command_center_data():
    config = _load_config()
    scan = _scan_all(config)
    work_items = scan["work_items"]
    health_checks = scan["health_checks"]
    restart_scripts = scan["restart_scripts"]
    active = [i for i in work_items if i.get("status_group") == "active"]
    waiting = [i for i in work_items if i.get("status_group") == "waiting"]
    paused = [i for i in work_items if i.get("status_group") == "paused"]
    done = [i for i in work_items if i.get("status_group") == "done"]
    stale = [i for i in work_items if i.get("is_stale")]
    critical = [c for c in health_checks if c.get("severity") == "critical"]
    warnings = [c for c in health_checks if c.get("severity") == "warning"]
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "schema_version": 3,
        "mode": "hybrid-ready-adapters",
        "config": _config_payload(config),
        "counts": {
            "sources": len(scan["sources"]),
            "work_items": len(work_items),
            "active": len(active),
            "waiting": len(waiting),
            "paused": len(paused),
            "done": len(done),
            "stale": len(stale),
            "inbox": len(scan["inbox_items"]),
            "health_checks": len(health_checks),
            "critical": len(critical),
            "warnings": len(warnings),
            "restart_scripts": len(restart_scripts),
            # Back-compat for old UI callers.
            "projects": len(work_items),
            "stalled": len(stale),
            "captures": len(scan["inbox_items"]),
        },
        "start_here": _pick_start_here(restart_scripts),
        "start_task": _pick_start_here(restart_scripts),
        "sources": scan["sources"],
        "work_items": work_items,
        "projects": work_items,
        "inbox_items": scan["inbox_items"],
        "captures": scan["inbox_items"],
        "health_checks": health_checks[:100],
        "restart_scripts": restart_scripts,
        "ops": {
            "plugin": "command-center",
            "mode": "read-only hybrid-ready adapter MVP",
            "profile": os.getenv("HERMES_ACTIVE_PROFILE", "default"),
            "normalization": "deterministic for structured sources; markdown/wiki are weak signals until LLM onboarding/enrichment is enabled",
            "setup_needed": config.setup_needed,
            "setup_prompt": _setup_prompt(config),
        },
    }


@router.get("/setup-prompt")
async def command_center_setup_prompt():
    config = _load_config()
    return {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "config": _config_payload(config), "prompt": _setup_prompt(config)}


@router.get("/sources")
async def command_center_sources():
    config = _load_config()
    scan = _scan_all(config)
    return {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "config": _config_payload(config), "sources": scan["sources"]}
