
import csv
import difflib
import hashlib
import json
import sqlite3
import subprocess
import time
import zipfile

from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# IGNITION BACKUP MANAGER V3
# ============================================================

ROOT = Path(__file__).resolve().parent

LIVE = ROOT / "live"
DEMO = ROOT / "demo"

AUDIT_DB = ROOT / "ignition_audit.sqlite3"
CSV_FILE = ROOT / "ignition_change_history.csv"

MONITOR_INTERVAL = 10
MAX_TEXT_BYTES = 256_000
MAX_DIFF_LINES = 500


# ============================================================
# DISPLAY HELPERS
# ============================================================

def heading(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def pause():
    input("\nPress Enter to continue...")


def choose(prompt, options):
    for number, option in enumerate(options, 1):
        print(f"{number}. {option}")

    print("0. Back")

    while True:
        value = input(f"\n{prompt}: ").strip()

        if value == "0":
            return None

        if value.isdecimal():
            index = int(value) - 1

            if 0 <= index < len(options):
                return index

        print("Invalid selection.")


def timestamp():
    return datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")


# ============================================================
# HASHING AND FILE READING
# ============================================================

def hash_stream(stream):
    digest = hashlib.sha256()
    total = 0
    small_data = bytearray()

    while True:
        chunk = stream.read(1024 * 1024)

        if not chunk:
            break

        digest.update(chunk)
        total += len(chunk)

        if total <= MAX_TEXT_BYTES:
            small_data.extend(chunk)
        else:
            small_data.clear()

    data = (
        bytes(small_data)
        if total <= MAX_TEXT_BYTES
        else None
    )

    return digest.hexdigest(), total, data


def read_file(path):
    with path.open("rb") as stream:
        return hash_stream(stream)


def decode_text(data):
    if data is None or b"\x00" in data:
        return None

    try:
        return data.decode("utf-8-sig")
    except UnicodeError:
        return None


def parse_json(data):
    if data is None:
        return None

    try:
        text = data.decode("utf-8-sig")
        return json.loads(text)
    except (UnicodeError, ValueError):
        return None


def canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False
    )


def semantic_json_hash(data):
    parsed = parse_json(data)

    if parsed is None:
        return None

    try:
        normalized = canonical_json(parsed)
    except (ValueError, TypeError):
        return None

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def snapshot_from_stream(stream, name):
    raw_hash, size, data = hash_stream(stream)

    text = decode_text(data)

    if name.lower().endswith(".json"):
        json_hash = semantic_json_hash(data)

        if json_hash is not None:
            return json_hash, size, text

    return raw_hash, size, text


def snapshot_file(path):
    with path.open("rb") as stream:
        return snapshot_from_stream(
            stream,
            path.name
        )


# ============================================================
# JSON COMPARISON
# ============================================================

def json_path(parent, key):
    if isinstance(key, int):
        return f"{parent}[{key}]"

    if not parent:
        return str(key)

    return f"{parent}.{key}"


def json_value(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True
    )


def json_differences(before, after, path="$"):
    changes = []

    if isinstance(before, dict) and isinstance(after, dict):
        keys = sorted(
            before.keys() | after.keys()
        )

        for key in keys:
            current_path = json_path(path, key)

            if key not in before:
                changes.append((
                    "ADDED",
                    current_path,
                    None,
                    after[key]
                ))

            elif key not in after:
                changes.append((
                    "REMOVED",
                    current_path,
                    before[key],
                    None
                ))

            else:
                changes.extend(
                    json_differences(
                        before[key],
                        after[key],
                        current_path
                    )
                )

    elif isinstance(before, list) and isinstance(after, list):
        length = max(len(before), len(after))

        for index in range(length):
            current_path = json_path(path, index)

            if index >= len(before):
                changes.append((
                    "ADDED",
                    current_path,
                    None,
                    after[index]
                ))

            elif index >= len(after):
                changes.append((
                    "REMOVED",
                    current_path,
                    before[index],
                    None
                ))

            else:
                changes.extend(
                    json_differences(
                        before[index],
                        after[index],
                        current_path
                    )
                )

    elif (
        type(before) is not type(after)
        or before != after
    ):
        changes.append((
            "MODIFIED",
            path,
            before,
            after
        ))

    return changes


