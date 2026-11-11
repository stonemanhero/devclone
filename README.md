# ⚡ devclone

**Clone complete Docker & local development projects between two devices in one command.**

When you work across two machines, `git clone` only gives you the source code. You still have to copy `.env` files, dump and import databases, move Docker volumes, transfer uploaded media files, rebuild slow Dockerfiles, and update `/etc/hosts`.

**`devclone`** automates all of that over SSH. Point it at your other device, pick a project, and get a fully running local environment—complete with databases, Docker volumes, pre-built images, `.env` configs, and local domains—without ever modifying the source device.

---

## ✨ Why `devclone`?

- **One-Command Environment Cloning**: Copies project files, clones Docker volumes and databases, transfers pre-built Docker images, configures `/etc/hosts`, and boots the containers automatically.
- **100% Read-Only on the Source Device**: Your remote device is never modified. Stopped database volumes are streamed via read-only (`:ro`) mounts, and running databases are dumped live with `mysqldump --single-transaction` without stopping or locking containers.
- **No Broken Dockerfile Builds**: If a project uses a custom-built Docker image (such as Laravel Sail or a custom `Dockerfile`), `devclone` streams the already-built image directly from the source device (`docker save | docker load`) so you never hit broken `apt-get` repositories or wait 10 minutes for a build.
- **Safe One-Time Setup Lock**: Once a project is cloned to your local device, `devclone setup` locks it so you can never accidentally overwrite your local code changes. Use `git` for code, and use `devclone` to inspect or pull database/config/media updates when needed.
- **Environment & Database Comparison (`devclone diff`)**: Instantly compare everything Git ignores between both devices:
  - `.env` keys and values (with automatic secret masking)
  - Database tables, columns, applied migrations, and exact row counts (works even when DB containers are stopped!)
  - Untracked media/storage folders (`wp-content/uploads`, `storage/app/public`)
  - `/etc/hosts` domains and Docker volumes

---

## 🚀 Quick Start

### 1. Requirements

- **Linux** or **macOS** with Python 3.9+
- **Docker** & **Docker Compose** (`docker compose`)
- **OpenSSH** (`ssh`) & **rsync**
- Passwordless SSH key access between your two devices

### 2. Install

```bash
# Clone the repository
git clone https://github.com/stonemanhero/devclone.git
cd devclone

# Create your local configuration from the example template
cp .env.example .env

# Install via pip (or link to ~/.local/bin)
pip install -e .
```

*(Alternatively, without `pip install -e .`: run `pip install -r requirements.txt && ln -s "$(pwd)/devclone" ~/.local/bin/devclone`)*

### 3. Configure SSH & `.env`

If you haven't set up passwordless SSH to your second device yet:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_devclone -N ""
ssh-copy-id -i ~/.ssh/id_devclone.pub user@192.168.1.100
```

Then open `.env` (or run `devclone config --edit`) and set your connection and project paths:

```ini
DEVCLONE_SSH_USER=john
DEVCLONE_SSH_HOST=192.168.1.100
DEVCLONE_SSH_KEY=/home/john/.ssh/id_devclone

DEVCLONE_REMOTE_PROJECTS_DIR=/home/john/Projects
DEVCLONE_LOCAL_PROJECTS_DIR=/home/john/Projects
```

---

## 🛠️ Usage

### Interactive Terminal UI
Run `devclone` with no arguments to open the interactive project browser (`↑`/`↓` to navigate, `/` to search by name or stack, `Enter` to clone an available project or compare an installed one):

```bash
devclone
```

### Commands Overview

| Command | Description |
| :--- | :--- |
| `devclone` | Open the interactive arrow-key terminal browser |
| `devclone list` | List all projects on the remote device and their local status |
| `devclone list --docker` | List only Docker Compose projects |
| `devclone setup <project>` | Clone a project (files + DB/volumes + images + `/etc/hosts` + start) |
| `devclone diff <project>` | Compare `.env`, database schema/rows/migrations, and uploads between devices |
| `devclone pull-db <project>` | Refresh **only** the database from the remote device (leaves code untouched) |
| `devclone pull-env <project>` | Interactively merge missing or updated `.env` variables |
| `devclone pull-uploads <project>` | Sync **only** missing files in `wp-content/uploads` or `storage/app/public` |
| `devclone config` | View active configuration (`--edit` to open `.env` in your editor) |

---

## 📖 Common Workflows

### 1. Cloning a Project for the First Time

```bash
# Pick from an interactive list of available projects
devclone setup

# Or clone a specific project directly (auto-confirm prompts with -y)
devclone setup my-app -y
```

**What happens during `devclone setup`:**
1. Checks if any running local containers use the same ports and offers to stop them.
2. Syncs project files over `rsync` (automatically skipping huge `.zip`/`.tar.gz` backup archives unless `--include-archives` is passed).
3. Clones all Docker volumes and databases (using a live `mysqldump` if the remote DB is running, or a fast read-only volume stream if stopped).
4. Streams any custom-built Docker images from the remote device so you don't have to rebuild them locally.
5. Adds required `.local` domains to `/etc/hosts` and starts the containers.

### 2. Checking Differences Between Devices (`devclone diff`)

Want to know if the other device has new database migrations, extra table rows, updated `.env` keys, or new media uploads?

```bash
# Compare a single project
devclone diff my-app

# Compare all installed projects at once
devclone diff --all

# Show full unmasked values for secret .env keys
devclone diff my-app --show-secrets
```

### 3. Updating Only the Database, `.env`, or Uploads

Once a project is installed locally, use `git pull` / `git push` for code changes. When you want to pull fresh data or configuration from the other device without touching your code:

```bash
# Replace local database with the latest database from the remote device
devclone pull-db my-app

# Interactively merge new .env variables
devclone pull-env my-app

# Copy any new uploaded files (wp-content/uploads, storage/app/public)
devclone pull-uploads my-app
```

---

## ⚙️ Configuration Reference (`.env`)

Your local `.env` file is ignored by Git. See [`.env.example`](.env.example) for the full template with comments:

| Variable | Example | Description |
| :--- | :--- | :--- |
| `DEVCLONE_SSH_USER` | `john` | SSH username on the remote device |
| `DEVCLONE_SSH_HOST` | `192.168.1.100` | IP address or hostname of the remote device |
| `DEVCLONE_SSH_PORT` | `22` | SSH port on the remote device |
| `DEVCLONE_SSH_KEY` | `~/.ssh/id_ed25519` | Path to private SSH key (leave empty to use `~/.ssh/config`) |
| `DEVCLONE_REMOTE_PROJECTS_DIR` | `/home/john/Projects` | Full path to the projects folder on the remote device |
| `DEVCLONE_LOCAL_PROJECTS_DIR` | `/home/john/Projects` | Full path to the projects folder on this device |
| `DEVCLONE_STATE_FILE` | `~/.config/devclone/state.json` | Local registry tracking installed projects |
| `DEVCLONE_SKIP_ARCHIVE_MIN_MB` | `25` | Skip top-level `.zip`/`.tar.gz` archives larger than this size (MB) |

---

## 📄 License

Released under the [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE).
