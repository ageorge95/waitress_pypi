# waitress_pypi

A lightweight, self-hosted private Python package index (PyPI) server backed by [pypiserver](https://pypi.org/project/pypiserver/) and served via [Waitress](https://docs.pylonsproject.org/projects/waitress/) WSGI server. Includes integrated background services for automated package mirroring and rotating backups.

---

## Features

- **Production WSGI Server**: Uses Waitress multi-threaded WSGI server (`0.0.0.0:8080`) for reliable concurrent downloads and uploads.
- **Automated Upstream Mirroring**:
  - Periodically checks upstream repositories (e.g., PyPI or private indexes like Artifactory/Nexus).
  - Supports PEP 503 (HTML) and PEP 691 (JSON) simple indexes.
  - Supports HTTP Basic Authentication, Bearer tokens, and disabling SSL verification for corporate proxies/intranets.
  - Verifies package checksums (SHA256, MD5, etc.) and performs atomic writes.
- **Automated Rotating Backups**:
  - Creates timestamped ZIP archives of the `wheels/` directory.
  - Automatically prunes old backups according to retention policies (`max_backups`).
- **Access Control**:
  - Unauthenticated package browsing and downloads (`pip install`).
  - Apache `.htpasswd` authentication required for uploads and updates (`twine upload`).
- **Orphan Process Protection**:
  - Background workers monitor the parent server process PID and exit cleanly when the server shuts down.

---

## Requirements

- Python 3.10, 3.11, or 3.12 (Python <= 3.9 and 3.13+ are not currently supported by installation checks).
- Windows, Linux, or macOS.

---

## Quick Start

### Windows (Automated)

1. **Install Dependencies & Virtual Environment:**
   Double-click `Install.bat` or run:
   ```cmd
   Install.bat
   ```
   This validates the Python version, creates a `venv` virtual environment, configures the `activate.bat` shortcut, and installs requirements (`pypiserver`, `waitress`, `passlib`).

2. **Generate Credentials (First run):**
   ```cmd
   call activate.bat
   python create_passwords.py
   ```
   *Creates a default `.htpasswd` file with username `anon` and password `password`.*

3. **Start the PyPI Server:**
   Double-click `START_server.bat` or run:
   ```cmd
   START_server.bat
   ```
   The server starts at `http://localhost:8080` along with background backup and mirror workers.

---

### Manual Setup (Cross-Platform / Linux / macOS)

1. **Create and activate virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. **Install dependencies:**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. **Generate password file:**
   ```bash
   python create_passwords.py
   ```

4. **Run the server:**
   ```bash
   python server.py
   ```

---

## Configuration (`config.json`)

All runtime settings for backups and package mirroring are controlled via `config.json`:

```json
{
  "backup_interval_hours": 24,
  "output_dir": "backups",
  "wheels_dir": "wheels",
  "max_backups": 10,
  "backup_run_on_startup": true,
  "mirror_enabled": true,
  "mirror_interval_minutes": 240,
  "mirror_run_on_startup": true,
  "mirror_packages": [
    "requests",
    "numpy"
  ],
  "mirror_sources": [
    "https://pypi.org/simple"
  ]
}
```

### Configuration Options

| Option | Type | Default | Description |
|---|---|---|---|
| `wheels_dir` | string | `"wheels"` | Directory containing served package distributions. |
| `output_dir` | string | `"backups"` | Target folder for backup archives. |
| `backup_interval_hours` | number | `24` | Interval (in hours) between backup snapshots. |
| `backup_run_on_startup` | boolean | `true` | Run a backup immediately when server launches. |
| `max_backups` | integer | `10` | Maximum number of backup archives to keep. |
| `mirror_enabled` | boolean | `true` | Enable or disable the periodic mirroring worker. |
| `mirror_interval_minutes` | number | `240` | Mirroring sync frequency in minutes (can also use `mirror_interval_hours`). |
| `mirror_run_on_startup` | boolean | `true` | Run mirror sync immediately when server launches. |
| `mirror_packages` | list | `[]` | Package names to mirror from upstream sources. |
| `mirror_sources` | list | `[...]` | Upstream repositories to fetch distributions from. |

### Mirror Source Authentication Examples

Upstream sources can be simple strings or structured dictionaries with authentication:

```json
"mirror_sources": [
  "https://pypi.org/simple",
  {
    "url": "https://artifactory.example.com/artifactory/api/pypi/pypi-repo/simple",
    "username": "svc_user",
    "password": "svc_password",
    "verify_ssl": true
  },
  {
    "url": "https://gitlab.example.com/api/v4/projects/123/packages/pypi/simple",
    "token": "glpat-xxxxxxxxxxxxxxxx",
    "verify_ssl": false
  }
]
```

---

## Client Usage

### 1. Installing Packages via Pip

Install a package from your local server:

```bash
pip install <package-name> --index-url http://localhost:8080/simple/ --trusted-host localhost
```

Or add the server as an extra index in addition to official PyPI:

```bash
pip install <package-name> --extra-index-url http://localhost:8080/simple/ --trusted-host localhost
```

### 2. Uploading Packages via Twine

Upload built packages (`.whl` or `.tar.gz`) into the server using credentials defined in `.htpasswd`:

```bash
twine upload --repository-url http://localhost:8080/ -u anon -p password dist/*
```

Alternatively, configure your `~/.pypirc`:

```ini
[distutils]
index-servers =
    local-pypi

[local-pypi]
repository = http://localhost:8080/
username = anon
password = password
```

Then upload with:
```bash
twine upload -r local-pypi dist/*
```

---

## User & Password Management

Passwords are stored in Apache htpasswd format (`.htpasswd`).

- To create or initialize `.htpasswd` with default user (`anon` / `password`), run `python create_passwords.py`.
- To add or modify users manually using Python:
  ```python
  from passlib.apache import HtpasswdFile
  ht = HtpasswdFile('.htpasswd', new=False)
  ht.set_password('username', 'secret_password')
  ht.save()
  ```
- Or use the `htpasswd` CLI tool if installed:
  ```bash
  htpasswd -s .htpasswd <username>
  ```

---

## Running Tests

Unit tests cover the configuration, mirror worker, and backup routines:

```bash
python -m unittest discover -s .
```

---

## Repository Structure

```text
├── config.json              # Server, backup, and mirror configuration
├── requirements.txt         # Python package dependencies
├── server.py                # Main entrypoint: runs Waitress & background workers
├── backup.py                # Scheduled backup generator and cleanup worker
├── mirror_wheels.py         # Scheduled upstream index package mirror worker
├── create_passwords.py      # Utility to initialize .htpasswd credentials
├── install_helper.py        # Python version compatibility verification
├── Install.bat              # Windows setup and installation script
├── START_server.bat         # Windows server startup script
├── test_backup.py           # Unit tests for backup operations
├── test_mirror_wheels.py    # Unit tests for package mirroring
├── wheels/                  # Served distribution packages directory
└── backups/                 # Stored ZIP backup snapshots
```
