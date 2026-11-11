"""Command-line interface for devclone."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional

import click
from rich import box
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .config import (
    REMOTE_HOST,
    REMOTE_PROJECTS_DIR,
    get_active_env_file,
    get_all_config_summary,
)
from .diff_engine import pull_env_interactive, run_diff
from .discovery import ProjectInfo, discover_projects
from .setup_flow import pull_db_only, pull_uploads_only, setup_project
from .tui import select_project_interactive

console = Console()


def fetch_all_projects() -> List[ProjectInfo]:
    """Fetch remote & local project inventory with a spinner."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=console,
    ) as progress:
        progress.add_task(
            description=f"Scanning Desktop ({REMOTE_HOST}:{REMOTE_PROJECTS_DIR}) & Laptop state...",
            total=None,
        )
        return discover_projects()


def render_projects_table(
    projects: List[ProjectInfo],
    docker_only: bool = False,
    available_only: bool = False,
    installed_only: bool = False,
) -> None:
    """Render a Rich formatted table of projects."""
    filtered = projects
    if docker_only:
        filtered = [p for p in filtered if p.has_docker]
    if available_only:
        filtered = [p for p in filtered if not p.installed_locally]
    if installed_only:
        filtered = [p for p in filtered if p.installed_locally]

    table = Table(
        title=f"Desktop Projects ({REMOTE_HOST}) — {len(filtered)}/{len(projects)}",
        show_header=True,
        header_style="bold cyan",
        box=box.SIMPLE_HEAVY,
        padding=(0, 1),
        collapse_padding=True,
    )
    table.add_column("#", justify="right", style="dim", min_width=2, no_wrap=True)
    table.add_column("Project", style="bold white", min_width=18, overflow="fold")
    table.add_column("Stack", min_width=11, no_wrap=True)
    table.add_column("Files", justify="right", min_width=8, no_wrap=True)
    table.add_column("DB/Vols", justify="right", min_width=9, no_wrap=True)
    table.add_column("Desktop", min_width=10, no_wrap=True)
    table.add_column("Laptop", min_width=9, no_wrap=True)

    for idx, p in enumerate(filtered, 1):
        stack_short = (
            p.stack.replace("Laravel Sail ", "Sail ")
            .replace("WordPress + ", "WP+")
            .replace("WordPress Docker", "WP Docker")
            .replace("Laravel Docker", "Laravel")
            .replace("Static / Misc", "Static/Misc")
            .replace("Backups / Archives", "Archive")
        )
        stack_str = f"[cyan]{stack_short}[/cyan]" if p.has_docker else f"[dim]{stack_short}[/dim]"
        vol_str = f"[magenta]{p.total_volume_size_human}[/magenta]" if p.total_volume_size_human != "-" else "[dim]-[/dim]"

        if not p.has_docker:
            desk_str = "[dim]-[/dim]"
        elif "Up" in p.desktop_state_summary:
            desk_str = f"[green]{p.desktop_state_summary}[/green]"
        else:
            desk_str = f"[yellow]{p.desktop_state_summary}[/yellow]"

        if p.installed_locally:
            lap_str = f"[bold green]✓ {p.laptop_state_summary.replace('Installed ', '')}[/bold green]"
        else:
            lap_str = "[dim]Available[/dim]"

        table.add_row(
            str(idx),
            p.name,
            stack_str,
            p.size_human,
            vol_str,
            desk_str,
            lap_str,
        )

    console.print(table)
    installed_cnt = sum(1 for p in projects if p.installed_locally)
    docker_cnt = sum(1 for p in projects if p.has_docker)
    console.print(
        f"[dim]Summary: {len(projects)} total projects ({docker_cnt} Docker, {len(projects) - docker_cnt} Standalone) | "
        f"{installed_cnt} Installed on Laptop | {len(projects) - installed_cnt} Available to Setup[/dim]"
    )


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """
    devclone — Clone complete Docker & local development projects between two devices.

    Rules enforced:
      • 100% Read-Only on the remote device (never modified).
      • One-Time Setup lock locally (installed projects are never overwritten; use git for code).
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(interactive_dashboard)


@cli.command("list")
@click.option("--docker", "docker_only", is_flag=True, help="Show only Docker projects.")
@click.option("--available", "available_only", is_flag=True, help="Show only projects not yet set up on Laptop.")
@click.option("--installed", "installed_only", is_flag=True, help="Show only projects already set up on Laptop.")
def cmd_list(docker_only: bool, available_only: bool, installed_only: bool) -> None:
    """List all remote projects on Desktop and their status on Laptop."""
    projects = fetch_all_projects()
    render_projects_table(
        projects,
        docker_only=docker_only,
        available_only=available_only,
        installed_only=installed_only,
    )


@cli.command("setup")
@click.argument("project_name", required=False)
@click.option("--docker-only", is_flag=True, help="In interactive picker, show only Docker projects.")
@click.option("--no-start", is_flag=True, help="Sync files and volumes but do not start containers.")
@click.option("--include-archives", is_flag=True, help="Include large redundant .zip/.tar.gz backup archives.")
@click.option("--sql-dump", "force_sql_dump", is_flag=True, help="Force SQL dump when Desktop DB is running.")
@click.option("--with-tunnel", is_flag=True, help="Also start cloudflared tunnel container if present.")
@click.option("-y", "--yes", "auto_yes", is_flag=True, help="Automatic yes to prompts.")
def cmd_setup(
    project_name: Optional[str],
    docker_only: bool,
    no_start: bool,
    include_archives: bool,
    force_sql_dump: bool,
    with_tunnel: bool,
    auto_yes: bool,
) -> None:
    """
    Set up a project from Desktop onto this Laptop (one-time copy + volume/DB clone + boot).
    """
    projects = fetch_all_projects()
    by_name = {p.name: p for p in projects}

    target: Optional[ProjectInfo] = None
    if project_name:
        if project_name not in by_name:
            console.print(f"[bold red]Error:[/bold red] Project '{project_name}' not found on Desktop.")
            sys.exit(1)
        target = by_name[project_name]
    else:
        pool = [p for p in projects if (p.has_docker if docker_only else True)]
        target = select_project_interactive(
            pool,
            title="Select an available project to SET UP on Laptop (↑/↓, / to search, Enter to setup)",
            only_available=True,
        )
        if not target:
            console.print("[dim]No project selected.[/dim]")
            return

    ok = setup_project(
        target,
        no_start=no_start,
        include_archives=include_archives,
        force_sql_dump=force_sql_dump,
        with_tunnel=with_tunnel,
        auto_yes=auto_yes,
    )
    if not ok:
        sys.exit(1)


@cli.command("diff")
@click.argument("project_name", required=False)
@click.option("--all", "all_installed", is_flag=True, help="Check non-Git differences for all installed projects.")
@click.option("--show-secrets", is_flag=True, help="Show unmasked secret values in .env diff.")
def cmd_diff(project_name: Optional[str], all_installed: bool, show_secrets: bool) -> None:
    """
    Show real non-Git differences (.env, Database schema/rows/migrations, untracked uploads, /etc/hosts)
    between Desktop and Laptop.
    """
    projects = fetch_all_projects()
    by_name = {p.name: p for p in projects}

    if all_installed:
        installed = [p for p in projects if p.installed_locally]
        if not installed:
            console.print("[yellow]No projects are currently installed on this laptop.[/yellow]")
            return
        for p in installed:
            run_diff(p, show_secrets=show_secrets)
        return

    if project_name:
        if project_name not in by_name:
            console.print(f"[bold red]Error:[/bold red] Project '{project_name}' not found on Desktop.")
            sys.exit(1)
        run_diff(by_name[project_name], show_secrets=show_secrets)
        return

    installed = [p for p in projects if p.installed_locally]
    if not installed:
        console.print(
            "[yellow]No projects are installed on this laptop yet. Run `devclone setup` first![/yellow]"
        )
        return

    target = select_project_interactive(
        installed,
        title="Select an installed project to inspect NON-GIT DIFFERENCES",
        only_available=False,
    )
    if target:
        run_diff(target, show_secrets=show_secrets)


@cli.command("pull-db")
@click.argument("project_name")
@click.option("--sql-dump", "force_sql_dump", is_flag=True, help="Use live mysqldump instead of volume clone.")
@click.option("-y", "--yes", "auto_yes", is_flag=True, help="Automatic yes to confirmation prompt.")
def cmd_pull_db(project_name: str, force_sql_dump: bool, auto_yes: bool) -> None:
    """Refresh ONLY the database from Desktop for an already-installed project (leaves code untouched)."""
    projects = fetch_all_projects()
    by_name = {p.name: p for p in projects}
    if project_name not in by_name:
        console.print(f"[bold red]Error:[/bold red] Project '{project_name}' not found.")
        sys.exit(1)
    pull_db_only(by_name[project_name], force_sql_dump=force_sql_dump, auto_yes=auto_yes)


@cli.command("pull-env")
@click.argument("project_name")
def cmd_pull_env(project_name: str) -> None:
    """Interactively merge missing or changed .env variables from Desktop into Laptop's .env."""
    projects = fetch_all_projects()
    by_name = {p.name: p for p in projects}
    if project_name not in by_name:
        console.print(f"[bold red]Error:[/bold red] Project '{project_name}' not found.")
        sys.exit(1)
    pull_env_interactive(by_name[project_name])