def show_json_diff(before_text, after_text):
    try:
        before = json.loads(before_text)
        after = json.loads(after_text)

    except (TypeError, ValueError):
        print("Unable to parse JSON for detailed comparison.")
        return

    changes = json_differences(before, after)

    if not changes:
        print("JSON content is identical.")
        return

    print(f"\n{len(changes)} JSON property changes found.\n")

    for action, path, old_value, new_value in changes:
        print("-" * 60)
        print(f"[{action}] {path}")

        if action in ("REMOVED", "MODIFIED"):
            print("BEFORE:", json_value(old_value))

        if action in ("ADDED", "MODIFIED"):
            print("AFTER: ", json_value(new_value))

    print("-" * 60)


# ============================================================
# GENERAL TEXT DIFFERENCES
# ============================================================

def show_text_diff(before, after, before_name, after_name):
    if before is None or after is None:
        print("Text comparison unavailable.")
        return

    differences = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=before_name,
        tofile=after_name
    )

    found = False

    for index, line in enumerate(differences):
        if index >= MAX_DIFF_LINES:
            print("\nAdditional differences omitted.")
            break

        found = True
        print(line, end="" if line.endswith("\n") else "\n")

    if not found:
        print("No text differences.")


def show_content_diff(name, before, after):
    heading(f"DIFFERENCES: {name}")

    if name.lower().endswith(".json"):
        show_json_diff(before, after)

    else:
        show_text_diff(
            before,
            after,
            "BEFORE",
            "AFTER"
        )


# ============================================================
# FIND PROJECT FILES
# ============================================================

def relative_files(folder, json_only=False):
    files = {}

    if not folder.is_dir():
        return files

    for path in folder.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue

        if json_only and path.suffix.lower() != ".json":
            continue

        name = path.relative_to(folder).as_posix()
        files[name] = path

    return files


def compare_snapshots(first, second):
    if first is None:
        return "ONLY DEMO"

    if second is None:
        return "ONLY LIVE"

    if first[0] == second[0]:
        return "IDENTICAL"

    return "DIFFERENT"


# ============================================================
# COMPARE LIVE AND DEMO FOLDERS
# ============================================================

def compare_folders(live_folder, demo_folder, json_only=False):
    title = (
        "JSON COMPARISON: LIVE VS DEMO"
        if json_only
        else "FILE COMPARISON: LIVE VS DEMO"
    )

    heading(title)

    live_files = relative_files(
        live_folder,
        json_only
    )

    demo_files = relative_files(
        demo_folder,
        json_only
    )

    names = sorted(
        live_files.keys() | demo_files.keys(),
        key=str.casefold
    )

    if not names:
        print("No matching files found.")
        return

    differences = []
    counts = {}

    for name in names:
        live_path = live_files.get(name)
        demo_path = demo_files.get(name)

        try:
            live_snapshot = (
                snapshot_file(live_path)
                if live_path else None
            )

            demo_snapshot = (
                snapshot_file(demo_path)
                if demo_path else None
            )

            status = compare_snapshots(
                live_snapshot,
                demo_snapshot
            )

        except OSError as error:
            status = "ERROR"
            print(f"[ACCESS ERROR] {name}: {error}")
            continue

        counts[status] = counts.get(status, 0) + 1
        print(f"[{status}] {name}")

        if status == "DIFFERENT":
            differences.append((
                name,
                live_snapshot[2],
                demo_snapshot[2]
            ))

    heading("COMPARISON SUMMARY")

    for status, count in counts.items():
        print(f"{status}: {count}")

    if not differences:
        return

    while True:
        print()
        selection = choose(
            "Select a file to inspect",
            [name for name, _, _ in differences]
        )

        if selection is None:
            return

        name, before, after = differences[selection]

        show_content_diff(name, before, after)


