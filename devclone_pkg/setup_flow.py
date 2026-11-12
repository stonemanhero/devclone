"""One-time Desktop -> Laptop setup pipeline and targeted non-code pull helpers."""

from __future__ import annotations

import gzip
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Confirm

from .config import (
    LOCAL_HELPER_IMAGE,
    REMOTE_HELPER_IMAGE,
    REMOTE_HOST,
    mark_project_installed,
    rsync_ssh_option,
    ssh_cmd_args,
)
from .discovery import (
    ContainerInfo,
    ProjectInfo,
    VolumeInfo,
    format_bytes,
    get_local_docker_containers,
    interpolate_env,
    parse_env_content,
)

console = Console()


def ensure_local_helper_image() -> None:
    """Ensure helper image (used for fast volume tar extraction) exists on Laptop."""
    res = subprocess.run(
        ["docker", "image", "inspect", LOCAL_HELPER_IMAGE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if res.returncode != 0:
        console.print(f"[dim]Pulling lightweight volume helper image ({LOCAL_HELPER_IMAGE})...[/dim]")
        subprocess.run(["docker", "pull", LOCAL_HELPER_IMAGE], check=True)


def extract_needed_host_ports(proj: ProjectInfo, include_tunnel: bool = False) -> Dict[int, str]:
    """Return mapping of host_port (int) -> service_name for a project."""
    port_map: Dict[int, str] = {}
    for svc, ports_str in proj.host_ports.items():
        if svc == "tunnel" and not include_tunnel:
            continue
        for item in ports_str.split(","):
            item = item.strip()
            if ":" in item:
                # Formats like "8000:80", "127.0.0.1:8000:80", "8080:80/tcp"
                parts = item.split(":")
                host_part = parts[-2] if len(parts) >= 2 else parts[0]
                m = re.match(r"^(\d+)", host_part)
                if m:
                    port_map[int(m.group(1))] = svc
    return port_map


def find_local_port_conflicts(needed_ports: Dict[int, str]) -> List[Tuple[int, str, ContainerInfo]]:
    """Check if any running container on Laptop binds any of the needed host ports."""
    conflicts: List[Tuple[int, str, ContainerInfo]] = []
    if not needed_ports:
        return conflicts
    running_containers = [c for c in get_local_docker_containers() if c.state == "running"]
    for port, svc in needed_ports.items():
        for c in running_containers:
            # Docker Ports string looks like: "0.0.0.0:8081->80/tcp, [::]:8081->80/tcp"
            if re.search(rf"(?:0\.0\.0\.0|127\.0\.0\.1|\[::\]):{port}->", c.ports):
                conflicts.append((port, svc, c))
    return conflicts


def check_missing_hosts(domains: List[str]) -> List[str]:
    """Return domains from `domains` that are not yet in Laptop's /etc/hosts."""
    if not domains:
        return []
    existing = set()
    try:
        with open("/etc/hosts", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                for token in line.split()[1:]:
                    existing.add(token)
    except Exception:
        pass
    return [d for d in domains if d not in existing]


def ensure_hosts_entries(domains: List[str], auto_yes: bool = False) -> None:
    """Offer to add missing .local domains to Laptop's /etc/hosts."""
    missing = check_missing_hosts(domains)
    if not missing:
        return

    console.print(
        f"\n[yellow]Local domain(s) missing in /etc/hosts:[/yellow] [bold]{', '.join(missing)}[/bold]"
    )
    if auto_yes or Confirm.ask("Add these domain(s) to Laptop's /etc/hosts (requires sudo if prompted)?", default=True):
        lines_to_add = "\n".join(f"127.0.0.1 {d}" for d in missing) + "\n"
        res = subprocess.run(
            ["sudo", "-n", "tee", "-a", "/etc/hosts"],
            input=lines_to_add,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if res.returncode == 0:
            console.print(f"[green]✓ Added {', '.join(missing)} to /etc/hosts[/green]")
        else:
            console.print("[dim]Please enter your laptop sudo password to update /etc/hosts:[/dim]")
            res2 = subprocess.run(
                ["sudo", "tee", "-a", "/etc/hosts"],
                input=lines_to_add,
                text=True,
                stdout=subprocess.DEVNULL,
            )
            if res2.returncode == 0:
                console.print(f"[green]✓ Added {', '.join(missing)} to /etc/hosts[/green]")
            else:
                console.print(
                    f"[yellow]! Could not update /etc/hosts automatically. Run manually:[/yellow]\n"
                    f"  [bold]echo '127.0.0.1 {' '.join(missing)}' | sudo tee -a /etc/hosts[/bold]"
                )


def clone_volume_readonly(vol: VolumeInfo, compose_project: str) -> None:
    """
    Clone a Docker volume from Desktop to Laptop 100% read-only on Desktop.
    Uses `docker run --rm -v <vol>:/from:ro <helper> tar -czf - -C /from .` on Desktop
    and pipes directly into the Laptop volume created with matching Docker Compose labels.
    """
    ensure_local_helper_image()

    # Create volume on Laptop with Docker Compose labels so `docker compose up` adopts it seamlessly
    cmd_create = ["docker", "volume", "create"]
    eff_proj = vol.compose_project or compose_project
    eff_vol = vol.compose_volume or vol.name.replace(f"{eff_proj}_", "", 1)
    if eff_proj:
        cmd_create += ["--label", f"com.docker.compose.project={eff_proj}"]
    if eff_vol:
        cmd_create += ["--label", f"com.docker.compose.volume={eff_vol}"]
    cmd_create.append(vol.name)
    subprocess.run(cmd_create, stdout=subprocess.DEVNULL, check=True)

    # Clear target volume first in case it existed before
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{vol.name}:/to",
            LOCAL_HELPER_IMAGE,
            "sh",
            "-c",
            "rm -rf /to/* /to/.[!.]* /to/..?* 2>/dev/null || true",
        ],
        check=True,
    )

    remote_cmd = (
        f"docker run --rm -v {shlex.quote(vol.name + ':/from:ro')} "
        f"{shlex.quote(REMOTE_HELPER_IMAGE)} tar -czf - -C /from ."
    )
    ssh_proc = subprocess.Popen(
        ssh_cmd_args() + [remote_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    local_proc = subprocess.Popen(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "-v",
            f"{vol.name}:/to",
            LOCAL_HELPER_IMAGE,
            "tar",
            "-xzf",
            "-",
            "-C",
            "/to",
        ],
        stdin=ssh_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if ssh_proc.stdout:
        ssh_proc.stdout.close()
    _, local_err = local_proc.communicate()
    _, ssh_err = ssh_proc.communicate()

    if ssh_proc.returncode != 0:
        raise RuntimeError(f"Remote volume read failed for {vol.name}: {ssh_err.decode(errors='replace')}")
    if local_proc.returncode != 0:
        raise RuntimeError(f"Local volume write failed for {vol.name}: {local_err.decode(errors='replace')}")


def clone_bind_mount_readonly(remote_dir: str, local_dir: Path) -> None:
    """
    Clone a root/mysql-owned bind-mounted directory (e.g. docker-volumes/mysql)
    100% read-only from Desktop using Docker `:ro` mount.
    """
    ensure_local_helper_image()
    local_dir.mkdir(parents=True, exist_ok=True)

    remote_cmd = (
        f"docker run --rm -v {shlex.quote(remote_dir + ':/from:ro')} "
        f"{shlex.quote(REMOTE_HELPER_IMAGE)} tar -czf - -C /from ."
    )
    ssh_proc = subprocess.Popen(
        ssh_cmd_args() + [remote_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    local_proc = subprocess.Popen(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "-v",
            f"{local_dir}:/to",
            LOCAL_HELPER_IMAGE,
            "tar",
            "-xzf",
            "-",
            "-C",
            "/to",
        ],
        stdin=ssh_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if ssh_proc.stdout:
        ssh_proc.stdout.close()
    _, local_err = local_proc.communicate()
    _, ssh_err = ssh_proc.communicate()
    if ssh_proc.returncode != 0 or local_proc.returncode != 0:
        raise RuntimeError(
            f"Bind-mount stream failed: {ssh_err.decode(errors='replace')} {local_err.decode(errors='replace')}"
        )


def stream_live_mysqldump_readonly(proj: ProjectInfo, dump_path: Path) -> None:
    """
    Dump a live running MySQL/MariaDB container on Desktop via `docker exec`
    with `--single-transaction` (100% read-only on Desktop).
    Passes password securely via MYSQL_PWD environment variable.
    """
    db = proj.db_config
    if not db:
        raise RuntimeError("No database configuration found for project.")

    dump_bin = "mariadb-dump" if "mariadb" in db.image.lower() else "mysqldump"
    pw = db.root_password or db.password
    env_flag = f"-e {shlex.quote('MYSQL_PWD=' + pw)} " if pw else ""
    db_quoted = shlex.quote(db.database)
    inner_cmd = (
        f"({dump_bin} -u root --single-transaction --routines --triggers --events --databases {db_quoted} "
        f"|| mysqldump -u root --single-transaction --routines --triggers --databases {db_quoted})"
    )
    remote_cmd = (
        f"docker exec {env_flag}{shlex.quote(db.container_name)} "
        f"sh -c {shlex.quote(inner_cmd)} | gzip -c"
    )
    with dump_path.open("wb") as out_f:
        proc = subprocess.run(
            ssh_cmd_args() + [remote_cmd],
            stdout=out_f,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Live SQL dump failed: {proc.stderr.decode(errors='replace')}")


def import_sql_dump_to_local(proj: ProjectInfo, dump_path: Path) -> None:
    """Import a .sql.gz dump file into the running local DB container on Laptop."""
    db = proj.db_config
    if not db:
        return
    # Find local DB container name
    local_conts = [
        c for c in get_local_docker_containers()
        if (c.working_dir == str(proj.local_path) or c.compose_project in (proj.name, proj.compose_project_name))
        and c.compose_service == db.service_name
    ]
    target_container = local_conts[0].name if local_conts else db.container_name
    pw = db.root_password or db.password
    client_bin = "mariadb" if "mariadb" in db.image.lower() else "mysql"

    exec_cmd = ["docker", "exec", "-i"]
    if pw:
        exec_cmd.extend(["-e", f"MYSQL_PWD={pw}"])
    exec_cmd.extend([
        target_container,
        "sh",
        "-c",
        f"({client_bin} -u root || mysql -u root)",
    ])

    with gzip.open(dump_path, "rb") as gz_f:
        proc = subprocess.run(
            exec_cmd,
            stdin=gz_f,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Local SQL import failed: {proc.stderr.decode(errors='replace')}")


def ensure_sail_env_defaults(local_env_path: Path) -> List[str]:
    """Ensure WWWUSER=1000 and WWWGROUP=1000 exist in .env if Sail uses them."""
    if not local_env_path.exists():
        return []
    content = local_env_path.read_text(encoding="utf-8", errors="replace")
    env = parse_env_content(content)
    added: List[str] = []
    lines_to_append: List[str] = []
    uid = str(os.getuid())
    gid = str(os.getgid())
    if "WWWUSER" not in env:
        lines_to_append.append(f"WWWUSER={uid}")
        added.append(f"WWWUSER={uid}")
    if "WWWGROUP" not in env:
        lines_to_append.append(f"WWWGROUP={gid}")
        added.append(f"WWWGROUP={gid}")
    if lines_to_append:
        with local_env_path.open("a", encoding="utf-8") as f:
            if not content.endswith("\n"):
                f.write("\n")
            f.write("\n# Added by devclone for local Docker build\n")
            f.write("\n".join(lines_to_append) + "\n")
    return added


def ensure_matching_db_image_from_remote(proj: ProjectInfo) -> None:
    """
    Ensure the local database image (e.g. `mysql` or `mariadb`) matches the exact image ID
    on the remote device. If a project uses an unpinned tag like `image: mysql`, pulling `:latest`
    from Docker Hub on the local device can fetch a newer major version (e.g. MySQL 26.x vs 9.5)
    that refuses to mount the cloned binary `/var/lib/mysql` volume (`MY-014060`).
    """
    if not proj.db_config or not proj.db_config.image:
        return
    raw_img = proj.db_config.image
    db_img = raw_img if ":" in raw_img else f"{raw_img}:latest"

    rem_proc = subprocess.run(
        ssh_cmd_args() + [f"docker image inspect --format '{{{{.Id}}}}' {shlex.quote(db_img)} 2>/dev/null"],
        capture_output=True,
        text=True,
    )
    if rem_proc.returncode != 0 or not rem_proc.stdout.strip():
        return
    rem_id = rem_proc.stdout.strip()

    loc_proc = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", db_img],
        capture_output=True,
        text=True,
    )
    loc_id = loc_proc.stdout.strip() if loc_proc.returncode == 0 else ""

    if loc_id == rem_id:
        return

    reason = (
        "version mismatch with remote DB volume"
        if loc_id
        else "ensuring exact DB version match"
    )
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        progress.add_task(
            description=f"Streaming database image [bold]{db_img}[/bold] from remote device ({reason})...",
            total=None,
        )
        ssh_p = subprocess.Popen(
            ssh_cmd_args() + [f"docker save {shlex.quote(db_img)}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        load_p = subprocess.Popen(
            ["docker", "load"],
            stdin=ssh_p.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if ssh_p.stdout:
            ssh_p.stdout.close()
        load_p.communicate()
        ssh_p.communicate()

    if ssh_p.returncode == 0 and load_p.returncode == 0:
        console.print(f"[green]✓ Synced matching database image [bold]{db_img}[/bold] from remote device[/green]")


def sync_custom_built_images_from_desktop(proj: ProjectInfo, include_tunnel: bool = False) -> bool:
    """
    For any service in docker-compose with a `build:` section, check if the built image
    already exists on Desktop. If it does (and is not yet on Laptop), stream it via
    `docker save | docker load` over LAN instead of running a slow/fragile Dockerfile build.
    Also ensures the database service image matches the exact remote image SHA so binary
    volumes never fail due to `:latest` tag drift.
    Returns True if all buildable services now have their images present on Laptop.
    """
    ensure_matching_db_image_from_remote(proj)
    services_dict = (proj.compose_data or {}).get("services") or {}
    all_ready = True

    for sname, sdef in services_dict.items():
        if not isinstance(sdef, dict) or "build" not in sdef:
            continue
        if sname == "tunnel" and not include_tunnel:
            continue

        # Determine image name
        img_name = ""
        if sdef.get("image"):
            img_name = interpolate_env(str(sdef["image"]), proj.env_vars)
        if not img_name:
            for c in proj.containers:
                if c.compose_service == sname and c.image and not re.match(r"^[0-9a-f]{12}$", c.image):
                    img_name = c.image
                    break
        if not img_name:
            img_name = f"{proj.compose_project_name}-{sname}:latest"

        # Check if image already exists on Laptop
        loc_chk = subprocess.run(
            ["docker", "image", "inspect", img_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if loc_chk.returncode == 0:
            console.print(f"[green]✓ Custom image [bold]{img_name}[/bold] already cached on Laptop[/green]")
            continue

        # Check if image exists on Desktop
        rem_chk = subprocess.run(
            ssh_cmd_args() + [f"docker image inspect {shlex.quote(img_name)} >/dev/null 2>&1"],
        )
        if rem_chk.returncode != 0:
            all_ready = False
            continue

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            progress.add_task(
                description=f"Streaming pre-built image [bold]{img_name}[/bold] from Desktop (avoids Dockerfile apt build)...",
                total=None,
            )
            ssh_p = subprocess.Popen(
                ssh_cmd_args() + [f"docker save {shlex.quote(img_name)}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            load_p = subprocess.Popen(
                ["docker", "load"],
                stdin=ssh_p.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if ssh_p.stdout:
                ssh_p.stdout.close()
            _, load_err = load_p.communicate()
            _, ssh_err = ssh_p.communicate()

        if ssh_p.returncode == 0 and load_p.returncode == 0:
            console.print(f"[green]✓ Transferred pre-built image [bold]{img_name}[/bold] from Desktop[/green]")
        else:
            console.print(
                f"[yellow]! Could not stream image {img_name} from Desktop ({ssh_err.decode(errors='replace').strip()}); will fallback to local build.[/yellow]"
            )
            all_ready = False

    return all_ready


def wait_for_local_db_ready(proj: ProjectInfo, timeout_sec: int = 60) -> bool:
    """Wait until the local DB container is Up (and healthy if healthcheck exists)."""
    if not proj.db_config:
        return True
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        conts = [
            c for c in get_local_docker_containers()
            if (c.working_dir == str(proj.local_path) or c.compose_project in (proj.name, proj.compose_project_name))
            and c.compose_service == proj.db_config.service_name
        ]
        if conts:
            c = conts[0]
            if c.state == "running":
                if "health: starting" not in c.status.lower():
                    return True
        time.sleep(2.0)
    return False


def setup_project(
    proj: ProjectInfo,
    no_start: bool = False,
    include_archives: bool = False,
    force_sql_dump: bool = False,
    with_tunnel: bool = False,
    auto_yes: bool = False,
) -> bool:
    """
    Execute the complete one-time Desktop -> Laptop setup pipeline for `proj`.
    Strictly enforces:
      1. Never overwrite an already-installed project on Laptop.
      2. Never mutate anything on Desktop (100% read-only SSH & :ro Docker mounts).
    """
    # 0. One-Time Setup Guardrail
    if proj.installed_locally:
        console.print(
            Panel(
                f"[bold red]Setup Blocked (One-Time Setup Rule)[/bold red]\n\n"
                f"Project [bold cyan]{proj.name}[/bold cyan] is already set up on this laptop at:\n"
                f"  [underline]{proj.local_path}[/underline]\n\n"
                f"To protect your local work, [bold]devclone setup[/bold] never overwrites an installed project.\n"
                f"• Use [bold green]git pull / git push[/bold green] for code changes.\n"
                f"• Use [bold yellow]devclone diff {proj.name}[/bold yellow] to inspect non-Git differences (.env, DB, uploads).\n"
                f"• Use [bold magenta]devclone pull-db {proj.name}[/bold magenta] or [bold magenta]devclone pull-env {proj.name}[/bold magenta] if you specifically need to refresh non-code state.",
                title="Guardrail Active",
                border_style="red",
            )
        )
        return False

    console.print(
        Panel(
            f"[bold cyan]Setting up project:[/bold cyan] [bold white]{proj.name}[/bold white]\n"
            f"• Stack:          [green]{proj.stack}[/green]\n"
            f"• Remote Source:  {REMOTE_HOST}:{proj.remote_path} ({proj.size_human})\n"
            f"• Local Target:   {proj.local_path}\n"
            f"• Docker Volumes: {', '.join(f'{v.name} ({v.size_human})' for v in proj.volumes) or ('bind-mount' if (proj.db_config and proj.db_config.bind_mount_dir) else 'None')}\n"
            f"• Local Domains:  {', '.join(proj.local_domains) or 'localhost'}",
            title="devclone One-Time Setup",
            border_style="cyan",
        )
    )

    # 1. Pre-flight port conflict check
    if proj.has_docker and not no_start:
        needed_ports = extract_needed_host_ports(proj, include_tunnel=with_tunnel)
        conflicts = find_local_port_conflicts(needed_ports)
        if conflicts:
            console.print("\n[yellow]Port conflicts detected with currently running containers on Laptop:[/yellow]")
            for port, svc, cont in conflicts:
                console.print(f"  • Port [bold]{port}[/bold] (needed by [cyan]{svc}[/cyan]) is in use by [bold]{cont.name}[/bold] ({cont.compose_project})")
            if auto_yes or Confirm.ask("Stop those conflicting local containers on Laptop before starting this project?", default=True):
                to_stop = sorted({c.name for _, _, c in conflicts})
                subprocess.run(["docker", "stop"] + to_stop, check=False)
                console.print(f"[green]✓ Stopped {', '.join(to_stop)}[/green]")

    # 2. Smart File Sync (rsync over SSH, read-only on Desktop)
    console.print(f"\n[bold cyan][1/4] Syncing project files from Desktop ({proj.size_human})...[/bold cyan]")
    proj.local_path.mkdir(parents=True, exist_ok=True)

    rsync_cmd = [
        "rsync",
        "-az",
        "--info=progress2",
    ] + rsync_ssh_option()
    skipped_archives: List[Dict[str, object]] = []
    if not include_archives and proj.large_archives:
        for arch in proj.large_archives:
            rsync_cmd.extend(["--exclude", f"/{arch['name']}"])
            skipped_archives.append(arch)

    # If project has a root-owned bind-mount DB folder (like docker-volumes/mysql),
    # exclude it from normal user rsync and stream it via Docker :ro instead.
    if proj.db_config and proj.db_config.bind_mount_dir:
        bm_clean = proj.db_config.bind_mount_dir.lstrip("./")
        rsync_cmd.extend(["--exclude", f"/{bm_clean}/"])

    rsync_cmd.extend([
        f"{REMOTE_HOST}:{proj.remote_path.rstrip('/')}/",
        f"{str(proj.local_path).rstrip('/')}/",
    ])

    rsync_res = subprocess.run(rsync_cmd)
    if rsync_res.returncode not in (0, 23):  # 23 = partial transfer due to permission on a cache file
        raise RuntimeError(f"rsync failed with exit code {rsync_res.returncode}")

    if skipped_archives:
        saved_bytes = sum(int(a["size_bytes"]) for a in skipped_archives)
        names = ", ".join(str(a["name"]) for a in skipped_archives)
        console.print(
            f"[dim]  ↳ Skipped redundant archive file(s): {names} (saved {format_bytes(saved_bytes)}; pass --include-archives to copy)[/dim]"
        )
    console.print("[green]✓ Project files synced to Laptop[/green]")

    # 3. Zero-Mutation Docker Volume & Database Sync
    cloned_volumes: List[str] = []
    live_dump_file: Optional[Path] = None

    if proj.has_docker:
        console.print("\n[bold cyan][2/4] Syncing Docker volumes & database (100% read-only on Desktop)...[/bold cyan]")
        for vol in proj.volumes:
            is_db_vol = bool(proj.db_config and proj.db_config.volume_name == vol.name)
            db_is_running_on_desktop = bool(
                is_db_vol and proj.db_config and proj.db_config.state == "running"
            )

            if is_db_vol and (db_is_running_on_desktop or force_sql_dump) and proj.db_config and proj.db_config.state == "running":
                # Desktop DB is live -> stream consistent online mysqldump
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=f"_{proj.name}.sql.gz", prefix="devclone_dump_")
                os.close(tmp_fd)
                live_dump_file = Path(tmp_path)
                with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), TimeElapsedColumn(), console=console) as progress:
                    progress.add_task(
                        description=f"Streaming live mysqldump from running Desktop container [bold]{proj.db_config.container_name}[/bold]...",
                        total=None,
                    )
                    stream_live_mysqldump_readonly(proj, live_dump_file)
                console.print(
                    f"[green]✓ Streamed live database dump ({format_bytes(live_dump_file.stat().st_size)} compressed)[/green]"
                )
            else:
                # Desktop container is Exited (or non-DB volume) -> stream read-only volume snapshot
                label = (
                    f"Database '{proj.db_config.database}' (volume {vol.name}, {vol.size_human})"
                    if (is_db_vol and proj.db_config)
                    else f"volume {vol.name} ({vol.size_human})"
                )
                with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), TimeElapsedColumn(), console=console) as progress:
                    progress.add_task(
                        description=f"Syncing [bold]{label}[/bold] from Desktop...",
                        total=None,
                    )
                    clone_volume_readonly(vol, proj.compose_project_name)
                cloned_volumes.append(vol.name)
                console.print(f"[green]✓ Synced {label}[/green]")

        # Handle bind-mounted DB directory (e.g. docker-volumes/mysql)
        if proj.db_config and proj.db_config.bind_mount_dir:
            bm_rel = proj.db_config.bind_mount_dir.lstrip("./")
            remote_bm = f"{proj.remote_path.rstrip('/')}/{bm_rel}"
            local_bm = proj.local_path / bm_rel
            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), TimeElapsedColumn(), console=console) as progress:
                progress.add_task(
                    description=f"Cloning bind-mounted DB directory [bold]{bm_rel}[/bold] via read-only mount...",
                    total=None,
                )
                clone_bind_mount_readonly(remote_bm, local_bm)
            cloned_volumes.append(f"bind:{bm_rel}")
            console.print(f"[green]✓ Cloned bind-mounted DB directory {bm_rel}[/green]")
    else:
        console.print("\n[dim][2/4] Non-Docker project — no Docker volumes to clone.[/dim]")

    # 4. Local Environment & /etc/hosts Configuration
    console.print("\n[bold cyan][3/4] Configuring local environment on Laptop...[/bold cyan]")
    if proj.has_docker and "Sail" in proj.stack:
        added_vars = ensure_sail_env_defaults(proj.local_path / ".env")
        if added_vars:
            console.print(f"[green]✓ Added {', '.join(added_vars)} to local .env for Sail compatibility[/green]")

    if proj.local_domains:
        ensure_hosts_entries(proj.local_domains, auto_yes=auto_yes)
    else:
        console.print("[green]✓ No custom /etc/hosts domains required[/green]")

    # 5. Start Docker containers & verify
    if proj.has_docker and not no_start and proj.compose_file:
        console.print("\n[bold cyan][4/4] Syncing pre-built images & starting Docker containers on Laptop...[/bold cyan]")
        services_dict = (proj.compose_data or {}).get("services") or {}
        service_names = list(services_dict.keys())
        if proj.has_tunnel and not with_tunnel and "tunnel" in service_names:
            service_names = [s for s in service_names if s != "tunnel"]
            console.print("[dim]  ↳ Skipping 'tunnel' (cloudflared) service on Laptop (pass --with-tunnel to include)[/dim]")

        images_ready = sync_custom_built_images_from_desktop(proj, include_tunnel=with_tunnel)
        build_flag = "--no-build" if images_ready else "--build"
        up_cmd = ["docker", "compose", "-f", proj.compose_file, "up", "-d", build_flag] + service_names
        up_res = subprocess.run(up_cmd, cwd=str(proj.local_path))
        if up_res.returncode != 0 and build_flag == "--no-build":
            console.print("[dim]  ↳ Retrying with `docker compose up -d --build`...[/dim]")
            up_res = subprocess.run(
                ["docker", "compose", "-f", proj.compose_file, "up", "-d", "--build"] + service_names,
                cwd=str(proj.local_path),
            )
        if up_res.returncode != 0:
            console.print("[yellow]! `docker compose up -d` returned a warning/error; checking container states...[/yellow]")

        if proj.db_config:
            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), TimeElapsedColumn(), console=console) as progress:
                progress.add_task(
                    description=f"Waiting for local database service ({proj.db_config.service_name}) to be ready...",
                    total=None,
                )
                db_ready = wait_for_local_db_ready(proj, timeout_sec=60)
            if db_ready:
                console.print(f"[green]✓ Database service ({proj.db_config.service_name}) is Up and ready[/green]")

        if live_dump_file and live_dump_file.exists():
            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), TimeElapsedColumn(), console=console) as progress:
                progress.add_task(
                    description="Importing live SQL dump into local database container...",
                    total=None,
                )
                import_sql_dump_to_local(proj, live_dump_file)
            live_dump_file.unlink(missing_ok=True)
            console.print("[green]✓ Imported live SQL dump into local database[/green]")
    else:
        console.print("\n[dim][4/4] Container startup skipped (--no-start or standalone project).[/dim]")

    # Record installation state
    ports_summary = [f"{svc}:{p}" for svc, p in proj.host_ports.items()]
    mark_project_installed(
        project_name=proj.name,
        stack=proj.stack,
        volumes_cloned=cloned_volumes,
        local_domains=proj.local_domains,
        ports=ports_summary,
        git_commit=proj.git_commit,
    )

    # Display final summary
    urls: List[str] = []
    for d in proj.local_domains:
        urls.append(f"http://{d}")
    for svc, p_str in proj.host_ports.items():
        if svc == "tunnel" and not with_tunnel:
            continue
        for item in p_str.split(","):
            item = item.strip()
            if ":" in item:
                hp = item.split(":")[-2]
                if hp == "80":
                    urls.append(f"http://localhost ({svc})")
                elif hp == "443":
                    urls.append(f"https://localhost ({svc})")
                elif hp not in ("3306", "33060", "6379", "63790", "6380", "1025", "1026"):
                    urls.append(f"http://localhost:{hp} ({svc})")

    console.print(
        Panel(
            f"[bold green]✓ Project '{proj.name}' is now set up on this laptop![/bold green]\n\n"
            f"• Local Path: [bold]{proj.local_path}[/bold]\n"
            f"• Endpoints:  {', '.join(dict.fromkeys(urls)) if urls else 'N/A'}\n"
            f"• Next Steps: Use [bold]git[/bold] for code changes, and [bold]devclone diff {proj.name}[/bold] to check non-Git differences.",
            title="Setup Complete",
            border_style="green",
        )
    )
    return True


def pull_db_only(proj: ProjectInfo, force_sql_dump: bool = False, auto_yes: bool = False) -> bool:
    """
    Refresh ONLY the database / DB volume for an already-installed project from Desktop -> Laptop,
    without touching any local project code files.
    """
    if not proj.installed_locally:
        console.print(f"[red]Project '{proj.name}' is not installed yet. Use `devclone setup {proj.name}` first.[/red]")
        return False
    if not proj.db_config:
        console.print(f"[yellow]Project '{proj.name}' does not have a Docker database service.[/yellow]")
        return False

    if not auto_yes and not Confirm.ask(
        f"Refresh local database for [bold cyan]{proj.name}[/bold cyan] from Desktop (local code files will NOT be touched)?",
        default=True,
    ):
        return False

    db = proj.db_config
    # Stop local DB container temporarily if we are cloning its volume
    local_conts = [
        c for c in get_local_docker_containers()
        if (c.working_dir == str(proj.local_path) or c.compose_project in (proj.name, proj.compose_project_name))
    ]
    running_local = [c.name for c in local_conts if c.state == "running"]

    if db.state == "running" or force_sql_dump:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=f"_{proj.name}.sql.gz", prefix="devclone_dump_")
        os.close(tmp_fd)
        dump_file = Path(tmp_path)
        console.print(f"[cyan]Streaming live SQL dump from Desktop ({db.container_name})...[/cyan]")
        stream_live_mysqldump_readonly(proj, dump_file)
        console.print("[cyan]Importing SQL dump into Laptop database container...[/cyan]")
        import_sql_dump_to_local(proj, dump_file)
        dump_file.unlink(missing_ok=True)
        console.print(f"[bold green]✓ Database '{db.database}' refreshed from Desktop![/bold green]")
        return True

    # Volume clone path
    if running_local:
        console.print(f"[dim]Stopping local container(s) {', '.join(running_local)} during volume refresh...[/dim]")
        subprocess.run(["docker", "stop"] + running_local, check=False)

    if db.volume_name:
        matched_vols = [v for v in proj.volumes if v.name == db.volume_name]
        vol = matched_vols[0] if matched_vols else VolumeInfo(name=db.volume_name, compose_project=proj.compose_project_name, compose_volume="db_data")
        console.print(f"[cyan]Cloning volume {vol.name} ({vol.size_human}) read-only from Desktop...[/cyan]")
        clone_volume_readonly(vol, proj.compose_project_name)
    elif db.bind_mount_dir:
        bm_rel = db.bind_mount_dir.lstrip("./")
        clone_bind_mount_readonly(f"{proj.remote_path.rstrip('/')}/{bm_rel}", proj.local_path / bm_rel)

    if running_local:
        console.print(f"[dim]Restarting local container(s) {', '.join(running_local)}...[/dim]")
        subprocess.run(["docker", "start"] + running_local, check=False)

    console.print(f"[bold green]✓ Database volume for '{proj.name}' refreshed from Desktop![/bold green]")
    return True


def pull_uploads_only(proj: ProjectInfo) -> bool:
    """
    Sync ONLY missing git-ignored upload/storage files (`wp-content/uploads`, `storage/app/public`)
    from Desktop -> Laptop without overwriting existing code files.
    """
    if not proj.installed_locally:
        console.print(f"[red]Project '{proj.name}' is not installed yet. Use `devclone setup {proj.name}` first.[/red]")
        return False

    candidate_dirs = [
        "wp-content/uploads",
        "html/wp-content/uploads",
        "src/wp-content/uploads",
        "storage/app/public",
    ]
    synced_any = False
    for rel in candidate_dirs:
        remote_dir = f"{proj.remote_path.rstrip('/')}/{rel}"
        check = subprocess.run(
            ssh_cmd_args() + [f"test -d {shlex.quote(remote_dir)}"],
        )
        if check.returncode == 0:
            target_dir = proj.local_path / rel
            target_dir.mkdir(parents=True, exist_ok=True)
            console.print(f"[cyan]Syncing missing files in {rel} from Desktop...[/cyan]")
            subprocess.run(
                [
                    "rsync",
                    "-avz",
                    "--ignore-existing",
                ]
                + rsync_ssh_option()
                + [
                    f"{REMOTE_HOST}:{remote_dir}/",
                    f"{target_dir}/",
                ],
                check=True,
            )
            synced_any = True

    if synced_any:
        console.print(f"[bold green]✓ Missing uploads/storage files synced for '{proj.name}'![/bold green]")
    else:
        console.print(f"[yellow]No standard upload directories (wp-content/uploads, storage/app/public) found on Desktop for '{proj.name}'.[/yellow]")
    return synced_any
