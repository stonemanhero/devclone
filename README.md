# `devclone` — Remote Desktop → Laptop Docker Project Cloner & Non-Git Diff Inspector

`devclone` is a terminal CLI and interactive TUI tool for developers who keep multiple Docker Compose and standalone projects on a primary workstation (Desktop) and want to seamlessly provision selected projects onto a secondary machine (Laptop) over SSH—while inspecting **real, non-Git differences** (`.env` variables, database schemas, migrations, row counts, and untracked media uploads) between both machines.

---

## Key Features & Safety Guarantees

1. **100% Read-Only on Remote Desktop**
   - Remote files, `.env` configs, Docker images, volumes, and container states are **never modified or stopped**.
   - **Stopped DB Volumes**: Cloned and inspected via ephemeral `:ro` (read-only) Docker volume mounts.
   - **Live Running DB Containers**: Dumped online using `mysqldump --single-transaction` (with credentials passed securely via `MYSQL_PWD` environment variables) without locking or interrupting the remote database.
   - **Pre-Built Custom Docker Images**: Automatically streams already-built custom images (e.g., Laravel Sail `sail-8.x/app` or custom `Dockerfile` images) directly from Desktop via `docker save | docker load`, avoiding slow or broken legacy `apt-get` builds on the Laptop.

2. **One-Time Setup Guardrail on Laptop**
   - Once a project is provisioned into `<DEVCLONE_LOCAL_PROJECTS_DIR>/<project>`, `devclone setup` **locks it** and refuses to overwrite it.
   - Day-to-day code changes stay strictly in **Git** (`git pull` / `git push`).
   - Non-code drift between Desktop and Laptop is inspected at any time with `devclone diff` or selectively refreshed with `devclone pull-db`, `devclone pull-env`, and `devclone pull-uploads`.

3. **Deep Non-Git ("Real") Diff Engine (`devclone diff`)**
   - **`.env` Drift**: Highlights keys missing on Laptop, extra keys on Laptop, and differing values (automatically masking secrets like `PASSWORD`, `SECRET`, `KEY`, `TOKEN` with short SHA-256 fingerprints).
   - **Database Drift**: Compares applied migrations (e.g. Laravel `migrations` table), missing/extra tables, column schema differences, and exact table row counts—even when database containers are stopped on either machine.
   - **Untracked Runtime Uploads**: Compares git-ignored media/storage directories (`wp-content/uploads`, `storage/app/public`).
   - **Host & Volume Checks**: Verifies custom `.local` domains in `/etc/hosts` and required Docker volumes.

---

## Requirements

- **Linux / macOS** with Python 3.9+
- **Docker** & **Docker Compose** (`docker compose`) installed on both machines
- **OpenSSH** (`ssh`) & **rsync**
- Passwordless SSH key access from Laptop → Desktop

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/stonemanhero/devclone.git
cd devclone

# 2. Create your local .env configuration from the template
cp .env.example .env

# 3. Edit .env with your SSH user, remote IP, and project directories
nano .env
```

### Install Python Dependencies & CLI Command

Using `pip` or `pipx`:
```bash
pip install -e .
```

Or link the executable script directly into `~/.local/bin`:
```bash
pip install -r requirements.txt
chmod +x devclone
ln -s "$(pwd)/devclone" ~/.local/bin/devclone
```

---

## Passwordless SSH Setup (Recommended)

`devclone` uses non-interactive SSH (`BatchMode=yes`). If you haven't set up a dedicated passwordless SSH key between your Laptop and Desktop yet:

```bash
# Generate a dedicated Ed25519 key (press Enter for no passphrase)
ssh-keygen -t ed25519 -f ~/.ssh/id_desktop -N ""

