"""Configuration loader (.env) and local installation state management for devclone."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
USER_CONFIG_ENV_FILE = Path.home() / ".config" / "devclone" / "config.env"


def _load_dotenv_file(path: Path) -> Dict[str, str]:
    """Parse a simple .env file into a dictionary."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if v and v[0] not in ('"', "'") and " #" in v:
                v = v.split(" #", 1)[0].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            values[k] = v
    except Exception:
        pass
    return values


def get_active_env_file() -> Path:
    """Return the path of the active configuration .env file."""
    custom = os.environ.get("DEVCLONE_CONFIG_FILE") or os.environ.get("PSYNC_CONFIG_FILE")
    if custom and Path(custom).expanduser().exists():
        return Path(custom).expanduser()
    if DEFAULT_ENV_FILE.exists():
        return DEFAULT_ENV_FILE
    if USER_CONFIG_ENV_FILE.exists():
        return USER_CONFIG_ENV_FILE
    return DEFAULT_ENV_FILE


_file_cfg: Dict[str, str] = {}
if USER_CONFIG_ENV_FILE.exists():
    _file_cfg.update(_load_dotenv_file(USER_CONFIG_ENV_FILE))
if DEFAULT_ENV_FILE.exists():
    _file_cfg.update(_load_dotenv_file(DEFAULT_ENV_FILE))
custom_env = os.environ.get("DEVCLONE_CONFIG_FILE") or os.environ.get("PSYNC_CONFIG_FILE")
if custom_env and Path(custom_env).expanduser().exists():
    _file_cfg.update(_load_dotenv_file(Path(custom_env).expanduser()))


def get_cfg(key: str, default: str = "") -> str:
    """
    Retrieve config value from process environment first, then .env file, then default.
    Supports both DEVCLONE_* and legacy PSYNC_* variable prefixes.
    """
    legacy_key = key.replace("DEVCLONE_", "PSYNC_", 1)
    if key in os.environ:
        return os.environ[key]
    if legacy_key in os.environ:
        return os.environ[legacy_key]
    if key in _file_cfg:
        return _file_cfg[key]
    if legacy_key in _file_cfg:
        return _file_cfg[legacy_key]
    return default


REMOTE_USER = get_cfg("DEVCLONE_SSH_USER", os.environ.get("USER", "developer"))
REMOTE_IP = get_cfg("DEVCLONE_SSH_HOST", "192.168.1.100")
REMOTE_PORT = int(get_cfg("DEVCLONE_SSH_PORT", "22") or "22")
SSH_KEY_PATH = get_cfg("DEVCLONE_SSH_KEY", "")
SSH_CONNECT_TIMEOUT = int(get_cfg("DEVCLONE_SSH_CONNECT_TIMEOUT", "8") or "8")

REMOTE_HOST = get_cfg("DEVCLONE_SSH_TARGET", "") or (
    f"{REMOTE_USER}@{REMOTE_IP}" if REMOTE_USER else REMOTE_IP
)

REMOTE_PROJECTS_DIR = get_cfg("DEVCLONE_REMOTE_PROJECTS_DIR", "~/Projects").rstrip("/")
LOCAL_PROJECTS_DIR = Path(
    get_cfg("DEVCLONE_LOCAL_PROJECTS_DIR", str(Path.home() / "Projects"))
).expanduser()

STATE_FILE = Path(
    get_cfg("DEVCLONE_STATE_FILE", str(Path.home() / ".config" / "devclone" / "state.json"))
).expanduser()
CONFIG_DIR = STATE_FILE.parent

# Helper image used on Desktop/Laptop with tar & gzip for read-only (:ro) Docker volume streaming
REMOTE_HELPER_IMAGE = get_cfg("DEVCLONE_REMOTE_HELPER_IMAGE", "redis:alpine")
LOCAL_HELPER_IMAGE = get_cfg("DEVCLONE_LOCAL_HELPER_IMAGE", "redis:alpine")

MAX_EPHEMERAL_SQL_MB = int(get_cfg("DEVCLONE_MAX_EPHEMERAL_SQL_MB", "4500") or "4500")
SKIP_ARCHIVE_MIN_MB = int(get_cfg("DEVCLONE_SKIP_ARCHIVE_MIN_MB", "25") or "25")

ARCHIVE_EXCLUDE_PATTERNS = [
    p.strip()
    for p in get_cfg(
        "DEVCLONE_ARCHIVE_EXCLUDE_PATTERNS",
        "backup.zip,*-backup.zip,*.tar.gz,*.sql.gz",
    ).split(",")
    if p.strip()
]


