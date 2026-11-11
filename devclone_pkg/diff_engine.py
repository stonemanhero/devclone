"""Non-Git 'Real Differences' engine comparing Desktop vs Laptop state."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from typing import Any, Dict, List, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from .config import (
    LOCAL_HELPER_IMAGE,
    MAX_EPHEMERAL_SQL_MB,
    REMOTE_HELPER_IMAGE,
    ssh_cmd_args,
)
from .discovery import (
    ProjectInfo,
    format_bytes,
    get_local_docker_containers,
    parse_env_content,
)

console = Console()

SENSITIVE_KEY_RE = re.compile(r"(PASSWORD|SECRET|KEY|TOKEN|DSN|PRIVATE)", re.IGNORECASE)


def mask_value(key: str, val: str, show_secrets: bool = False) -> str:
    """Mask sensitive .env values while keeping a short hash fingerprint so user sees they differ."""
    if show_secrets or not val or not SENSITIVE_KEY_RE.search(key):
        return val
    short_hash = hashlib.sha256(val.encode("utf-8")).hexdigest()[:6]
    if len(val) <= 6:
        return f"*** (sha:{short_hash})"
    return f"{val[:2]}***{val[-2:]} (sha:{short_hash})"


# Python helper script executed inside either remote or local environment to inspect DB & runtime files
DB_INSPECT_SCRIPT = r"""
import os, sys, json, shlex, subprocess

cfg = json.loads(sys.stdin.read())
mode = cfg["mode"]  # "running", "ephemeral_sql", "ibd_stat"
container = cfg.get("container", "")
volume = cfg.get("volume", "")
bind_dir = cfg.get("bind_dir", "")
image = cfg.get("image", "mysql:8.0")
database = cfg.get("database", "")
root_pw = cfg.get("root_password", "")
helper_image = cfg.get("helper_image", "redis:alpine")

db_str = database.replace("'", "''")
db_ident = database.replace("`", "``")

out = {
    "method": mode,
    "tables": {},       # table_name -> {"rows": int, "size_bytes": int, "columns": [col_name:col_type]}
    "migrations": [],   # list of migration strings
    "error": None,
}

SQL_PROBE = f'''
SELECT TABLE_NAME, IFNULL(TABLE_ROWS, 0), IFNULL(DATA_LENGTH + INDEX_LENGTH, 0)
FROM information_schema.tables
WHERE TABLE_SCHEMA = '{db_str}' AND TABLE_TYPE = 'BASE TABLE';
SELECT '---COLUMNS---';
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
FROM information_schema.columns
WHERE TABLE_SCHEMA = '{db_str}'
ORDER BY TABLE_NAME, ORDINAL_POSITION;
'''

def parse_sql_output(raw_text, run_sql_fn):
    parts = raw_text.split("---COLUMNS---")
    tbl_lines = parts[0].strip().splitlines() if parts else []
    col_lines = parts[1].strip().splitlines() if len(parts) > 1 else []
    for line in tbl_lines:
        cols = line.split("\t")
        if len(cols) >= 3 and cols[0] != "TABLE_NAME":
            tname = cols[0]
            out["tables"][tname] = {
                "rows": int(cols[1]) if cols[1].isdigit() else 0,
                "size_bytes": int(cols[2]) if cols[2].isdigit() else 0,
                "columns": []
            }
    for line in col_lines:
        cols = line.split("\t")
        if len(cols) >= 4 and cols[0] != "TABLE_NAME":
            tname, cname, ctype, is_null = cols[0], cols[1], cols[2], cols[3]
            if tname in out["tables"]:
                out["tables"][tname]["columns"].append(f"{cname}:{ctype}:{is_null}")

    # Exact COUNT(*) for up to 80 tables so InnoDB approximations don't mislead
    tnames = list(out["tables"].keys())
    if tnames and len(tnames) <= 80:
        union_q = " UNION ALL ".join(
            f"SELECT '{t.replace(chr(39), chr(39)*2)}' AS t, COUNT(*) AS c FROM `{db_ident}`.`{t.replace('`', '``')}`" for t in tnames
        ) + ";"
        cnt_out = run_sql_fn(union_q)
        if cnt_out:
            for line in cnt_out.strip().splitlines():
                cparts = line.split("\t")
                if len(cparts) == 2 and cparts[0] in out["tables"] and cparts[1].isdigit():
                    out["tables"][cparts[0]]["rows"] = int(cparts[1])

    # Check Laravel migrations table if present
    if "migrations" in out["tables"]:
        mig_out = run_sql_fn(f"SELECT migration FROM `{db_ident}`.`migrations` ORDER BY id;")
        if mig_out:
            out["migrations"] = [
                m.strip() for m in mig_out.strip().splitlines()
                if m.strip() and m.strip() != "migration"
            ]

