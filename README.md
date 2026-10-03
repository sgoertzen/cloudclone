# CloudClone

Unraid-friendly container that mirrors Google Drive to a local folder on a schedule. Uses `rclone sync` (incremental: only new/changed files are downloaded) behind a small web UI.

- Multiple Google accounts, each with its own schedule (daily / weekly / monthly at a chosen time) and sub-folder.
- Read-only Drive access (`drive.readonly`); the container can never change your Drive.
- Google Docs/Sheets/Slides are exported as `.docx` / `.xlsx` / `.pptx`.
- Exact mirror: files deleted in Drive are deleted from the backup on the next run.
- Optional folder selector (account → Settings → *Choose folders…*): back up only chosen folders. With nothing selected, the whole Drive is backed up. Files in the Drive root are skipped when folders are selected; deselected folders already in `Data/` are no longer updated and are not pruned.
- Live progress bar, cancel button, and "Run now".

## Build & run
```
docker build -t cloudclone .
docker run -d --name cloudclone -p 8080:8080 \
  -v /mnt/user/backup/cloudclone:/backup \
  -e TZ=America/Denver cloudclone
```
On Unraid, push the image to a registry, set `<Repository>` in `cloudclone.xml`, and copy the XML to `/boot/config/plugins/dockerMan/templates-user/` (or paste it via *Add Container → Template*).

The single `/backup` mount holds `Data/` (the mirrored files, one sub-folder per account) and `Config/` (settings and Google tokens), both created automatically.

## Connecting Google
Create your own OAuth client once (the UI can save it as the default for all accounts):
1. Google Cloud Console → new project → enable **Google Drive API**.
2. OAuth consent screen (External); **publish** the app (otherwise refresh tokens expire after 7 days).
3. Credentials → OAuth client ID, then paste the Client ID/Secret into the UI.

**One-click sign-in (recommended):** set `PUBLIC_URL` (e.g. `https://gdrive.example.com`), a name your reverse proxy serves over HTTPS for this container. It can resolve to your LAN IP; it doesn't need to be exposed to the internet. Create the client as **Web application** with Authorized redirect URI `https://gdrive.example.com/oauth/callback`. *Connect Google* then returns straight to the app. The browser must be able to reach that URL.

**Without `PUBLIC_URL`:** Google blocks redirects to LAN IPs, so create a **Desktop app** client instead. After approving, copy the URL of the failed `127.0.0.1:53682` page into the UI.

## Notes
- Because it is an exact mirror, treat `Data/` as a copy, not an archive. If you want protection against accidental deletion in Drive, snapshot/back up `Data/` separately.
- Exported Google Docs have no stable size, so rclone detects changes by modified time for those.
- One sync runs at a time; others queue.