# Copy the public key to your remote Desktop
ssh-copy-id -i ~/.ssh/id_desktop.pub user@192.168.1.100
```

Then set `DEVCLONE_SSH_KEY=/home/<user>/.ssh/id_desktop` in your `.env` file.

---

## Configuration (`.env`)

All settings are configured via `.env` (which is git-ignored). See [`.env.example`](.env.example) for a fully commented template:

```ini
# --- 1. SSH Connection to Remote Desktop ---
DEVCLONE_SSH_USER=john
DEVCLONE_SSH_HOST=192.168.1.100
DEVCLONE_SSH_PORT=22
DEVCLONE_SSH_TARGET=
DEVCLONE_SSH_KEY=/home/john/.ssh/id_ed25519
DEVCLONE_SSH_CONNECT_TIMEOUT=8

# --- 2. Remote & Local Project Directories (Full Paths) ---
DEVCLONE_REMOTE_PROJECTS_DIR=/home/john/Projects
DEVCLONE_LOCAL_PROJECTS_DIR=/home/john/Projects

# --- 3. Local State Registry ---
DEVCLONE_STATE_FILE=/home/john/.config/devclone/state.json

# --- 4. Docker Volume, Database & File Sync Settings ---
DEVCLONE_REMOTE_HELPER_IMAGE=redis:alpine
DEVCLONE_LOCAL_HELPER_IMAGE=redis:alpine
DEVCLONE_MAX_EPHEMERAL_SQL_MB=4500
DEVCLONE_SKIP_ARCHIVE_MIN_MB=25
DEVCLONE_ARCHIVE_EXCLUDE_PATTERNS=backup.zip,*-backup.zip,*.tar.gz,*.sql.gz
```

You can also view or edit the active configuration at any time via the CLI:
```bash
# Display active configuration table
devclone config

# Open the active .env file in $EDITOR (or nano)
devclone config --edit
```

---

## Usage & Commands

### 1. Interactive Terminal UI (TUI)
Launch the full-screen arrow-key selector (`↑`/`↓` to navigate, `/` to live-filter by name or stack, `Enter` to set up an available project or diff an installed project):
```bash
devclone
```

### 2. List Projects (`devclone list`)
Discover all remote projects on Desktop, their detected stack (Laravel Sail, WordPress, Next.js, etc.), file sizes, Docker volume sizes, container statuses, and Laptop installation state:
```bash
devclone list
devclone list --docker       # Only Docker Compose projects
devclone list --available    # Only projects not yet set up on Laptop
devclone list --installed    # Only projects already installed on Laptop
```

### 3. One-Time Project Setup (`devclone setup`)
Provision a project from Desktop onto Laptop (syncs project files, clones Docker volumes/databases read-only, streams custom pre-built Docker images, configures `.env` and `/etc/hosts`, and boots containers):
```bash
devclone setup                   # Interactive picker of available projects
devclone setup <project-name>    # Set up a specific project
devclone setup <project-name> -y # Non-interactive (auto-confirm prompts)
```
**Useful flags:**
- `--no-start`: Sync files and Docker volumes without starting containers.
- `--include-archives`: Include large top-level `.zip`/`.tar.gz` backup archives (skipped by default).
- `--sql-dump`: Force online `mysqldump` streaming.
- `--with-tunnel`: Start `cloudflared` tunnel containers if defined in `docker-compose.yml` (skipped by default on Laptop).

### 4. Inspect Non-Git Differences (`devclone diff`)
Compare `.env` variables, database tables/columns/migrations/row counts, untracked uploads, and `/etc/hosts` between Desktop and Laptop:
```bash
devclone diff                    # Interactive picker of installed projects
devclone diff <project-name>     # Diff a specific project
devclone diff --all              # Diff all installed projects
devclone diff <project> --show-secrets  # Unmask sensitive .env values
```

### 5. Targeted Non-Code Sync Helpers
When a project is already installed on your Laptop and you only want to refresh non-Git state from Desktop without touching your local code files:
```bash
# Refresh ONLY the database / DB volume from Desktop
devclone pull-db <project-name>

# Interactively merge missing or updated .env keys from Desktop
devclone pull-env <project-name>

# Sync ONLY missing files in wp-content/uploads or storage/app/public
devclone pull-uploads <project-name>
```

---

## License

Released under the [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE).
