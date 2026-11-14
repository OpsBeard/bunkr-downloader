# Changelog

## v1.0.0 — 2026-11-14

First public release.

- Downloads all pages of a Bunkr album search sequentially, with per-file progress, retry prompts, and resumable SHA-256 checkpoints.
- Recognizes multiple file-page identifier formats and supported Bunkr CDN domains.
- Runs optional Microsoft Defender scans by album and reports download, scan, and disk usage totals.
- Keeps the oldest exact duplicate and moves extras to a review folder. The SQLite catalog maps original uploads and aliases to retained or re-encoded files.
- Estimates savings and offers validated x265, FLAC, and JPEG XL conversion. Files below the 10% savings threshold are kept as-is.
- Provides an optional WinGet FFmpeg installer, standalone tool checks, and a local text summary.

### Before v1.0.0

Development milestones preceding the first public release:

1. Album search pagination, sequential downloads, filename and size checks, and retry handling.
2. Expanded CDN and media identifier support, plus per-album Defender scans.
3. Exact duplicate detection, persistent catalog, chunked resume, and media compression estimates.
4. Windows file hashing fixes, on-demand duplicate archive checks, and dependency diagnostics.
