# Bunkr Downloader

A Windows Terminal utility for downloading Bunkr search results, resuming interrupted transfers, and organizing a local media collection. Developed by **Opsbeard** with assistance from ChatGPT.

## Quick start

From PowerShell in the folder containing the script, using Python 3.10 or newer:

```powershell
py -m pip install -r requirements.txt
py .\bunkr_download.py
```

Paste a `balbums.st` or `bunkr-albums.io` search results URL and enter a dedicated download folder. The script reads every search page, processes albums sequentially, and retries interrupted transfers. It resumes from SHA-256-verified checkpoints when the server confirms the file and byte range.

For on-demand Microsoft Defender scans after each album, run Windows Terminal as administrator. The script reports whether it can find the Defender command. Only download material you have permission to keep.

## Catalog and duplicates

Keep `bunkr_catalog.sqlite3` at the root of your collection. It records upload IDs, filenames, original hashes, and replacement hashes so the script can recognize files you've moved, renamed, or re-encoded within that folder tree.

The script keeps the oldest copy of each exact duplicate and moves the extras to `.bunkr-duplicates`. Review the folder before deleting its contents. Moving duplicates or keeping processed originals does not reclaim their disk space.

## Optional media compression

Check the tools without searching or downloading:

```powershell
py .\bunkr_download.py --check-tools
```

| Media | Tooling | What the script checks |
| --- | --- | --- |
| Video | FFmpeg and FFprobe with `libx265` and `libvmaf` | HEVC encode, playback validation, and visual quality checks. x265 runs on the CPU. |
| Audio | FFmpeg and FFprobe with FLAC support | Lossless conversion of supported uncompressed PCM files. |
| JPEG images | JPEG XL tools `cjxl` and `djxl` | The original JPEG can be reconstructed byte for byte. |

On Windows, choose **I** at the tool prompt to install FFmpeg Essentials through WinGet. For JPEG XL, get the Windows CLI tools from the [libjxl releases page](https://github.com/libjxl/libjxl/releases), extract them, and add the executable folder to PATH. **Learn more** asks before opening a browser.

After deduplication, the script samples eligible files and estimates space savings and runtime. You can stop after the report or convert all or selected media types. Each output must pass validation and save at least 10% before the script applies your choice to move, delete, or keep the original. Video encoding is lossy and the estimates vary with the source. Choose **Move to processed_raws** to retain originals for comparison. A text summary is optional and stays in the collection root.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE). Commercial reuse and sale are outside the granted permissions. See the license for the permitted uses and redistribution terms.
