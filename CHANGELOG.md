# Changelog

All notable changes to IDM Clone. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.5.0] - 2026-11-01

### Changed
- **Download list**: the file name column is no longer squeezed to a few characters; it takes
  the space that is left after narrower default widths for the other columns, and every cell
  stays on one line (long names elide instead of wrapping). The Progress cell now complements
  the Status column instead of repeating it: "55.6% · 3.1 GB of 5.5 GB" while downloading,
  "Paused · 42.5%", "Error · HTTP 403".
- **Columns are yours**: right-click the header to show or hide columns, drag them into another
  order, or reset the layout.
- **Layout is remembered**: window size and position, sidebar width, column widths, order and
  sort, and whether the details pane is open survive a restart (stored in `ui.ini` in the data
  folder, so portable installs carry it along).
- **Details pane**: long URLs and paths elide in the middle instead of forcing the window wider
  than the link; the full text is in the tooltip and "Copy link".
- **Settings**: numeric fields and the appearance picker are compact instead of stretching across
  the dialog; the clipboard sub-option is indented and greys out when the clipboard watcher is
  off; time fields and the saved-logins list match the other inputs.
- Header hover highlight, focus ring on combo boxes, and muted disabled inputs in both themes.
- The window has a sensible minimum size (860 × 540) and opens at 1280 × 780 on first run.

### Fixed
- Links in the app, installer and README pointed at a GitHub repository that does not exist;
  "Check for updates" and the in-app installer download now use the real one.
- `register-browser.ps1` and `build-exe.ps1` had their usage comments corrupted by a stray
  control character, and `build-installer.ps1` did not parse at all (escaped quotes in the
  version lookup), so no installer could be built from it. The Inno Setup script itself failed
  to compile under Inno Setup 6 (`LoadStringFromFile` needs an `AnsiString`).

## [0.4.0] - 2026-11-01

### Added
- **BitTorrent**: magnet links, `.torrent` files and links, DHT, peer exchange, UPnP, piece map,
  fast-resume, optional seeding, upload and connection limits, and an opt-in `magnet:` /
  `.torrent` handler so browser clicks open in the app.
- **MEGA**: file and folder links, fetched with the multi-connection engine and decrypted while
  downloading; expired temporary URLs are refreshed on resume.
- **Scheduler**: run queued downloads only inside a daily time window on chosen weekdays. Downloads
  still running when the window closes are paused and continue when it opens again.
- **HTTP authentication**: sites answering 401 get a login row in the Add dialog. Saved logins are
  encrypted with Windows DPAPI for the current account and reused automatically.
- **Per-download limits**: right-click a running download to change its connection count or give
  it its own speed limit without stopping it.
- **Status and Description columns**: the failure reason ("Error · HTTP 403 Forbidden", "Timed
  out", "Disk full") and the page title or site, both sortable.
- **Duplicate warning**: the Add dialog flags links already in the list or already downloaded and
  offers "Download anyway"; it also warns when a same-named file exists.
- **yt-dlp self-update**: the newest yt-dlp release is fetched into the app data folder at startup
  and used instead of the bundled copy, so site breakages are fixed without a new app build.
  Settings › Video shows the version in use and has "Update yt-dlp now".
- **Firefox**: the extension loads in Firefox as an event page, and the installer and
  `register-browser.ps1` register the native host for it.
- **Engine logging**: start/stop, every added task, completions, failures with the reason,
  segment retries, scheduler transitions and yt-dlp updates go to `%APPDATA%\idm-clone\logs`.
- Floating **Download** button over page videos in the browser.
- **In-app update**: the installed build can download a new release and run the installer itself.
- **Portable mode**: a `portable.txt` beside the exe keeps all data in a `data` folder next to it;
  `IDM_CLONE_DATA_DIR` overrides the data folder.
- **History limit**: the list keeps the most recent finished downloads (default 500, configurable).
- Accessible names on icon-only buttons and crisp rendering at fractional display scaling.
- **Checksum verification**: paste an MD5, SHA-1, SHA-256 or SHA-512 digest in the Add dialog and
  the file is hashed before it is renamed (a mismatch keeps the .part and marks the download
  failed); right-click a finished download for "Verify checksum…".

### Changed
- Cookies and Authorization headers are protected (DPAPI) before they are written to the download
  history.
- The speed limiter no longer bursts after idle time; it caps unused credit at a quarter second.
- `ftp://` links are no longer accepted by the link validator; the engine never supported them.
- Browser cookies are no longer forwarded when a link redirects to a different host.
- The login row warns when the link is plain `http://`, since the password would travel unencrypted.

### Fixed
- A download whose connection count was lowered mid-run could keep more workers than requested.

### Internal
- Test suite runs in parallel with pytest-xdist; release notes are taken from this file.
- mypy runs in CI; download history persistence moved to `engine/history.py`.

## [0.2.0] - 2026-10-27

### Added
- Initial public release: segmented multi-connection downloads, pause and resume across restarts,
  Chrome/Edge/Brave extension, yt-dlp video downloads with quality picker, categories, clipboard
  watcher, light and dark themes, tray icon, installer and portable build.

[Unreleased]: https://github.com/shahadatjuton/downstream/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/shahadatjuton/downstream/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/shahadatjuton/downstream/compare/v0.2.0...v0.4.0
[0.2.0]: https://github.com/shahadatjuton/downstream/releases/tag/v0.2.0