try:
    if mode == "running":
        pw_env = ["-e", f"MYSQL_PWD={root_pw}"] if root_pw else []
        def run_live(q):
            for cli_bin in ("mysql", "mariadb"):
                cmd = ["docker", "exec", "-i"] + pw_env + [container, cli_bin, "-N", "-B", "-u", "root"]
                r = subprocess.run(cmd, input=q, capture_output=True, text=True)
                if r.returncode == 0:
                    return r.stdout
            return ""

        probe_res = run_live(SQL_PROBE)
        if probe_res:
            parse_sql_output(probe_res, run_live)
        else:
            mode = "ibd_stat"

    if mode == "ephemeral_sql":
        mount_src = volume if volume else bind_dir
        inner_sh = f'''
mkdir -p /tmp/db
for f in /vol/* /vol/.[!.]*; do
    base=$(basename "$f")
    case "$base" in
        binlog*) continue ;;
        *) cp -a "$f" /tmp/db/ 2>/dev/null || true ;;
    esac
done
chown -R mysql:mysql /tmp/db 2>/dev/null || true
DB_BIN=$(command -v mysqld || command -v mariadbd)
CLI_BIN=$(command -v mysql || command -v mariadb)
$DB_BIN --datadir=/tmp/db --user=mysql --skip-log-bin --skip-grant-tables --skip-networking --tmpdir=/tmp --socket=/tmp/mysql.sock >/dev/null 2>&1 &
DB_PID=$!
for i in $(seq 1 40); do
    if [ -S /tmp/mysql.sock ]; then break; fi
    sleep 0.25
done
if [ -S /tmp/mysql.sock ]; then
    $CLI_BIN -N -B -S /tmp/mysql.sock << 'EOSQL' 2>/dev/null
{SQL_PROBE.strip()}
EOSQL
    echo "---MIGRATIONS---"
    $CLI_BIN -N -B -S /tmp/mysql.sock -e "SELECT migration FROM \\`{db_ident}\\`.\\`migrations\\` ORDER BY id;" 2>/dev/null || true
    echo "---COUNTS---"
    UNION_SQL=$($CLI_BIN -N -B -S /tmp/mysql.sock -e "SET SESSION group_concat_max_len = 1000000; SELECT GROUP_CONCAT(CONCAT('SELECT \\'', TABLE_NAME, '\\' AS t, COUNT(*) AS c FROM \\`{db_ident}\\`.\\`', TABLE_NAME, '\\`') SEPARATOR ' UNION ALL ') FROM information_schema.tables WHERE TABLE_SCHEMA='{db_str}' AND TABLE_TYPE='BASE TABLE';" 2>/dev/null)
    if [ -n "$UNION_SQL" ]; then
        $CLI_BIN -N -B -S /tmp/mysql.sock -e "$UNION_SQL;" 2>/dev/null || true
    fi
    kill $DB_PID 2>/dev/null || true
    wait $DB_PID 2>/dev/null || true
else
    exit 1
fi
'''
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{mount_src}:/vol:ro", image, "sh", "-c", inner_sh],
            capture_output=True, text=True, timeout=60
        )
        if r.returncode == 0 and r.stdout.strip():
            raw_all = r.stdout
            s_probe, rest = (raw_all.split("---MIGRATIONS---", 1) + [""])[:2]
            s_mig, s_cnt = (rest.split("---COUNTS---", 1) + [""])[:2]
            parse_sql_output(s_probe, lambda _q: "")
            for line in s_mig.strip().splitlines():
                if line.strip() and line.strip() != "migration":
                    out["migrations"].append(line.strip())
            for line in s_cnt.strip().splitlines():
                cparts = line.split("\t")
                if len(cparts) == 2 and cparts[0] in out["tables"] and cparts[1].strip().isdigit():
                    out["tables"][cparts[0]]["rows"] = int(cparts[1].strip())
        else:
            mode = "ibd_stat"

    if mode == "ibd_stat":
        out["method"] = "ibd_stat"
        mount_src = volume if volume else bind_dir
        if mount_src:
            vol_db_path = shlex.quote(f"/vol/{database}")
            r = subprocess.run(
                [
                    "docker", "run", "--rm", "-v", f"{mount_src}:/vol:ro", helper_image,
                    "sh", "-c",
                    f"find {vol_db_path} -maxdepth 1 -type f \\( -name '*.ibd' -o -name '*.MYD' \\) -exec stat -c '%n\t%s\t%Y' {{}} + 2>/dev/null"
                ],
                capture_output=True, text=True
            )
            for line in r.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    fname = os.path.basename(parts[0])
                    tname = os.path.splitext(fname)[0].split("#P#")[0]
                    sz = int(parts[1]) if parts[1].isdigit() else 0
                    prev = out["tables"].get(tname, {"rows": -1, "size_bytes": 0, "columns": []})
                    prev["size_bytes"] += sz
                    out["tables"][tname] = prev
