"""Remote and local project discovery, Docker Compose / .env parsing, and volume mapping."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml

from .config import (
    LOCAL_PROJECTS_DIR,
    REMOTE_HOST,
    REMOTE_PROJECTS_DIR,
    SKIP_ARCHIVE_MIN_MB,
    is_project_installed,
    ssh_cmd_args,
)


@dataclass
class ContainerInfo:
    name: str
    image: str
    state: str  # e.g. "running", "exited"
    status: str  # e.g. "Up 2 hours", "Exited (0) 2 hours ago"
    compose_project: str
    compose_service: str
    working_dir: str
    ports: str = ""


@dataclass
class VolumeInfo:
    name: str
    compose_project: str
    compose_volume: str
    size_human: str = "0B"
    size_bytes: int = 0


@dataclass
class DBConfig:
    service_name: str
    container_name: str
    image: str
    state: str  # "running", "exited", "missing"
    volume_name: Optional[str]
    bind_mount_dir: Optional[str]
    database: str
    username: str
    password: str
    root_password: str
    host_port: Optional[str] = None


@dataclass
class ProjectInfo:
    name: str
    remote_path: str
    local_path: Path
    size_human: str
    size_bytes: int
    has_docker: bool
    compose_file: Optional[str]  # filename e.g. "docker-compose.yml" or "compose.yaml"
    compose_data: Dict[str, Any] = field(default_factory=dict)
    compose_project_name: str = ""
    stack: str = "Standalone"
    env_vars: Dict[str, str] = field(default_factory=dict)
    has_env: bool = False
    has_git: bool = False
    git_branch: str = ""
    git_commit: str = ""
    containers: List[ContainerInfo] = field(default_factory=list)
    volumes: List[VolumeInfo] = field(default_factory=list)
    db_config: Optional[DBConfig] = None
    host_ports: Dict[str, str] = field(default_factory=dict)  # service -> "host_port->container_port"
    local_domains: List[str] = field(default_factory=list)
    large_archives: List[Dict[str, Any]] = field(default_factory=list)
    has_tunnel: bool = False
    installed_locally: bool = False
    local_containers: List[ContainerInfo] = field(default_factory=list)
    local_volumes: List[str] = field(default_factory=list)

    @property
    def total_volume_size_bytes(self) -> int:
        return sum(v.size_bytes for v in self.volumes)

    @property
    def total_volume_size_human(self) -> str:
        if not self.volumes:
            if self.db_config and self.db_config.bind_mount_dir:
                return "bind-mount"
            return "-"
        return format_bytes(self.total_volume_size_bytes)

    @property
    def desktop_state_summary(self) -> str:
        if not self.has_docker:
            return "-"
        if not self.containers:
            return "No containers"
        running = sum(1 for c in self.containers if c.state == "running")
        total = len(self.containers)
        if running == total:
            return f"Up ({running}/{total})"
        if running > 0:
            return f"Partial ({running}/{total} Up)"
        return f"Exited ({total})"

    @property
    def laptop_state_summary(self) -> str:
        if not self.installed_locally:
            return "Available"
        if not self.has_docker:
            return "Installed"
        if not self.local_containers:
            return "Installed (Stopped)"
        running = sum(1 for c in self.local_containers if c.state == "running")
        total = len(self.local_containers)
        if running > 0:
            return f"Installed ({running}/{total} Up)"
        return "Installed (Exited)"


def format_bytes(num_bytes: int) -> str:
    """Format byte count into human-readable string."""
    if num_bytes <= 0:
        return "0 B"
    val = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if val < 1024.0 or unit == "TB":
            if unit in ("B", "KB"):
                return f"{int(val)} {unit}"
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} TB"


def parse_size_to_bytes(size_str: str) -> int:
    """Parse Docker size string like '213.2MB', '3.932GB', '326.6kB', '88B' to bytes."""
    s = size_str.strip()
    if not s or s == "0B":
        return 0
    m = re.match(r"^([0-9.]+)\s*([a-zA-Z]+)$", s)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2).upper()
    mult = {
        "B": 1,
        "KB": 1000,
        "KIB": 1024,
        "K": 1024,
        "MB": 1000**2,
        "MIB": 1024**2,
        "M": 1024**2,
        "GB": 1000**3,
        "GIB": 1024**3,
        "G": 1024**3,
        "TB": 1000**4,
        "TIB": 1024**4,
        "T": 1024**4,
    }.get(unit, 1)
    return int(num * mult)


def parse_env_content(content: str) -> Dict[str, str]:
    """Parse a .env file content into a dictionary of key -> value."""
    env: Dict[str, str] = {}
    for raw_line in content.splitlines():
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
        # Strip inline comments if unquoted
        if v and v[0] not in ('"', "'") and " #" in v:
            v = v.split(" #", 1)[0].strip()
        # Strip matching quotes
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        env[k] = v
    # Resolve simple ${VAR} references within .env itself
    for k, v in list(env.items()):
        env[k] = interpolate_env(v, env)
    return env


def interpolate_env(val: str, env: Dict[str, str]) -> str:
    """Resolve ${VAR:-default} and ${VAR} expressions using env dict."""
    if not isinstance(val, str) or "$" not in val:
        return str(val)

    def repl(match: re.Match) -> str:
        inner = match.group(1) or match.group(2)
        if ":-" in inner:
            var_name, default = inner.split(":-", 1)
            res = env.get(var_name.strip(), "")
            return res if res != "" else default
        if "-" in inner:
            var_name, default = inner.split("-", 1)
            return env.get(var_name.strip(), default)
        return env.get(inner.strip(), "")

    return re.sub(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)", repl, val)


REMOTE_DISCOVERY_PYTHON = r"""
import os, sys, json, subprocess, glob
from concurrent.futures import ThreadPoolExecutor