# ============================================================
# ZIP FILE HELPERS
# ============================================================

def zip_members(archive, json_only=False):
    result = {}

    for member in archive.infolist():
        if member.is_dir():
            continue

        if json_only and not member.filename.lower().endswith(".json"):
            continue

        if member.filename in result:
            raise ValueError(
                f"Duplicate ZIP entry: {member.filename}"
            )

        result[member.filename] = member

    return result


def zip_browse(path):
    heading(f"ZIP CONTENTS: {path.name}")

    if not path.is_file():
        print("ZIP file not found.")
        return

    try:
        with zipfile.ZipFile(path) as archive:
            for member in sorted(
                archive.infolist(),
                key=lambda item: item.filename.casefold()
            ):
                if member.is_dir():
                    print("[DIR]", member.filename)
                else:
                    print(
                        f"{member.file_size:>10} bytes "
                        f"{member.filename}"
                    )

    except (OSError, zipfile.BadZipFile) as error:
        print("ZIP error:", error)


# ============================================================
# COMPARE ZIP BACKUPS
# ============================================================

def compare_zips(live_zip, demo_zip, json_only=False):
    heading(
        "JSON INSIDE ZIP COMPARISON"
        if json_only
        else "ZIP CONTENT COMPARISON"
    )

    print("LIVE:", live_zip.name)
    print("DEMO:", demo_zip.name)

    if not live_zip.is_file() or not demo_zip.is_file():
        print("Both ZIP files must exist.")
        return

    differences = []
    counts = {}

    try:
        with zipfile.ZipFile(live_zip) as live_archive:
            with zipfile.ZipFile(demo_zip) as demo_archive:

                live_members = zip_members(
                    live_archive,
                    json_only
                )

                demo_members = zip_members(
                    demo_archive,
                    json_only
                )

                names = sorted(
                    live_members.keys() | demo_members.keys(),
                    key=str.casefold
                )

                for name in names:
                    live_snapshot = None
                    demo_snapshot = None

                    if name in live_members:
                        with live_archive.open(
                            live_members[name]
                        ) as stream:
                            live_snapshot = snapshot_from_stream(
                                stream,
                                name
                            )

                    if name in demo_members:
                        with demo_archive.open(
                            demo_members[name]
                        ) as stream:
                            demo_snapshot = snapshot_from_stream(
                                stream,
                                name
                            )

                    status = compare_snapshots(
                        live_snapshot,
                        demo_snapshot
                    )

                    counts[status] = counts.get(status, 0) + 1

                    print(f"[{status}] {name}")

                    if status == "DIFFERENT":
                        differences.append((
                            name,
                            live_snapshot[2],
                            demo_snapshot[2]
                        ))

    except (
        OSError,
        zipfile.BadZipFile,
        RuntimeError,
        ValueError,
        NotImplementedError
    ) as error:
        print("ZIP comparison error:", error)
        return

    heading("ZIP COMPARISON SUMMARY")

    for status, count in counts.items():
        print(f"{status}: {count}")

    while differences:
        selection = choose(
            "Inspect changed ZIP member",
            [name for name, _, _ in differences]
        )

        if selection is None:
            return

        name, before, after = differences[selection]
        show_content_diff(name, before, after)


# ============================================================
# SELECT ZIP BACKUPS
# ============================================================

def find_zips(folder):
    if not folder.is_dir():
        return []

    return sorted(
        [
            path
            for path in folder.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() == ".zip"
        ],
        key=lambda path: path.as_posix().casefold()
    )