except Exception as e:
    out["error"] = str(e)

print(json.dumps(out))
"""


def inspect_database_state(proj: ProjectInfo, is_remote: bool) -> Dict[str, Any]:
    """Inspect database schema, row counts, and migrations on either Desktop (read-only) or Laptop."""
    db = proj.db_config
    if not db:
        return {"method": "none", "tables": {}, "migrations": [], "error": None}

    # Determine container & state on target side
    if is_remote:
        state = db.state
        container = db.container_name
        vol_size = sum(v.size_bytes for v in proj.volumes if v.name == db.volume_name)
        mount_dir = f"{proj.remote_path.rstrip('/')}/{db.bind_mount_dir.lstrip('./')}" if db.bind_mount_dir else ""
        helper_img = REMOTE_HELPER_IMAGE
    else:
        loc_conts = [
            c for c in get_local_docker_containers()
            if (c.working_dir == str(proj.local_path) or c.compose_project in (proj.name, proj.compose_project_name))
            and c.compose_service == db.service_name
        ]
        state = loc_conts[0].state if loc_conts else "exited"
        container = loc_conts[0].name if loc_conts else db.container_name
        vol_size = sum(v.size_bytes for v in proj.volumes if v.name == db.volume_name)
        mount_dir = str(proj.local_path / db.bind_mount_dir.lstrip("./")) if db.bind_mount_dir else ""
        helper_img = LOCAL_HELPER_IMAGE

    if state == "running":
        mode = "running"
    elif vol_size > 0 and vol_size <= MAX_EPHEMERAL_SQL_MB * 1024 * 1024:
        mode = "ephemeral_sql"
    elif db.bind_mount_dir:
        mode = "ephemeral_sql"
    else:
        mode = "ibd_stat"

    payload = json.dumps(
        {
            "mode": mode,
            "container": container,
            "volume": db.volume_name or "",
            "bind_dir": mount_dir,
            "image": db.image,
            "database": db.database,
            "root_password": db.root_password or db.password,
            "helper_image": helper_img,
        }
    )

    script_with_payload = DB_INSPECT_SCRIPT.replace(
        "cfg = json.loads(sys.stdin.read())",
        f"cfg = json.loads({repr(payload)})",
    )
    if is_remote:
        proc = subprocess.run(
            ssh_cmd_args() + ["python3 -"],
            input=script_with_payload,
            capture_output=True,
            text=True,
        )
    else:
        proc = subprocess.run(
            ["python3", "-"],
            input=script_with_payload,
            capture_output=True,
            text=True,
        )

    if proc.returncode != 0 or not proc.stdout.strip():
        return {"method": "error", "tables": {}, "migrations": [], "error": proc.stderr.strip()}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as e:
        return {"method": "error", "tables": {}, "migrations": [], "error": str(e)}


RUNTIME_SCAN_SCRIPT = r"""
import os, json

pdir = __PDIR__
candidates = [
    "wp-content/uploads",
    "html/wp-content/uploads",
    "src/wp-content/uploads",
    "storage/app/public",
]
res = {}
for rel in candidates:
    full = os.path.join(pdir, rel)
    if os.path.isdir(full):
        files = {}
        total_bytes = 0
        for root, _, fnames in os.walk(full):
            for fn in fnames:
                fpath = os.path.join(root, fn)
                try:
                    st = os.stat(fpath)
                    rpath = os.path.relpath(fpath, full)
                    files[rpath] = st.st_size
                    total_bytes += st.st_size
                except Exception:
                    pass
        res[rel] = {"count": len(files), "total_bytes": total_bytes, "files": files}