PROJ_DIR = os.path.expanduser(__REMOTE_PROJECTS_DIR__)
SKIP_MIN_BYTES = __SKIP_ARCHIVE_MIN_BYTES__
result = {
    "projects": {},
    "containers": [],
    "volumes": {},
    "hosts": []
}

def fetch_hosts():
    hosts = []
    try:
        with open("/etc/hosts", "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[0] in ("127.0.0.1", "0.0.0.0"):
                    for h in parts[1:]:
                        if h not in ("localhost", "kubernetes.docker.internal") and not h.startswith("#"):
                            hosts.append(h)
    except Exception:
        pass
    return hosts

def fetch_containers():
    conts = []
    try:
        fmt = "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}\t{{.Ports}}\t{{.Label \"com.docker.compose.project\"}}\t{{.Label \"com.docker.compose.service\"}}\t{{.Label \"com.docker.compose.project.working_dir\"}}"
        out = subprocess.run(["docker", "ps", "-a", "--format", fmt], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 8:
                conts.append({
                    "name": parts[0],
                    "image": parts[1],
                    "state": parts[2],
                    "status": parts[3],
                    "ports": parts[4],
                    "compose_project": parts[5],
                    "compose_service": parts[6],
                    "working_dir": parts[7],
                })
    except Exception:
        pass
    return conts

def fetch_volumes():
    vol_sizes = {}
    vols = {}
    try:
        df_out = subprocess.run(["docker", "system", "df", "-v"], capture_output=True, text=True).stdout
        in_vols = False
        for line in df_out.splitlines():
            if line.startswith("VOLUME NAME"):
                in_vols = True
                continue
            if in_vols:
                if not line.strip() or line.startswith("Build cache"):
                    break
                parts = line.split()
                if len(parts) >= 3:
                    vol_sizes[parts[0]] = parts[2]
    except Exception:
        pass

    try:
        vol_names = subprocess.run(["docker", "volume", "ls", "-q"], capture_output=True, text=True).stdout.splitlines()
        if vol_names:
            insp_out = subprocess.run(["docker", "volume", "inspect"] + vol_names, capture_output=True, text=True).stdout
            for v in json.loads(insp_out):
                vname = v.get("Name", "")
                labels = v.get("Labels") or {}
                vols[vname] = {
                    "name": vname,
                    "compose_project": labels.get("com.docker.compose.project", ""),
                    "compose_volume": labels.get("com.docker.compose.volume", ""),
                    "size_human": vol_sizes.get(vname, "0B"),
                }
    except Exception:
        pass
    return vols

def fetch_dir_sizes():
    dir_sizes = {}
    try:
        subdirs = [os.path.join(PROJ_DIR, d) for d in os.listdir(PROJ_DIR) if os.path.isdir(os.path.join(PROJ_DIR, d)) and not d.startswith(".")]
        if subdirs:
            # Run du -sk ignoring permission denied return code (e.g. root-owned bind mounts)
            du_out = subprocess.run(["du", "-sk"] + subdirs, capture_output=True, text=True).stdout
            for line in du_out.splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    dir_sizes[os.path.basename(parts[1].rstrip("/"))] = int(parts[0]) * 1024
    except Exception:
        pass
    return dir_sizes

with ThreadPoolExecutor(max_workers=4) as ex:
    f_hosts = ex.submit(fetch_hosts)
    f_conts = ex.submit(fetch_containers)
    f_vols = ex.submit(fetch_volumes)
    f_sizes = ex.submit(fetch_dir_sizes)
    result["hosts"] = f_hosts.result()
    result["containers"] = f_conts.result()
    result["volumes"] = f_vols.result()
    dir_sizes = f_sizes.result()


# Scan each project folder
compose_candidates = ["docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"]
if os.path.isdir(PROJ_DIR):
    for entry in sorted(os.listdir(PROJ_DIR)):
        pdir = os.path.join(PROJ_DIR, entry)
        if not os.path.isdir(pdir) or entry.startswith("."):
            continue
        info = {
            "name": entry,
            "remote_path": pdir,
            "size_bytes": dir_sizes.get(entry, 0),
            "compose_file": None,
            "compose_content": None,
            "env_content": None,
            "has_git": os.path.isdir(os.path.join(pdir, ".git")),
            "git_branch": "",
            "git_commit": "",
            "markers": [],
            "large_archives": [],
        }
        for cf in compose_candidates:
            cf_path = os.path.join(pdir, cf)
            if os.path.isfile(cf_path):
                info["compose_file"] = cf
                try:
                    with open(cf_path, "r", errors="replace") as f:
                        info["compose_content"] = f.read()
                except Exception:
                    pass
                break

        env_path = os.path.join(pdir, ".env")
        if os.path.isfile(env_path):
            try:
                with open(env_path, "r", errors="replace") as f:
                    info["env_content"] = f.read()
            except Exception:
                pass

        if info["has_git"]:
            try:
                info["git_branch"] = subprocess.check_output(
                    ["git", "-C", pdir, "rev-parse", "--abbrev-ref", "HEAD"],
                    text=True, stderr=subprocess.DEVNULL
                ).strip()
                info["git_commit"] = subprocess.check_output(
                    ["git", "-C", pdir, "rev-parse", "--short", "HEAD"],
                    text=True, stderr=subprocess.DEVNULL
                ).strip()
            except Exception:
                pass

        # Detect project stack markers
        for m in ["artisan", "wp-config.php", "wp-login.php", "html/wp-login.php", "src/wp-login.php",
                  "composer.json", "package.json", "next.config.mjs", "Cargo.toml", "requirements.txt",
                  "main.py", "functions.php", "style.css", "Dockerfile", "docker-volumes/mysql"]:
            if os.path.exists(os.path.join(pdir, m)):
                info["markers"].append(m)

        # Check top-level large archives (> SKIP_MIN_BYTES) and archive-only folders
        try:
            entries = os.listdir(pdir)
            has_any_archive = False
            for fn in entries:
                fpath = os.path.join(pdir, fn)
                if os.path.isfile(fpath) and fn.endswith((".zip", ".tar.gz", ".tgz", ".sql", ".sql.gz")):
                    has_any_archive = True
                    sz = os.path.getsize(fpath)
                    if sz > SKIP_MIN_BYTES and fn.endswith((".zip", ".tar.gz", ".tgz")):
                        info["large_archives"].append({"name": fn, "size_bytes": sz})
            if has_any_archive and not info["markers"]:
                info["markers"].append("__archive_dir__")
        except Exception:
            pass

        result["projects"][entry] = info

print(json.dumps(result))
"""


def get_local_docker_containers() -> List[ContainerInfo]:
    """Return all Docker containers on this machine."""
    containers: List[ContainerInfo] = []
    try:
        fmt = '{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}\t{{.Ports}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}\t{{.Label "com.docker.compose.project.working_dir"}}'
        out = subprocess.check_output(
            ["docker", "ps", "-a", "--format", fmt],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 8:
                containers.append(
                    ContainerInfo(
                        name=parts[0],
                        image=parts[1],
                        state=parts[2],
                        status=parts[3],
                        ports=parts[4],
                        compose_project=parts[5],
                        compose_service=parts[6],
                        working_dir=parts[7],
                    )
                )
    except Exception:
        pass
    return containers


def get_local_docker_volumes() -> List[str]:
    """Return list of Docker volume names on this machine."""
    try:
        out = subprocess.check_output(
            ["docker", "volume", "ls", "-q"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def classify_stack(
    name: str,
    has_docker: bool,
    compose_data: Dict[str, Any],
    markers: List[str],
    large_archives: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Classify a project's stack based on compose services and file markers."""
    services = (compose_data or {}).get("services") or {}
    if has_docker and services:
        svc_names = set(services.keys())
        images = " ".join(str((s or {}).get("image", "")) for s in services.values()).lower()
        if "laravel.test" in svc_names or "sail" in images:
            php_ver = ""
            for s in services.values():
                img = str((s or {}).get("image", ""))
                m = re.search(r"sail-(\d+\.\d+)", img)
                if m:
                    php_ver = f" PHP {m.group(1)}"
                    break
            return f"Laravel Sail{php_ver}"
        if "wordpress" in svc_names or "wordpress" in images:
            if "mariadb" in images:
                return "WordPress + MariaDB"
            return "WordPress + MySQL"
        if "artisan" in markers:
            return "Laravel Docker"
        if "src/wp-login.php" in markers or "html/wp-login.php" in markers:
            return "WordPress Docker"
        return "Custom Docker"

    if "artisan" in markers:
        return "Laravel (No Compose)"
    if "next.config.mjs" in markers:
        return "Next.js"
    if "Cargo.toml" in markers:
        return "Rust"
    if "requirements.txt" in markers or "main.py" in markers:
        return "Python"
    if "package.json" in markers:
        return "Node.js"
    if "wp-config.php" in markers or ("functions.php" in markers and "style.css" in markers):
        return "WP Theme/Plugin"
    if "composer.json" in markers:
        return "PHP"
    if "__archive_dir__" in markers or large_archives or "backup" in name.lower():
        return "Backups / Archives"
    return "Static / Misc"


def extract_db_config(
    project_name: str,
    compose_data: Dict[str, Any],
    env_vars: Dict[str, str],
    containers: List[ContainerInfo],
    volumes: List[VolumeInfo],
    markers: List[str],
) -> Optional[DBConfig]:
    """Identify the database service, container, credentials, and volume for a Docker project."""
    services = (compose_data or {}).get("services") or {}
    if not services:
        return None

    db_svc_name = None
    db_svc = None
    for sname, sdef in services.items():
        if not isinstance(sdef, dict):
            continue
        img = str(sdef.get("image", "")).lower()
        if sname in ("mysql", "db", "mariadb") or "mysql" in img or "mariadb" in img or sname.endswith("-db"):
            db_svc_name = sname
            db_svc = sdef
            break

    if not db_svc_name or not db_svc:
        return None

    image = interpolate_env(str(db_svc.get("image", "mysql")), env_vars)

    # Find matching container
    matched_container: Optional[ContainerInfo] = None
    for c in containers:
        if c.compose_service == db_svc_name or c.name.endswith(f"_{db_svc_name}") or c.name.endswith(f"-{db_svc_name}-1"):
            matched_container = c
            break

    container_name = (
        matched_container.name
        if matched_container
        else interpolate_env(str(db_svc.get("container_name", f"{project_name}-{db_svc_name}-1")), env_vars)
    )
    state = matched_container.state if matched_container else "exited"

    # Parse environment variables on the db service
    raw_env = db_svc.get("environment") or {}
    svc_env: Dict[str, str] = {}
    if isinstance(raw_env, dict):
        for k, v in raw_env.items():
            svc_env[str(k)] = interpolate_env(str(v if v is not None else ""), env_vars)
    elif isinstance(raw_env, list):
        for item in raw_env:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                svc_env[k.strip()] = interpolate_env(v.strip(), env_vars)

    database = (
        svc_env.get("MYSQL_DATABASE")
        or env_vars.get("DB_DATABASE")
        or env_vars.get("MYSQL_DATABASE")
        or "laravel"
    )
    username = (
        svc_env.get("MYSQL_USER")
        or env_vars.get("DB_USERNAME")
        or env_vars.get("MYSQL_USER")
        or "root"
    )
    password = (
        svc_env.get("MYSQL_PASSWORD")
        or env_vars.get("DB_PASSWORD")
        or env_vars.get("MYSQL_PASSWORD")
        or ""
    )
    root_password = (
        svc_env.get("MYSQL_ROOT_PASSWORD")
        or env_vars.get("MYSQL_ROOT_PASSWORD")
        or env_vars.get("DB_PASSWORD")
        or password
    )

    # Identify DB volume or bind mount
    vol_name: Optional[str] = None
    bind_mount: Optional[str] = None
    svc_vols = db_svc.get("volumes") or []
    for v_entry in svc_vols:
        v_str = str(v_entry)
        if ":/var/lib/mysql" in v_str:
            src = v_str.split(":/var/lib/mysql")[0].strip()
            if src.startswith(".") or src.startswith("/"):
                bind_mount = src
            else:
                # Match against discovered volumes
                for vol in volumes:
                    if vol.compose_volume == src or vol.name.endswith(f"_{src}") or vol.name == src:
                        vol_name = vol.name
                        break
                if not vol_name:
                    vol_name = f"{project_name}_{src}"

    # Extract host port if mapped
    host_port = None
    for p in db_svc.get("ports") or []:
        p_resolved = interpolate_env(str(p), env_vars)
        if ":" in p_resolved:
            hp = p_resolved.rsplit(":", 1)[0].split(":")[-1]
            host_port = hp

    return DBConfig(
        service_name=db_svc_name,
        container_name=container_name,
        image=image,
        state=state,
        volume_name=vol_name,
        bind_mount_dir=bind_mount,
        database=database,
        username=username,
        password=password,
        root_password=root_password,
        host_port=host_port,
    )


def discover_projects() -> List[ProjectInfo]:
    """
    Scan REMOTE_HOST:REMOTE_PROJECTS_DIR in one batched SSH call
    and combine with local laptop state.
    """
    script = (
        REMOTE_DISCOVERY_PYTHON
        .replace("__REMOTE_PROJECTS_DIR__", repr(REMOTE_PROJECTS_DIR))
        .replace("__SKIP_ARCHIVE_MIN_BYTES__", str(SKIP_ARCHIVE_MIN_MB * 1024 * 1024))
    )
    proc = subprocess.run(
        ssh_cmd_args() + ["python3 -"],
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    raw = json.loads(proc.stdout)

    remote_containers = [
        ContainerInfo(
            name=c["name"],
            image=c["image"],
            state=c["state"],
            status=c["status"],
            ports=c.get("ports", ""),
            compose_project=c["compose_project"],
            compose_service=c["compose_service"],
            working_dir=c["working_dir"],
        )
        for c in raw.get("containers", [])
    ]

    remote_volumes: Dict[str, VolumeInfo] = {}
    for vname, v in raw.get("volumes", {}).items():
        sz_human = v.get("size_human", "0B")
        remote_volumes[vname] = VolumeInfo(
            name=vname,
            compose_project=v.get("compose_project", ""),
            compose_volume=v.get("compose_volume", ""),
            size_human=sz_human,
            size_bytes=parse_size_to_bytes(sz_human),
        )

    remote_hosts = set(raw.get("hosts", []))
    local_containers = get_local_docker_containers()
    local_volumes = get_local_docker_volumes()

    projects: List[ProjectInfo] = []
    for name, pdata in sorted(raw.get("projects", {}).items()):
        remote_path = pdata["remote_path"]
        local_path = LOCAL_PROJECTS_DIR / name
        size_bytes = int(pdata.get("size_bytes", 0))
        compose_file = pdata.get("compose_file")
        compose_content = pdata.get("compose_content")
        env_content = pdata.get("env_content")
        markers = pdata.get("markers", [])
        large_archives = pdata.get("large_archives", [])

        env_vars = parse_env_content(env_content) if env_content else {}
        compose_data: Dict[str, Any] = {}
        if compose_content:
            try:
                loaded = yaml.safe_load(compose_content)
                if isinstance(loaded, dict):
                    compose_data = loaded
            except Exception:
                compose_data = {}

        has_docker = bool(compose_file and compose_data.get("services"))

        # Match containers belonging to this project
        proj_containers = [
            c for c in remote_containers
            if c.working_dir == remote_path or c.compose_project == name
        ]

        # Determine effective compose project name
        compose_project_name = env_vars.get("COMPOSE_PROJECT_NAME", "")
        if not compose_project_name and proj_containers:
            compose_project_name = proj_containers[0].compose_project
        if not compose_project_name:
            compose_project_name = name.lower()

        # Match volumes belonging to this project
        proj_volumes: List[VolumeInfo] = []
        declared_vols = set(((compose_data or {}).get("volumes") or {}).keys())
        for vname, vinfo in remote_volumes.items():
            if vinfo.compose_project in (name, compose_project_name):
                proj_volumes.append(vinfo)
            elif any(vname == f"{p}_{dv}" for p in (name, compose_project_name) for dv in declared_vols):
                proj_volumes.append(vinfo)

        # Extract host ports per service
        host_ports: Dict[str, str] = {}
        has_tunnel = False
        services = (compose_data or {}).get("services") or {}
        for sname, sdef in services.items():
            if not isinstance(sdef, dict):
                continue
            img = str(sdef.get("image", "")).lower()
            if "cloudflared" in img or sname == "tunnel":
                has_tunnel = True
            ports_list = sdef.get("ports") or []
            resolved_ports = []
            for p in ports_list:
                p_str = interpolate_env(str(p), env_vars)
                resolved_ports.append(p_str)
            if resolved_ports:
                host_ports[sname] = ", ".join(resolved_ports)

        # Extract local domains (.local)
        domains: List[str] = []
        for k in ("APP_URL", "WP_HOME", "WP_SITEURL", "LOCAL_DOMAIN", "SERVER_NAME"):
            val = env_vars.get(k, "")
            if val:
                host = re.sub(r"^https?://", "", val).split("/")[0].split(":")[0].strip()
                if host and host != "localhost" and not re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
                    if host not in domains:
                        domains.append(host)
        # Also check if `<name>.local` or `<name without -prod>.local` is in Desktop's /etc/hosts
        for candidate in (f"{name}.local", f"{name.replace('-', '')}.local"):
            if candidate in remote_hosts and candidate not in domains:
                domains.append(candidate)

        stack = classify_stack(name, has_docker, compose_data, markers, large_archives)
        db_cfg = (
            extract_db_config(name, compose_data, env_vars, proj_containers, proj_volumes, markers)
            if has_docker
            else None
        )

        # Match local laptop containers & volumes
        loc_conts = [
            c for c in local_containers
            if c.working_dir == str(local_path) or c.compose_project in (name, compose_project_name)
        ]
        loc_vols = [
            v for v in local_volumes
            if any(v == pv.name for pv in proj_volumes) or v.startswith(f"{compose_project_name}_")
        ]

        projects.append(
            ProjectInfo(
                name=name,
                remote_path=remote_path,
                local_path=local_path,
                size_human=format_bytes(size_bytes),
                size_bytes=size_bytes,
                has_docker=has_docker,
                compose_file=compose_file,
                compose_data=compose_data,
                compose_project_name=compose_project_name,
                stack=stack,
                env_vars=env_vars,
                has_env=bool(env_content),
                has_git=bool(pdata.get("has_git")),
                git_branch=pdata.get("git_branch", ""),
                git_commit=pdata.get("git_commit", ""),
                containers=proj_containers,
                volumes=proj_volumes,
                db_config=db_cfg,
                host_ports=host_ports,
                local_domains=domains,
                large_archives=large_archives,
                has_tunnel=has_tunnel,
                installed_locally=is_project_installed(name),
                local_containers=loc_conts,
                local_volumes=loc_vols,
            )
        )

    return projects