def ssh_cmd_args() -> List[str]:
    """Build standard ssh command prefix respecting configured port, key, and timeout."""
    args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
    ]
    if REMOTE_PORT and REMOTE_PORT != 22:
        args.extend(["-p", str(REMOTE_PORT)])
    if SSH_KEY_PATH and Path(SSH_KEY_PATH).expanduser().exists():
        args.extend([
            "-i",
            str(Path(SSH_KEY_PATH).expanduser()),
            "-o",
            "IdentityAgent=none",
            "-o",
            "IdentitiesOnly=yes",
        ])
    args.append(REMOTE_HOST)
    return args


def rsync_ssh_option() -> List[str]:
    """Return ['-e', 'ssh ...'] option list for rsync if non-default SSH options are configured."""
    parts = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}"]
    if REMOTE_PORT and REMOTE_PORT != 22:
        parts.extend(["-p", str(REMOTE_PORT)])
    if SSH_KEY_PATH and Path(SSH_KEY_PATH).expanduser().exists():
        parts.extend([
            "-i",
            str(Path(SSH_KEY_PATH).expanduser()),
            "-o",
            "IdentityAgent=none",
            "-o",
            "IdentitiesOnly=yes",
        ])
    return ["-e", " ".join(parts)]


def get_all_config_summary() -> Dict[str, str]:
    """Return dictionary of active configuration settings for `devclone config`."""
    return {
        "CONFIG_FILE": str(get_active_env_file()),
        "DEVCLONE_SSH_USER": REMOTE_USER,
        "DEVCLONE_SSH_HOST": REMOTE_IP,
        "DEVCLONE_SSH_PORT": str(REMOTE_PORT),
        "DEVCLONE_SSH_TARGET": REMOTE_HOST,
        "DEVCLONE_SSH_KEY": SSH_KEY_PATH or "(default ~/.ssh/config)",
        "DEVCLONE_SSH_CONNECT_TIMEOUT": f"{SSH_CONNECT_TIMEOUT}s",
        "DEVCLONE_REMOTE_PROJECTS_DIR": REMOTE_PROJECTS_DIR,
        "DEVCLONE_LOCAL_PROJECTS_DIR": str(LOCAL_PROJECTS_DIR),
        "DEVCLONE_STATE_FILE": str(STATE_FILE),
        "DEVCLONE_REMOTE_HELPER_IMAGE": REMOTE_HELPER_IMAGE,
        "DEVCLONE_LOCAL_HELPER_IMAGE": LOCAL_HELPER_IMAGE,
        "DEVCLONE_MAX_EPHEMERAL_SQL_MB": f"{MAX_EPHEMERAL_SQL_MB} MB",
        "DEVCLONE_SKIP_ARCHIVE_MIN_MB": f"{SKIP_ARCHIVE_MIN_MB} MB",
        "DEVCLONE_ARCHIVE_EXCLUDE_PATTERNS": ", ".join(ARCHIVE_EXCLUDE_PATTERNS),
    }


def load_state() -> Dict[str, Any]:
    """Load the local devclone state file."""
    if not STATE_FILE.exists():
        return {"installed_projects": {}}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if "installed_projects" not in data:
                data["installed_projects"] = {}
            return data
    except Exception:
        return {"installed_projects": {}}


def save_state(state: Dict[str, Any]) -> None:
    """Persist the local devclone state file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def is_project_installed(project_name: str) -> bool:
    """
    Return True if the project is already set up on this machine.
    Checks both the state registry and the local filesystem directory
    (<LOCAL_PROJECTS_DIR>/<project_name>) to enforce the one-time setup rule.
    """
    state = load_state()
    if project_name in state.get("installed_projects", {}):
        local_dir = LOCAL_PROJECTS_DIR / project_name
        if local_dir.exists():
            return True

    local_dir = LOCAL_PROJECTS_DIR / project_name
    if local_dir.exists() and any(local_dir.iterdir()):
        return True
    return False


def mark_project_installed(
    project_name: str,
    stack: str,
    volumes_cloned: list[str],
    local_domains: list[str],
    ports: list[str],
    git_commit: Optional[str] = None,
) -> None:
    """Record a project as installed on this machine."""
    state = load_state()
    state.setdefault("installed_projects", {})[project_name] = {
        "installed_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "remote_path": f"{REMOTE_PROJECTS_DIR}/{project_name}",
        "local_path": str(LOCAL_PROJECTS_DIR / project_name),
        "stack": stack,
        "volumes_cloned": volumes_cloned,
        "local_domains": local_domains,
        "ports": ports,
        "git_commit_at_setup": git_commit,
    }
    save_state(state)