@cli.command("pull-uploads")
@click.argument("project_name")
def cmd_pull_uploads(project_name: str) -> None:
    """Sync ONLY missing git-ignored uploads (`wp-content/uploads`, `storage/app/public`) from Desktop."""
    projects = fetch_all_projects()
    by_name = {p.name: p for p in projects}
    if project_name not in by_name:
        console.print(f"[bold red]Error:[/bold red] Project '{project_name}' not found.")
        sys.exit(1)
    pull_uploads_only(by_name[project_name])


@cli.command("config")
@click.option("--edit", is_flag=True, help="Open the .env configuration file in $EDITOR (or nano).")
def cmd_config(edit: bool) -> None:
    """View or edit the devclone .env configuration."""
    env_file = get_active_env_file()
    if edit:
        editor = os.environ.get("EDITOR") or "nano"
        subprocess.run([editor, str(env_file)])
        return

    cfg = get_all_config_summary()
    table = Table(
        title=f"devclone Active Configuration ({env_file})",
        show_header=True,
        header_style="bold cyan",
        box=box.SIMPLE_HEAVY,
    )
    table.add_column("Setting", style="bold white")
    table.add_column("Value", style="green")
    for k, v in cfg.items():
        table.add_row(k, v)
    console.print(table)
    console.print(f"[dim]Tip: Edit [bold]{env_file}[/bold] directly or run `devclone config --edit`.[/dim]")


@cli.command("ui")
def interactive_dashboard() -> None:
    """Open the interactive terminal browser to inspect, diff, or set up projects."""
    projects = fetch_all_projects()
    target = select_project_interactive(
        projects,
        title="devclone — Select a project (↑/↓ to navigate, / to filter, Enter for actions, q to quit)",
        only_available=False,
    )
    if not target:
        return

    if not target.installed_locally:
        setup_project(target)
    else:
        run_diff(target)
