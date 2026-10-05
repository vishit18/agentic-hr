
## Backups and restore
- Back up the database: `.\scripts\backup.ps1` (or `make backup`). Files go to `backups/` (git-ignored); the newest 14 are kept.
- Restore (replaces the current database): `.\scripts\restore.ps1 backups\hub-<date>.dump -Yes`
- Export everything as a zip (people, requests, logs, original files): sign in as an administrator and use Reports > Export all data.
- Uploaded CVs and request files live in `storage/inbox`; include that folder in your normal file backup.
