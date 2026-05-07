![banner](https://raw.githubusercontent.com/mrusse/soularr/refs/heads/main/resources/banner.png)

<h1 align="center">Soularr</h1>
<p align="center">
  A Python script that connects Lidarr with Soulseek!
</p>

<p align="center">
  <a href="https://discord.gg/EznhgYBayN">
    <img src="https://img.shields.io/discord/1292895470301220894?label=Discord&logo=discord&style=for-the-badge&cacheSeconds=60" alt="Join our Discord">
  </a>
</p>

# About

Soularr reads all of your "wanted" albums/artists from Lidarr and downloads them using Slskd. It uses the libraries: [pyarr](https://github.com/totaldebug/pyarr) and [slskd-api](https://github.com/bigoulours/slskd-python-api) to make this happen. View the demo below!

![Soularr_small](https://github.com/user-attachments/assets/15c47a82-ddf2-40e3-b143-2ad7f570730f)

As downloads complete in Slskd the script will automatically tell Lidarr to import the files, making it a truly hands off process.

# What this fork adds

This fork keeps the upstream behaviour and layers a stateful in-flight tracker, an orphan recovery flow, and a slskd-side post-download organizer on top. The goal is to stop Soularr from re-grabbing albums it already has, recover from edge cases (manual slskd grabs, partial downloads, restarts) without losing files, and feed Lidarr a clean folder layout so its `ManualImport` accepts uploads cleanly.

| Feature | Module | Effect |
|---|---|---|
| Persistent state store | `state.py` (TinyDB) | Single JSON DB at `<data>/soularr.db.json`. Tables for in-flight grabs, failed imports, orphans, runtime singletons. fcntl-locked so `soularr.py` and the web UI can both write safely. |
| In-flight dedup | `filter_list` in `soularr.py` | Skips wanted albums whose transfers are still in slskd or whose files are already on disk awaiting Lidarr import. No more duplicate grabs across cycles. |
| slskd adoption | `adopt.py` | At cycle start: refreshes existing trackers from slskd, and adopts any active slskd download whose path matches a wanted album (e.g. user grabbed it manually in the slskd UI). |
| Orphan recovery | `orphans.py` + new **Orphans** UI tab | Walks `/downloads`, groups audio by `(artist, album, format)` ID3 tags, fuzzy-matches Lidarr, and auto-imports via `ManualImport` when the album is wanted. Otherwise records `pending` with full per-file rejection details for the user. |
| Post-download organizer | `slskd_hook.py` | Receives slskd's `DownloadFileComplete` webhook and moves each file to `{Artist}/{Album}/{FormatBucket}/{TrackNo:02d} - {Title}.{ext}`. Reuses existing folders case-insensitively so two peers with subtly different tag casing merge instead of forking parallel trees. |
| Web UI: Orphans tab | `webui/` | Per-row Artist / Album / Format / Audio files / Rejections / Actions, status badge, immediate Re-scan, modal showing Lidarr's per-file `ManualImport` preview. |

The cycle runs in this order each pass: **wanted-list fetch → adopt → orphan scan → grab → monitor**. Adopt has to run before orphan scan so an album whose transfers just settled is auto-imported instead of re-evaluated against stale state.

New deps: `tinydb>=4.8.0`. No other runtime additions.

# Setup

## Install and configure Lidarr and Slskd

**Lidarr**
[https://lidarr.audio/](https://lidarr.audio/)

Make sure Lidarr can see your Slskd download directory, if you are running Lidarr in a Docker container you may need to mount the directory. You will then need add it to your config (see "download_dir" under "Lidarr" in the example config).

**Slskd**
[https://github.com/slskd/slskd](https://github.com/slskd/slskd)

The script requires an api key from Slskd. Take a look at their [docs](https://github.com/slskd/slskd/blob/master/docs/config.md#authentication) on how to set it up (all you have to do is add it to the yml file under `web, authentication, api_keys, my_api_key`).

> **slskd version:** the post-download organizer in this fork uses slskd's webhook integration with the `DownloadFileComplete` event. Tested against slskd `0.25.x`; should work on any version that supports `integrations.webhooks` with the `download.file.complete` / `DownloadFileComplete` event (slskd ≥ `0.21.x`). The orphan / adoption flows work without the webhook — slskd's HTTP API alone is enough — but the canonical `{Artist}/{Album}/{Format}/` layout that makes Lidarr's matching cleanest only happens when the webhook is wired up.

### Configure slskd's post-download webhook

Add a webhook integration to your `slskd.yml` so each completed download is reorganized by `slskd_hook` into the canonical layout Lidarr expects:

```yml
integrations:
  webhooks:
    soularr_organize:
      on:
        - DownloadFileComplete
      call:
        url: http://soularr:8265/api/slskd-hook
        # If you password-protect the soularr web UI, add credentials here.
        # The endpoint itself does not require auth.
        retry:
          attempts: 3
          delay: 5
```

A few things to verify when this is set up:

- slskd and soularr must share the destination volume (`/downloads` in the example compose) so `slskd_hook` can move files into place.
- The webhook URL must be reachable from inside the slskd container (use the compose service name, not `localhost`).
- The hook is a no-op for non-audio files and for files that fail to read tags — those stay where slskd left them and are picked up by the orphan scan as-is.

### Configure Lidarr quality profile

The `ManualImport` Lidarr command honours the artist's quality profile, so make sure the formats you actually download are allowed there:

- **Settings → Profiles → Quality** → pick the profile you assign to artists soularr should grab. Enable every quality you want imported (e.g. `FLAC`, `FLAC 16/44.1`, `FLAC 24bit`, `MP3-320`, ...). A track in a disabled quality is rejected by `ManualImport` even when the tags match perfectly.
- **Settings → Media Management → Importing** → keep `Use Hardlinks instead of Copy` set per your filesystem, but the import works either way.
- Lidarr needs read+write access to the same `/downloads` path soularr uses (otherwise `ManualImport` can preview but not actually move files into the library).
- Optionally restrict soularr to a specific quality profile via `[Search Settings] → quality_profile_filter = <profile_id>` so only artists on that profile are processed.

## Docker

The best way to run the script is through Docker. A Docker image is available through [ghcr.io](https://github.com/mrusse/soularr/pkgs/container/soularr) and [dockerhub](https://hub.docker.com/r/mrusse08/soularr).

Assuming, your user and group is `1000:1000` and that you have a directory structure similar to the following:

```bash
/
├── Media
│   ├── Downloads
│   ├── Music
│   └── slskd_downloads
└── Containers
    ├── lidarr
    ├── slskd
    └── soularr
```

Where `Downloads` could be any music download directory, `slskd_downloads` is your slskd download directory, and finally `Music` is the location for you music files then an example docker run command might be:

```shell
docker run -d \
  --name soularr \
  --restart unless-stopped \
  --hostname soularr \
  -e TZ=ETC/UTC \
  -e SCRIPT_INTERVAL=300 \
  -e WEBUI_ENABLED=true \
  -p 8265:8265 \
  -v /Media/slskd_downloads:/downloads \
  -v /Containers/soularr:/data \
  --user 1000:1000 \
  mrusse08/soularr:latest
```

Or you can also set it up with the provided [Docker Compose](https://github.com/mrusse/soularr/blob/main/docker-compose.yml).

```yml
services:
  soularr:
    image: mrusse08/soularr:latest
    container_name: soularr
    hostname: soularr
    user: 1000:1000 # this should be set to your UID and GID, which can be determined via `id -u` and `id -g`, respectively
    environment:
      - TZ=Etc/UTC
      - SCRIPT_INTERVAL=300 # Script interval in seconds
      - WEBUI_ENABLED=true
    ports:
      - "8265:8265"
    volumes:
      # "You can set /downloads to whatever you want but will then need to change the Slskd download dir in your config file"
      - /Media/slskd_downloads:/downloads
      # Select where you are storing your config file.
      # Leave "/data" since thats where the script expects the config file to be
      - /Containers/soularr:/data
    restart: unless-stopped
```

Note: You **must** edit both volumes in the docker compose above.

- `/path/to/slskd/downloads:/downloads`

  - This is where you put your Slskd downloads path.

  - You can point it to whatever dir you want but make sure to put the same dir in your config file under `[Slskd] -> download_dir`.

  - For example you could leave it as `/downloads` then in your config your entry would be `download_dir = /downloads`.

- `/path/to/config/dir:/data`

  - This is where put the path you are storing your config file. It must point to `/data`.

You can also edit `SCRIPT_INTERVAL` to choose how often (in seconds) you want the script to run (default is every 300 seconds). Another thing to note is that by default the user is set to appropriate user on your system. If you wish to edit this change `user: 1000:1000` in the Docker compose to whatever you prefer. You can determine the user via the command `id -u` and the group vi `id -g`.

It is important that `lidarr` and `slskd` agree on the user/group. If they do not agree then it is unlikely you will have successful imports. Also, it is important to note that lidarr will need access to the downloads directory of slskd.

For a more complete example see the compose file bellow which contains `lidarr`, `slskd`, and `soularr`:

```yml
services:
  lidarr:
    image: ghcr.io/hotio/lidarr:latest
    container_name: lidarr
    hostname: lidarr
    environment:
      - TZ=ETC/UTC
      - PUID=1000
      - PGID=1000
    volumes:
      - /Containers/lidarr:/config
      - /Media:/data
    ports:
      - "8686:8686"
    restart: unless-stopped

  slskd:
    image: slskd/slskd
    container_name: slskd
    hostname: slskd
    user: 1000:1000
    environment:
      - TZ=ETC/UTC
      - SLSKD_REMOTE_CONFIGURATION=true
    ports:
      - "5030:5030"
      - "5031:5031"
      - "50300:50300"
    volumes:
      - /Containers/slskd:/app
      - /Media:/data
    restart: unless-stopped

  soularr:
    image: mrusse08/soularr:latest
    container_name: soularr
    hostname: soularr
    user: 1000:1000
    environment:
      - TZ=ETC/UTC
      - SCRIPT_INTERVAL=300
      - WEBUI_ENABLED=true
    ports:
      - "8265:8265"
    volumes:
      - /Media/slskd_downloads:/downloads
      - /Container/soularr:/data
    restart: unless-stopped
```

## Configure your config file

The config file has a bunch of different settings that affect how the script runs. Any lists in the config such as "accepted_countries" need to be comma separated with no spaces (e.g. `","` not `" , "` or `" ,"`).

Given the directory structure above you can use the following configuration

**Example config:**

```ini
[Lidarr]
# Get from Lidarr: Settings > General > Security
api_key = yourlidarrapikeygoeshere
# URL Lidarr uses (e.g., what you use in your browser)
host_url = http://lidarr:8686
# Path to slskd downloads inside the Lidarr container
download_dir = /data/slskd_downloads
# If true, Lidarr won't auto-import from Slskd
disable_sync = False

[Slskd]
# Create manually (see docs)
api_key = yourslskdapikeygoeshere
# URL Slskd uses
host_url = http://slskd:5030
url_base = /
# Download path inside Slskd container
download_dir = /downloads
# Delete search after Soularr runs
delete_searches = False
# Max seconds to wait for downloads (prevents infinite hangs)
stalled_timeout = 3600

[Release Settings]
# Use the release manually selected in Lidarr, ignoring the other release settings below
use_selected_lidarr_release = False
# Pick release with most common track count
use_most_common_tracknum = True
allow_multi_disc = True
# Accepted release countries
accepted_countries = Europe,Japan,United Kingdom,United States,[Worldwide],Australia,Canada
# Don't check the region of the release
skip_region_check = False 
# Accepted formats
accepted_formats = CD,Digital Media,Vinyl

[Search Settings]
search_timeout = 5000
maximum_peer_queue = 50
# Minimum upload speed (bits/sec)
minimum_peer_upload_speed = 0
# Minimum match ratio between Lidarr track and Soulseek filename
minimum_filename_match_ratio = 0.8
# Minimum time (seconds) between searches. Set to 0 to disable.
minimum_search_interval = 5
# Preferred file types and qualities (most to least preferred)
# Use "flac" or "mp3" to ignore quality details
allowed_filetypes = flac 24/192,flac 16/44.1,flac,mp3 320,mp3
ignored_users = User1,User2,Fred,Bob
# Prepend artist name when searching for albums
album_prepend_artist = False
# Search modes: all, incrementing_page, first_page
# "all": search for every wanted record, "first_page": repeatedly searches the first page, "incrementing_page": starts with the first page and increments on each run.
search_type = incrementing_page
# Albums to process per run
number_of_albums_to_grab = 10
# Blacklist words in album or track titles (case-insensitive)
title_blacklist = Word1,word2
# Blacklist words in search query (case-insensitive)
search_blacklist = WordToStripFromSearch1,WordToStripFromSearch2
# Lidarr search source: "missing" or "cutoff_unmet"
search_source = missing
# Skip re-downloading albums that previously failed to import into Lidarr
failed_import_denylist = True
# Only process artists with this Lidarr quality profile id (0 = disabled).
# Lookup the id at Lidarr -> Settings -> Profiles -> Quality -> hover the profile.
quality_profile_filter = 0

[Orphan Settings]
# Walks /downloads at cycle start, groups audio by ID3 tags, and auto-imports
# folders whose matched album is currently in Lidarr's wanted list. Folders
# that don't match a wanted album are surfaced in the Orphans web UI tab for
# manual review.
enabled = True
# Fuzzy-match thresholds for resolving the Lidarr album from ID3 tags.
artist_name_match_ratio = 0.85
album_name_match_ratio = 0.85
# How long (seconds) to wait for Lidarr's ManualImport command to complete
# before giving up.
lidarr_command_timeout = 60

[Adopt Settings]
# Reconciles state.albums with slskd's reality at cycle start: refreshes
# transfer states, drops trackers whose transfers are gone, and adopts any
# active slskd download whose path matches a wanted album. Closes the gap
# where the user manually grabbed an album in the slskd UI.
enabled = True
# Minimum fuzzy score (0..1) when matching slskd folder paths to Lidarr
# album metadata. Lower = more permissive.
fuzzy_threshold = 0.7

[Download Settings]
download_filtering = True
use_extension_whitelist = False
extensions_whitelist = lrc,nfo,txt

[Logging]
# Passed to Python's logging.basicConfig()
# See: https://docs.python.org/3/library/logging.html
level = INFO
format = [%(levelname)s|%(module)s|L%(lineno)d] %(asctime)s: %(message)s
datefmt = %Y-%m-%dT%H:%M:%S%z
# Enable logging to a file in addition to stdout
log_to_file = True
# Log filename (resolved relative to the data directory)
log_file = soularr.log
# Maximum log file size in bytes before rotation (default: 1MB)
max_bytes = 1048576
# Number of rotated log files to keep
backup_count = 3
```

[Full list of countries from Musicbrainz.](https://musicbrainz.org/doc/Release/Country)

[Full list of formats (also from Musicbrainz but for some reason they don't have a nice list)](https://pastebin.com/raw/pzGVUgaE)

An [example config](https://github.com/mrusse/soularr/blob/main/config.ini) is included in the repo.

## Web UI

Soularr includes a built-in web interface accessible at `http://your-host:8265` with:
- **Log viewer** — streams logs in real time. The `Clear` button backs the live log up to a timestamped `.bak` and truncates it on disk.
- **Config editor** — view and edit your `config.ini` in the browser.
- **Failed Imports** — view and clear albums that previously failed to import into Lidarr.
- **Orphans** — every folder under `/downloads` that's not currently being grabbed but isn't in Lidarr's library either, grouped one row per release `(artist, album, format)`:
  - Inline status badge (`pending`, `no_match`, `error`, `partial`, `ignored`).
  - Per-row actions: **Import** (run ManualImport, skip soft rejections), **Force** (override soft rejections like *Has missing tracks*, *Album match too low*), **Re-scan** (re-evaluate now without waiting for the next cycle), **Ignore**, **Delete** (rmtree the folder + cancel any matching slskd transfers).
  - Click the row to open Lidarr's full per-file `ManualImport` preview (per-track quality, matched album, exact rejection reason).
  - "In Lidarr: X/Y QUALITY" annotation when the album already has files in the library.

The web UI also exposes the slskd webhook endpoint at `POST /api/slskd-hook` (see *Configure slskd's post-download webhook* above).

The web UI is enabled by default in Docker. Make sure port `8265` is exposed in your compose file or `docker run` command (see the examples above).

To disable it, set the environment variable:

```yml
- WEBUI_ENABLED=false
```

If you are running the container on a shared pod, and wish to change the port on which the web UI listens to, set the environment variable:

```yml
- WEBUI_PORT=18265
```

Thanks to [EricH9958](https://github.com/EricH9958/Soularr-Dashboard) for making the original dashboard for Soularr.

## Running Manually

Install the requirements:

```bash
python -m pip install -r requirements.txt
```

You can simply run the script with:

```bash
python soularr.py
```

Note: the `config.ini` file needs to be in the same directory as `soularr.py`.

To also start the web UI, run in a separate terminal:

```bash
python webui/webui.py
```

Then open `http://localhost:8265` in your browser. If your `config.ini` is not in the repo root, pass `--var-dir` to point to its directory:

```bash
python webui/webui.py --var-dir /path/to/your/config
```

### Scheduling the script

Even if you are not using Docker you can still schedule the script. I have included an example bash script below that can be scheduled using a [cron job](https://crontab.guru/every-5-minutes).

```bash
#!/bin/bash
cd /path/to/soularr/python/script

dt=$(date '+%d/%m/%Y %H:%M:%S');
echo "Starting Soularr! $dt"

if ps aux | grep "[s]oularr.py" > /dev/null; then
    echo "Soularr is already running. Exiting..."
else
    python soularr.py
fi
```

**Example cron job setup:**

Edit crontab file with

```bash
crontab -e
```

Then enter in your schedule followed by the command. For example:

```cron
*/5 * * * * /path/to/run.sh
```

This would run the bash script every 5 minutes.

All of this is focused on Linux but the Python script runs fine on Windows as well. You can use things like the [Windows Task Scheduler](https://en.wikipedia.org/wiki/Windows_Task_Scheduler) to perform similar scheduling operations.

## Logging

There are some very basic options for logging found under the `[Logging]` section of the `config.ini` file. The defaults
should be sensible for a typical logging scenario, but are still somewhat opinionated. Some users may not like how the
log messages are formatted and would prefer a much simpler output than what is provided by default.

For example, if you want the logs to only show the message and none of the other detailed information, edit the
`[Logging]` section's `format` property to look like this:

```ini
[Logging]
format = %(message)s
```

For more information on the options available for logging, including more options for changing how the messages are
formatted, see the comments in the `[Logging]` section from the [example config.ini](#configure-your-config-file).

### Log to a File

Soularr can write logs to a rotating file in addition to stdout. Enable it in your `config.ini`:

```ini
[Logging]
log_to_file = True
log_file = soularr.log
max_bytes = 1048576
backup_count = 3
```

The log file is written to the data directory (the same directory as `config.ini` when running locally, or `/data/` in Docker). When the file reaches `max_bytes` it is rotated, keeping up to `backup_count` old files (`soularr.log`, `soularr.log.1`, `soularr.log.2`, etc.).

### Advanced Logging Usage

For more information on the options available for logging, including more options for changing how messages are
formatted, see the [Python logging documentation](https://docs.python.org/3/library/logging.html).

**If you would like more advanced logging configuration options to be implemented** (such as configuring filters,
formatters, handlers, additional streams, and multi-logger setups), consider submitting a feature request in
[the official discord](https://discord.gg/EznhgYBayN) or [submitting an Issue in the GitHub repository itself](https://github.com/mrusse/soularr/issues).

##

<p align="center">
  <a href='https://ko-fi.com/mrusse' target='_blank'><img height='35' style='border:0px;height:46px;' src='https://az743702.vo.msecnd.net/cdn/kofi3.png?v=0' border='0' alt='Buy Me a Coffee at ko-fi.com' /></a>
</p>