print(json.dumps(res))
"""


def scan_runtime_dirs(project_path: str, is_remote: bool) -> Dict[str, Any]:
    """Scan git-ignored runtime upload/storage directories on Desktop or Laptop."""
    script = RUNTIME_SCAN_SCRIPT.replace("__PDIR__", repr(project_path))
    if is_remote:
        proc = subprocess.run(
            ssh_cmd_args() + ["python3 -"],
            input=script,
            capture_output=True,
            text=True,
        )
    else:
        proc = subprocess.run(
            ["python3", "-"],
            input=script,
            capture_output=True,
            text=True,
        )
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            return json.loads(proc.stdout.strip())
        except Exception:
            pass
    return {}


def run_diff(proj: ProjectInfo, show_secrets: bool = False) -> bool:
    """
    Compare and display all non-Git ('real') differences between Desktop and Laptop for `proj`.
    Returns True if any differences were found, False if everything is in sync.
    """
    if not proj.installed_locally:
        console.print(
            Panel(
                f"[yellow]Project [bold]{proj.name}[/bold] is not installed on this laptop yet.[/yellow]\n"
                f"Run [bold cyan]devclone setup {proj.name}[/bold cyan] to provision it first.",
                title=f"Diff: {proj.name}",
                border_style="yellow",
            )
        )
        return True

    has_any_diff = False
    console.print(f"\n[bold cyan]━━━ Inspecting Non-Git Differences: [white]{proj.name}[/white] ({proj.stack}) ━━━[/bold cyan]")

    # -------------------------------------------------------------------------
    # 1. .env Configuration Diff
    # -------------------------------------------------------------------------
    local_env_path = proj.local_path / ".env"
    local_env = (
        parse_env_content(local_env_path.read_text(encoding="utf-8", errors="replace"))
        if local_env_path.exists()
        else {}
    )
    remote_env = proj.env_vars

    missing_on_laptop = [k for k in remote_env if k not in local_env]
    extra_on_laptop = [k for k in local_env if k not in remote_env]
    changed_keys = [
        k for k in remote_env
        if k in local_env and remote_env[k] != local_env[k]
    ]

    env_table = Table(title="[1] .env Configuration (Desktop vs Laptop)", show_header=True, header_style="bold magenta")
    env_table.add_column("Status", width=18)
    env_table.add_column("Key", style="bold")
    env_table.add_column("Desktop Value")
    env_table.add_column("Laptop Value")

    if not proj.has_env and not local_env_path.exists():
        console.print("  [dim][1] .env: Neither Desktop nor Laptop uses a .env file.[/dim]")
    elif not missing_on_laptop and not extra_on_laptop and not changed_keys:
        console.print(f"  [green]✓ [1] .env Configuration: All {len(remote_env)} keys match identically.[/green]")
    else:
        has_any_diff = True
        for k in missing_on_laptop:
            env_table.add_row("[red]+ Missing on Laptop[/red]", k, mask_value(k, remote_env[k], show_secrets), "[dim]<missing>[/dim]")
        for k in changed_keys:
            env_table.add_row(
                "[yellow]~ Value Differs[/yellow]",
                k,
                mask_value(k, remote_env[k], show_secrets),
                mask_value(k, local_env[k], show_secrets),
            )
        for k in extra_on_laptop:
            env_table.add_row("[cyan]- Extra on Laptop[/cyan]", k, "[dim]<missing>[/dim]", mask_value(k, local_env[k], show_secrets))
        console.print(env_table)

    # -------------------------------------------------------------------------
    # 2. Database Schema, Migrations & Row Count Diff
    # -------------------------------------------------------------------------
    if proj.db_config:
        console.print("  [dim]Inspecting Desktop (read-only) & Laptop databases...[/dim]")
        rem_db = inspect_database_state(proj, is_remote=True)
        loc_db = inspect_database_state(proj, is_remote=False)

        rem_tables = rem_db.get("tables", {})
        loc_tables = loc_db.get("tables", {})
        rem_migs = set(rem_db.get("migrations", []))
        loc_migs = set(loc_db.get("migrations", []))

        db_diffs: List[Tuple[str, str, str, str]] = []

        # Migrations check
        only_rem_migs = sorted(rem_migs - loc_migs)
        only_loc_migs = sorted(loc_migs - rem_migs)
        for m in only_rem_migs:
            db_diffs.append(("[red]Migration Missing on Laptop[/red]", m, "Applied", "Not applied"))
        for m in only_loc_migs:
            db_diffs.append(("[cyan]Migration Only on Laptop[/cyan]", m, "Not applied", "Applied"))

        # Tables & Columns & Rows check
        all_tnames = sorted(set(rem_tables.keys()) | set(loc_tables.keys()))
        for tname in all_tnames:
            if tname not in loc_tables:
                rt = rem_tables[tname]
                r_desc = f"{rt['rows']} rows ({format_bytes(rt['size_bytes'])})" if rt["rows"] >= 0 else format_bytes(rt["size_bytes"])
                db_diffs.append(("[red]Table Missing on Laptop[/red]", tname, r_desc, "[dim]<missing>[/dim]"))
            elif tname not in rem_tables:
                lt = loc_tables[tname]
                l_desc = f"{lt['rows']} rows ({format_bytes(lt['size_bytes'])})" if lt["rows"] >= 0 else format_bytes(lt["size_bytes"])
                db_diffs.append(("[cyan]Table Only on Laptop[/cyan]", tname, "[dim]<missing>[/dim]", l_desc))
            else:
                rt = rem_tables[tname]
                lt = loc_tables[tname]
                # Compare columns if both sides inspected via SQL
                if rt.get("columns") and lt.get("columns") and rt["columns"] != lt["columns"]:
                    r_cols = set(rt["columns"])
                    l_cols = set(lt["columns"])
                    diff_c = (r_cols ^ l_cols)
                    db_diffs.append((
                        "[yellow]Column Schema Diff[/yellow]",
                        tname,
                        f"{len(r_cols)} cols",
                        f"{len(l_cols)} cols ({len(diff_c)} diff)",
                    ))
                # Compare rows or size
                if rt["rows"] >= 0 and lt["rows"] >= 0:
                    if rt["rows"] != lt["rows"]:
                        db_diffs.append((
                            "[yellow]Row Count Diff[/yellow]",
                            tname,
                            f"{rt['rows']:,} rows ({format_bytes(rt['size_bytes'])})",
                            f"{lt['rows']:,} rows ({format_bytes(lt['size_bytes'])})",
                        ))
                else:
                    if rt["size_bytes"] != lt["size_bytes"]:
                        db_diffs.append((
                            "[yellow]Tablespace Size Diff[/yellow]",
                            tname,
                            format_bytes(rt["size_bytes"]),
                            format_bytes(lt["size_bytes"]),
                        ))

        if not db_diffs:
            mig_note = f", {len(rem_migs)} migrations match" if rem_migs else ""
            console.print(
                f"  [green]✓ [2] Database ({proj.db_config.database}): All {len(all_tnames)} tables match in schema & row counts{mig_note}.[/green]"
            )
        else:
            has_any_diff = True
            db_table = Table(
                title=f"[2] Database Differences ({proj.db_config.database} — Desktop [{rem_db.get('method')}] vs Laptop [{loc_db.get('method')}])",
                show_header=True,
                header_style="bold magenta",
            )
            db_table.add_column("Diff Type", width=26)
            db_table.add_column("Table / Migration", style="bold")
            db_table.add_column("Desktop")
            db_table.add_column("Laptop")
            for dtype, item, d_val, l_val in db_diffs:
                db_table.add_row(dtype, item, d_val, l_val)
            console.print(db_table)
    else:
        console.print("  [dim][2] Database: No Docker database service configured.[/dim]")

    # -------------------------------------------------------------------------
    # 3. Untracked / Git-Ignored Runtime Uploads & Storage Diff
    # -------------------------------------------------------------------------
    rem_runtime = scan_runtime_dirs(proj.remote_path, is_remote=True)
    loc_runtime = scan_runtime_dirs(str(proj.local_path), is_remote=False)
    all_rdirs = sorted(set(rem_runtime.keys()) | set(loc_runtime.keys()))

    runtime_diffs: List[Tuple[str, str, str, str]] = []
    for rdir in all_rdirs:
        r_info = rem_runtime.get(rdir, {"count": 0, "total_bytes": 0, "files": {}})
        l_info = loc_runtime.get(rdir, {"count": 0, "total_bytes": 0, "files": {}})
        r_files = r_info.get("files", {})
        l_files = l_info.get("files", {})
        missing_files = [f for f in r_files if f not in l_files]
        size_diff_files = [f for f in r_files if f in l_files and r_files[f] != l_files[f]]
        extra_files = [f for f in l_files if f not in r_files]
        if missing_files or size_diff_files or extra_files:
            runtime_diffs.append((
                rdir,
                f"{r_info['count']} files ({format_bytes(r_info['total_bytes'])})",
                f"{l_info['count']} files ({format_bytes(l_info['total_bytes'])})",
                f"+{len(missing_files)} missing on Laptop, ~{len(size_diff_files)} size diff, -{len(extra_files)} extra",
            ))

    if not all_rdirs:
        console.print("  [dim][3] Runtime Uploads/Storage: No upload/storage directories present.[/dim]")
    elif not runtime_diffs:
        console.print(
            f"  [green]✓ [3] Runtime Uploads/Storage ({', '.join(all_rdirs)}): Identical file counts and sizes.[/green]"
        )
    else:
        has_any_diff = True
        rt_table = Table(title="[3] Untracked Uploads & Storage Differences", show_header=True, header_style="bold magenta")
        rt_table.add_column("Directory", style="bold")
        rt_table.add_column("Desktop")
        rt_table.add_column("Laptop")
        rt_table.add_column("Details")
        for rdir, d_str, l_str, details in runtime_diffs:
            rt_table.add_row(rdir, d_str, l_str, details)
        console.print(rt_table)

    # -------------------------------------------------------------------------
    # 4. Host & Docker Environment Check (/etc/hosts & Volumes)
    # -------------------------------------------------------------------------
    from .setup_flow import check_missing_hosts

    missing_hosts = check_missing_hosts(proj.local_domains)
    missing_vols = [v.name for v in proj.volumes if v.name not in proj.local_volumes]

    if not missing_hosts and not missing_vols:
        console.print("  [green]✓ [4] Host & Docker Config: /etc/hosts domains and named volumes are present on Laptop.[/green]")
    else:
        has_any_diff = True
        if missing_hosts:
            console.print(f"  [yellow]! [4] Missing /etc/hosts domain(s) on Laptop: {', '.join(missing_hosts)}[/yellow]")
        if missing_vols:
            console.print(f"  [red]! [4] Missing Docker volume(s) on Laptop: {', '.join(missing_vols)}[/red]")

    return has_any_diff


def pull_env_interactive(proj: ProjectInfo) -> bool:
    """Interactively merge missing or updated .env keys from Desktop into Laptop's .env."""
    if not proj.installed_locally:
        console.print(f"[red]Project '{proj.name}' is not installed on this laptop yet.[/red]")
        return False

    local_env_path = proj.local_path / ".env"
    local_content = local_env_path.read_text(encoding="utf-8", errors="replace") if local_env_path.exists() else ""
    local_env = parse_env_content(local_content)
    remote_env = proj.env_vars

    missing = [k for k in remote_env if k not in local_env]
    changed = [k for k in remote_env if k in local_env and remote_env[k] != local_env[k] and k not in ("WWWUSER", "WWWGROUP")]

    if not missing and not changed:
        console.print(f"[green]✓ Laptop .env for '{proj.name}' already has all Desktop keys and values![/green]")
        return True

    lines_to_append: List[str] = []
    for k in missing:
        if Confirm.ask(f"Add missing key [bold cyan]{k}[/bold cyan]={mask_value(k, remote_env[k], True)} to Laptop .env?", default=True):
            val = remote_env[k]
            quoted = f'"{val}"' if (" " in val or "#" in val) else val
            lines_to_append.append(f"{k}={quoted}")

    if lines_to_append:
        with local_env_path.open("a", encoding="utf-8") as f:
            if local_content and not local_content.endswith("\n"):
                f.write("\n")
            f.write("\n# Merged from Desktop by devclone\n")
            f.write("\n".join(lines_to_append) + "\n")
        console.print(f"[green]✓ Added {len(lines_to_append)} key(s) to {local_env_path}[/green]")

    for k in changed:
        if Confirm.ask(
            f"Update key [bold yellow]{k}[/bold yellow] from '{local_env[k]}' -> '{remote_env[k]}'?",
            default=False,
        ):
            val = remote_env[k]
            quoted = f'"{val}"' if (" " in val or "#" in val) else val
            text = local_env_path.read_text(encoding="utf-8", errors="replace")
            new_text = re.sub(rf"(?m)^(?:export\s+)?{re.escape(k)}=.*$", f"{k}={quoted}", text)
            local_env_path.write_text(new_text, encoding="utf-8")
            console.print(f"[green]✓ Updated {k} in {local_env_path}[/green]")

    return True