def select_zip_pair(json_only=False):
    live_zips = find_zips(LIVE)
    demo_zips = find_zips(DEMO)

    if not live_zips or not demo_zips:
        print("ZIP files must exist in LIVE and DEMO.")
        return

    heading("SELECT LIVE ZIP")

    live_selection = choose(
        "Choose LIVE backup",
        [
            str(path.relative_to(LIVE))
            for path in live_zips
        ]
    )

    if live_selection is None:
        return

    heading("SELECT DEMO ZIP")

    demo_selection = choose(
        "Choose DEMO backup",
        [
            str(path.relative_to(DEMO))
            for path in demo_zips
        ]
    )

    if demo_selection is None:
        return

    live_zip = live_zips[live_selection]
    demo_zip = demo_zips[demo_selection]

    while True:
        heading("ZIP BACKUP EXPLORER")

        selection = choose(
            "Select action",
            [
                "View LIVE ZIP contents",
                "View DEMO ZIP contents",
                "Compare all ZIP contents",
                "Compare JSON inside ZIPs"
            ]
        )

        if selection is None:
            return

        if selection == 0:
            zip_browse(live_zip)

        elif selection == 1:
            zip_browse(demo_zip)

        elif selection == 2:
            compare_zips(live_zip, demo_zip)

        elif selection == 3:
            compare_zips(
                live_zip,
                demo_zip,
                json_only=True
            )

        pause()


# ============================================================
# PROJECT EXPLORER
# ============================================================

def show_tree(folder):
    heading(f"FOLDER: {folder}")

    if not folder.is_dir():
        print("Folder not found.")
        return

    for path in sorted(
        folder.rglob("*"),
        key=lambda p: p.as_posix().casefold()
    ):
        if path.is_symlink():
            continue

        relative = path.relative_to(folder)
        indent = "  " * (len(relative.parts) - 1)

        if path.is_dir():
            print(f"{indent}[DIR] {path.name}")
        else:
            print(f"{indent}{path.name}")


def explore():
    while True:
        heading("PROJECT EXPLORER")

        projects = set()

        for base in (LIVE, DEMO):
            if not base.is_dir():
                continue

            for path in base.iterdir():
                if path.is_dir() and not path.is_symlink():
                    projects.add(path.name)

        projects = sorted(projects, key=str.casefold)

        options = [
            f"Project: {name}"
            for name in projects
        ]

        options.append("Explore and compare ZIP backups")

        selection = choose(
            "Select project",
            options
        )

        if selection is None:
            return

        if selection == len(projects):
            select_zip_pair()
            continue

        project = projects[selection]

        show_tree(LIVE / project)
        show_tree(DEMO / project)

        action = choose(
            "Project comparison",
            [
                "Compare all files",
                "Compare JSON files only"
            ]
        )

        if action == 0:
            compare_folders(
                LIVE / project,
                DEMO / project
            )

        elif action == 1:
            compare_folders(
                LIVE / project,
                DEMO / project,
                json_only=True
            )

        pause()


# ============================================================
# GIT HELPER
# ============================================================

def git(*args, capture=False):
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
        check=False
    )


# ============================================================
# GITHUB UPLOAD
# ============================================================

