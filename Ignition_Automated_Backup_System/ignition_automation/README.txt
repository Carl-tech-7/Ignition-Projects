IGNITION BACKUP MANAGER - SETUP
================================
1. Read SECURITY.txt. Get company approval. Confirm GitHub repository is private.
2. Copy backup_to_github.bat, backup_manager.py, config.json and install_daily_task.bat
   into C:\Users\CarlVenter\OneDrive - Concera\Desktop\Ignition-Projects
3. Review/edit config.json. The GitHub remote is the existing origin/main.
4. Create C:\IgnitionBackupInbox\Projects and C:\IgnitionBackupInbox\Gateway.
5. Ignition 8.3 Gateway web UI: Platform > System > Gateway Settings > Scheduled Backups.
   Enable scheduled backup; schedule 17:30 daily (cron: 30 17 * * *);
   destination should be a path accessible by the Gateway SERVICE (not necessarily
   the path on your desktop). If Gateway is remote, have IT arrange a secured
   network share/copy into C:\IgnitionBackupInbox\Gateway. Set retention.
6. For individual project exports, arrange an approved export process that places
   project ZIPs into C:\IgnitionBackupInbox\Projects. This package DOES NOT
   silently access a remote Gateway or export projects itself.
   A full Gateway backup includes all projects, but not independent ZIP exports.
7. Run backup_to_github.bat manually. Review console and logs. If successful,
   run install_daily_task.bat (18:00 every day) and test via Task Scheduler.
8. Project notes: edit Projects\<ProjectName>\Project_Notes.txt in the Git repo.
   The script will not overwrite notes. They are tracked by Git.
9. See Logs in logs\YYYY-MM-DD.log. Failed pushes are not reported as success.

PROJECT ZIP INPUT FORMAT
- Preferred: Projects\<ProjectName>\backup.zip (or any .zip name).
- Also accepted: Projects\<ProjectName>.zip (project name from filename).
- The script stores only the newest distinct ZIP per project in
  Projects\<ProjectName>\latest.zip, based on SHA256 of ZIP bytes.
- Identical ZIP content with a new timestamped filename is skipped.
- Different ZIP bytes may still contain identical Ignition resources;
  this tool does not semantically compare inside ZIPs.
- Missing projects are FLAGGED after 2 consecutive successful inbox scans;
  nothing is automatically deleted from the repository.
- Gateway .gwbk files are copied to local GatewayBackups (gitignored),
  never pushed to GitHub by default.
- Project ZIPs >90 MB are not committed; review log and use approved storage.

LIMITATIONS
- The desktop computer must be on, signed in as a user with access to the repo,
  and have network connectivity when Task Scheduler runs.
- GitHub Desktop authentication must work for git push in the scheduled account.
- Never store Gateway backups, passwords, private keys, or secrets in GitHub.
- Avoid OneDrive and Git editing the same files simultaneously; test with IT.
- Project source must be a complete, stable export set for deletion warnings.
- No force pushes, automatic pulls/merges, or destructive project deletions.