def upload(scope):
    heading(f"UPLOAD {scope.upper()} TO GITHUB")

    targets = (
        ["live", "demo"]
        if scope == "both"
        else [scope]
    )

    for target in targets:
        if not (ROOT / target).is_dir():
            print("Missing folder:", target)
            return

    try:
        repository = git(
            "rev-parse",
            "--show-toplevel",
            capture=True
        )

    except FileNotFoundError:
        print("Git is not installed.")
        return

    if repository.returncode:
        print("Not a Git repository.")
        return

    if Path(repository.stdout.strip()).resolve() != ROOT:
        print("Script must be in the Git repository root.")
        return

    branch = git(
        "branch",
        "--show-current",
        capture=True
    )

    if branch.stdout.strip() != "main":
        print("Switch to the main branch first.")
        return

    conflicts = git(
        "ls-files",
        "-u",
        capture=True
    )

    if conflicts.returncode or conflicts.stdout.strip():
        print("Resolve Git conflicts first.")
        return

    staged = git(
        "diff",
        "--cached",
        "--name-only",
        capture=True
    )

    if staged.returncode or staged.stdout.strip():
        print("Commit or unstage existing changes first.")
        return

    print("Checking GitHub...")

    fetch = git("fetch", "origin")

    if fetch.returncode:
        print("GitHub fetch failed.")
        return

    remote = git(
        "rev-parse",
        "--verify",
        "refs/remotes/origin/main",
        capture=True
    )

    if remote.returncode:
        print("origin/main not found.")
        return

    ancestor = git(
        "merge-base",
        "--is-ancestor",
        "origin/main",
        "HEAD"
    )

    if ancestor.returncode:
        print("Remote changes detected.")
        print("Sync using GitHub Desktop first.")
        return

    stage = git(
        "add",
        "-A",
        "--",
        *targets
    )

    if stage.returncode:
        print("Unable to stage project files.")
        return

    changes = git(
        "diff",
        "--cached",
        "--name-status",
        "--",
        *targets,
        capture=True
    )

    if changes.returncode:
        print("Unable to inspect staged changes.")
        return

    if not changes.stdout.strip():
        print("No changes to upload.")
        return

    print("\nChanges:")
    print(changes.stdout)

    confirm = input(
        "\nCommit and upload? (y/N): "
    ).strip().lower()

    if confirm != "y":
        git(
            "restore",
            "--staged",
            "--",
            *targets
        )
        print("Upload cancelled.")
        return

    comment = input(
        "Enter backup comment: "
    ).strip()[:160]

    if not comment:
        comment = "Ignition backup update"

    commit = git(
        "commit",
        "-m",
        f"{scope.upper()} - {comment}",
        "--only",
        "--",
        *targets
    )

    if commit.returncode:
        print("Git commit failed.")
        return

    result = git(
        "push",
        "origin",
        "main"
    )

    if result.returncode:
        print("GitHub push failed.")
        print("Your local commit has been preserved.")
    else:
        print("GitHub upload successful!")


# ============================================================
# AUDIT DATABASE
# ============================================================

def audit_connection():
    db = sqlite3.connect(AUDIT_DB)

    db.execute("PRAGMA busy_timeout=5000")

    db.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            path TEXT PRIMARY KEY,
            digest TEXT NOT NULL,
            size INTEGER NOT NULL,
            text_content TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL,
            path TEXT NOT NULL,
            action TEXT NOT NULL,
            before_text TEXT,
            after_text TEXT,
            old_hash TEXT,
            new_hash TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS audit_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    db.commit()
    return db


# ============================================================
# BUILD AUDIT INVENTORY
# ============================================================

def build_inventory():
    inventory = {}
    errors = []

    for base in (LIVE, DEMO):
        if not base.is_dir():
            errors.append(f"Missing folder: {base}")
            continue

        try:
            paths = list(base.rglob("*"))
        except OSError as error:
            errors.append(f"{base}: {error}")
            continue

        for path in paths:
            name = path.relative_to(ROOT).as_posix()

            try:
                if path.is_symlink() or not path.is_file():
                    continue

                inventory[name] = snapshot_file(path)

                # Inspect JSON and other files inside ZIPs.
                if path.suffix.lower() == ".zip":
                    with zipfile.ZipFile(path) as archive:
                        members = zip_members(archive)

                        for member_name, member in members.items():
                            audit_name = (
                                name + "::" + member_name
                            )

                            with archive.open(member) as stream:
                                inventory[audit_name] = (
                                    snapshot_from_stream(
                                        stream,
                                        member_name
                                    )
                                )

            except PermissionError as error:
                errors.append(
                    f"[ACCESS DENIED] {name}: {error}"
                )

            except (
                OSError,
                zipfile.BadZipFile,
                RuntimeError,
                ValueError,
                NotImplementedError
            ) as error:
                errors.append(f"{name}: {error}")

    return inventory, errors


# ============================================================
# SCAN AND RECORD CHANGES
# ============================================================

def scan_changes(silent=False):
    if not silent:
        heading("SCAN LIVE AND DEMO FOR CHANGES")

    inventory, errors = build_inventory()

    if errors:
        for error in errors:
            print("[SCAN ERROR]", error)

        print("\nScan incomplete.")
        print("Audit history was not updated.")
        return

    with audit_connection() as db:
        previous = {
            row[0]: row[1:]
            for row in db.execute("""
                SELECT path, digest, size, text_content
                FROM snapshots
            """)
        }

        initialized = db.execute("""
            SELECT value
            FROM audit_settings
            WHERE key='initialized'
        """).fetchone()

        baseline = initialized is None
        changes = []

        all_names = sorted(
            previous.keys() | inventory.keys(),
            key=str.casefold
        )

        for name in all_names:
            before = previous.get(name)
            after = inventory.get(name)

            if before is None:
                action = "ADDED"

            elif after is None:
                action = "DELETED"

            elif before[0] != after[0]:
                action = "MODIFIED"

            else:
                continue

            changes.append((
                name,
                action,
                before,
                after
            ))

        detected = timestamp()

        with db:
            if baseline:
                db.execute("""
                    INSERT INTO audit_settings(key, value)
                    VALUES ('initialized', '1')
                """)

            for name, action, before, after in changes:
                if not baseline:
                    db.execute("""
                        INSERT INTO events (
                            detected_at,
                            path,
                            action,
                            before_text,
                            after_text,
                            old_hash,
                            new_hash
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        detected,
                        name,
                        action,
                        before[2] if before else None,
                        after[2] if after else None,
                        before[0] if before else None,
                        after[0] if after else None
                    ))

                if after is None:
                    db.execute(
                        "DELETE FROM snapshots WHERE path=?",
                        (name,)
                    )

                else:
                    db.execute("""
                        INSERT INTO snapshots (
                            path,
                            digest,
                            size,
                            text_content
                        )
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(path)
                        DO UPDATE SET
                            digest=excluded.digest,
                            size=excluded.size,
                            text_content=excluded.text_content
                    """, (
                        name,
                        *after
                    ))

    if baseline:
        print(
            f"Baseline created for {len(inventory)} "
            "files and ZIP members."
        )

    elif not changes:
        if not silent:
            print("No changes detected.")

    else:
        for name, action, _, _ in changes:
            print(f"[{action}] {name}")

        print(f"\n{len(changes)} changes recorded.")
        print(f"Detected at: {detected} UTC")


# ============================================================
# AUDIT CHANGE HISTORY
# ============================================================

def view_change_history(json_only=False):
    heading(
        "JSON CHANGE HISTORY"
        if json_only
        else "ALL CHANGE HISTORY"
    )

    with audit_connection() as db:
        rows = db.execute("""
            SELECT id, detected_at, path, action
            FROM events
            ORDER BY id DESC
        """).fetchall()

    if json_only:
        rows = [
            row for row in rows
            if row[2].lower().endswith(".json")
        ]

    rows = rows[:100]

    if not rows:
        print("No recorded changes found.")
        return

    for event_id, detected, path, action in rows:
        print(
            f"{event_id} | {detected} | "
            f"[{action}] {path}"
        )

    while True:
        answer = input(
            "\nEnter event ID to inspect "
            "(Enter to return): "
        ).strip()

        if not answer:
            return

        if not answer.isdecimal():
            print("Enter a valid event ID.")
            continue

        with audit_connection() as db:
            event = db.execute("""
                SELECT path, action, before_text, after_text
                FROM events
                WHERE id=?
            """, (int(answer),)).fetchone()

        if event is None:
            print("Event not found.")
            continue

        path, action, before, after = event

        heading(f"{action}: {path}")

        if before is None and after is None:
            print(
                "No text snapshot available. "
                "File may be binary or too large."
            )
            continue

        if path.lower().endswith(".json"):
            if action == "ADDED":
                show_json_diff("{}", after)

            elif action == "DELETED":
                show_json_diff(before, "{}")

            else:
                show_json_diff(before, after)

        else:
            show_text_diff(
                before or "",
                after or "",
                "BEFORE",
                "AFTER"
            )


# ============================================================
# GIT COMMIT HISTORY
# ============================================================

def view_git_history():
    heading("GIT COMMIT HISTORY")

    try:
        result = git(
            "log",
            "-20",
            "--date=iso-strict",
            "--pretty=format:%h | %ad | %an <%ae> | %s",
            "--",
            "live",
            "demo",
            capture=True
        )

    except FileNotFoundError:
        print("Git is not installed.")
        return

    if result.returncode:
        print("Unable to read Git history.")
        print(result.stderr)
        return

    print(
        result.stdout
        or "No commits found."
    )

    print(
        "\nCommit authors are not necessarily "
        "the people who originally edited files."
    )


# ============================================================
# MONITOR FOLDERS
# ============================================================

def monitor_folders():
    heading("LIVE AND DEMO MONITOR")

    print(
        f"Scanning every {MONITOR_INTERVAL} seconds."
    )
    print("Press Ctrl+C to stop.")

    try:
        while True:
            scan_changes(silent=True)
            time.sleep(MONITOR_INTERVAL)

    except KeyboardInterrupt:
        print("\nMonitoring stopped.")


# ============================================================
# EXPORT AUDIT HISTORY
# ============================================================

def export_audit_csv():
    heading("EXPORT AUDIT HISTORY")

    with audit_connection() as db:
        rows = db.execute("""
            SELECT
                id,
                detected_at,
                path,
                action,
                old_hash,
                new_hash
            FROM events
            ORDER BY id
        """).fetchall()

    with CSV_FILE.open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:
        writer = csv.writer(file)

        writer.writerow([
            "Event ID",
            "Detected At UTC",
            "Path",
            "Action",
            "Old Hash",
            "New Hash"
        ])

        writer.writerows(rows)

    print(f"Exported {len(rows)} events.")
    print("Saved to:", CSV_FILE)


# ============================================================
# MAIN MENU
# ============================================================

def main():
    heading("IGNITION BACKUP MANAGER V3")

    print("Repository:", ROOT)

    while True:
        heading("MAIN MENU")

        options = [
            "Upload LIVE to GitHub",
            "Upload DEMO to GitHub",
            "Compare ALL LIVE and DEMO files",
            "Upload BOTH folders to GitHub",
            "Explore project folders and ZIP archives",
            "Scan for changes",
            "View highlighted change history",
            "View Git commit history",
            "Monitor LIVE and DEMO folders",
            "Export audit history to CSV",
            "Compare JSON files between LIVE and DEMO",
            "View JSON change history",
            "Compare JSON inside ZIP backups",
            "Exit"
        ]

        selection = choose(
            "Select option",
            options
        )

        if selection is None or selection == 13:
            print("Goodbye!")
            return

        if selection == 0:
            upload("live")

        elif selection == 1:
            upload("demo")

        elif selection == 2:
            compare_folders(LIVE, DEMO)

        elif selection == 3:
            upload("both")

        elif selection == 4:
            explore()

        elif selection == 5:
            scan_changes()

        elif selection == 6:
            view_change_history()

        elif selection == 7:
            view_git_history()

        elif selection == 8:
            monitor_folders()

        elif selection == 9:
            export_audit_csv()

        elif selection == 10:
            compare_folders(
                LIVE,
                DEMO,
                json_only=True
            )

        elif selection == 11:
            view_change_history(
                json_only=True
            )

        elif selection == 12:
            select_zip_pair(
                json_only=True
            )

        pause()


# ============================================================
# START APPLICATION
# ============================================================

if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("\nApplication cancelled.")

    except EOFError:
        print("\nInput ended.")
