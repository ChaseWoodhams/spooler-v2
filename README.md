# Spooler v2

Local web GUI for **Elegoo Centauri Carbon** FDM 3D printers (CC1 and CC2). Monitor and control multiple printers from a single browser tab — or install as a PWA on your phone. Experimental support for **Prusa (PrusaLink)** and **Klipper (Moonraker)** printers is also included.

![Status: printing, paused, idle, complete](https://img.shields.io/badge/CC1%20%26%20CC2-supported-brightgreen)
![Prusa](https://img.shields.io/badge/Prusa%20(PrusaLink)-experimental-orange)
![Klipper](https://img.shields.io/badge/Klipper%20(Moonraker)-experimental-orange)

> **Branches**
> - `main` — latest stable release. This is what the Docker image (`ghcr.io/tharje/spooler-v2:latest`) is built from.
> - `dev` — active development. New features and fixes land here first and are tested before being merged to `main`.

## Quick start

**Requires:** [Docker](https://docs.docker.com/get-docker/)

```bash
curl -fsSL https://raw.githubusercontent.com/tharje/spooler-v2/main/docker-compose.yml -o docker-compose.yml
docker compose up -d
```

Open **`http://127.0.0.1:8080`** in your browser. Spooler has no login and listens on this machine only by default; to reach it from a phone or another computer, see [Network access](#network-access).

> **Note:** `network_mode: host` is required on Linux, so the container shares the host's network and `BIND_HOST` applies to the host directly. On **macOS or Windows**, Docker's host networking does not work — use the [Python setup](#option-b--python-no-docker) instead.

---

## Features

- **Live status** – idle, printing, warming up, paused, leveling, complete, cancelled, error
- **Camera feed** – MJPEG stream from each printer (CC2), proxied through Spooler
- **Temperature monitoring** – nozzle, bed and chamber with live target values
- **Print progress** – layer count, percentage, elapsed and remaining time
- **Print speed control** – adjust speed while printing or paused (CC1 and CC2)
- **Print options** – preview thumbnail, build plate, time-lapse and bed levelling before starting a print
- **File browser** – list, start and delete files stored on the printer
- **Print queue** – upload G-code or queue files already on a printer, assign printers, reorder, and start jobs with one-job-per-printer protection
- **Canvas / AMS multi-material** – shows filament slots; link each slot to a Spoolman spool for per-tray deduction
- **Filament tracking** – live mm/g per print and cumulative history log; deducts from the correct spool on tray changes
- **Web Push notifications** – print complete, layer checkpoint, nozzle still hot, nozzle overheat, spool low/empty *(still in testing)*
- **Controls** – pause, resume, stop, light toggle
- **Auto-discovery** – finds printers on the local network automatically
- **Persistence** – printers, history and spool data saved between restarts
- **Local-only by default** – no login; listens on 127.0.0.1 unless you opt in to a wider address
- **Spoolman integration** – filament spool inventory with auto-deduction after each print
- **Filament catalogue import** – one-click import of all ELEGOO filaments from SpoolmanDB
- **PWA** – installable as a home screen app on Android and iOS
- **HTTPS** – self-signed cert on port 8443; works with Tailscale serve out of the box

---

## Installation

### Option A – Docker (recommended)

No git clone needed. Just download the compose file and start:

```bash
curl -fsSL https://raw.githubusercontent.com/tharje/spooler-v2/main/docker-compose.yml -o docker-compose.yml
docker compose up -d
```

Open **`http://<server-ip>:8080`**.

Both Spooler and Spoolman start automatically. Data is stored in Docker volumes (`spooler_data`, `spoolman_data`) and survives restarts and rebuilds.

**Useful commands:**

```bash
docker compose pull && docker compose up -d   # update to latest version
docker compose logs -f                        # live logs
docker compose down                           # stop (data preserved)
```

### Option B – Python (no Docker)

```bash
git clone https://github.com/tharje/spooler-v2.git
cd spooler-v2
./setup.sh
```

Spoolman needs to run separately — see [Spoolman docs](https://github.com/Donkie/Spoolman).

### Option C – Windows workstation (supervised)

With Spooler and Spoolman checked out side by side (`spooler-v2/` and `spoolman-service/`, each with its own `.venv`):

```powershell
.\start-local.ps1                  # start the supervisor and wait until both services are healthy
.\install-autostart.ps1 -StartNow  # start automatically at sign-in (no administrator rights needed)
.\stop-local.ps1                   # stop the supervisor, Spooler and Spoolman
.\uninstall-autostart.ps1          # remove autostart
```

`supervisor.py` launches Spoolman and Spooler, checks their health endpoints every 10 s, and restarts a service that exits or fails three checks in a row, backing off up to 5 minutes if it keeps crashing. Services that are already running are adopted rather than started twice. Logs are written to `logs/` in Spooler's data directory.

---

## Install as PWA

**Android** (Chrome): open the URL → tap ⋮ → **Add to Home Screen**  
**iOS** (Safari): open the URL → tap Share → **Add to Home Screen**

For a proper standalone app (no browser chrome), use HTTPS:

- `https://<server-ip>:8443` — self-signed cert (download and install it from that URL)
- [Tailscale serve](https://tailscale.com/kb/1312/serve) — valid cert automatically

---

## Network access

Spooler has **no authentication, by design**. It listens on `127.0.0.1` (this machine only) by default, so only people using the computer it runs on can open it.

To use it from a phone or another computer, set `BIND_HOST` in a `.env` file next to `docker-compose.yml` (or the server's environment):

```env
BIND_HOST=0.0.0.0        # all interfaces
# BIND_HOST=192.168.1.10 # or one LAN address
```

Then restart (`docker compose up -d`) and open `http://<server-ip>:8080`.

> **Warning:** anyone who can reach that address and port can control your printers, queue and files. Spooler provides no login and no other access control; the bind address is the only protection. Only expose it on a network you trust, and never forward the port to the internet. With Docker's `network_mode: host`, the setting applies directly to the host's network interfaces.

Printer access codes and MQTT passwords (see below) are credentials for the printers themselves and are unrelated to this.

---

## Adding printers

- Click **Discover** to auto-find printers on the network
- Click **Add Printer** to enter an IP address manually

For **CC2 (Centauri Carbon 2)**, you also need the MQTT password shown on the printer under **Settings → Network**.

For **Prusa printers (PrusaLink)**, select *Prusa – PrusaLink HTTP API* and enter the API key from your printer's PrusaLink settings. *(Still experimental — basic control and file browser work, some features may be limited.)*

For **Klipper printers (Moonraker)**, select *Klipper – Moonraker HTTP API* and enter the IP address of your Moonraker host (default port 7125). API key is optional — only needed if your Moonraker is configured with authentication. *(Still experimental — basic control and file browser work, some features may be limited.)*

Printer configs are saved and reconnect automatically on restart.

---

## Filament tracking

- Live filament usage (mm and grams) shown on each printer card while printing
- All completed and cancelled prints are logged in the **History** panel
- **Single-material printers** – assign a Spoolman spool to the printer; used grams are deducted automatically after each print
- **Multi-material (Canvas/AMS)** – link each filament slot to a Spoolman spool; filament is tracked per tray and deducted from the correct spool when the active tray changes

---

## Spoolman

Spooler integrates with **[Spoolman](https://github.com/Donkie/Spoolman)**, an open-source filament spool manager.

- Runs on **port 7912** alongside Spooler (started automatically by Docker Compose)
- Open **Spoolman UI** directly from the Spools panel
- Add spools manually
- **Import Elegoo** imports all 97 ELEGOO filament types from SpoolmanDB in one click
- Spool **location** in Spoolman is set automatically to the printer's name (e.g. `CC2`) whenever a spool is linked to a slot or the printer connects — no manual setup needed

By default, Spooler proxies the Spoolman UI through itself (so one port covers everything). To redirect the browser directly to Spoolman instead — useful if you run a separate reverse proxy or need better WebSocket support — set `PROXY_SPOOLMAN=false` in your `.env` file.

---

## Print queue

Open **Queue** in the side navigation to line up work across printers (CC1 printers for now).

- **Add jobs** – upload a `.gcode` file (button or drag and drop), or press **Queue** next to a file in a printer's file browser. Uploaded files are kept in `jobs/<id>/` in the data directory; slicer estimates for time and filament are read from the G-code.
- **Organise** – assign each job to a printer (or leave it unassigned), set build plate, leveling and time-lapse options, add notes, and reorder with the arrows.
- **Start** – Spooler checks the printer is online and idle, uploads the file to the printer if needed, sends the start command and follows the print. Jobs move through *queued → starting → printing → completed / cancelled / failed / outcome unknown*.
- **One job per printer** – a printer running a queued job accepts no second job, and manual starts from the file browser are refused until it finishes. If a printer goes away mid-job, **Mark failed** releases it.
- **Finished jobs** stay listed with their outcome and can be queued again. Every state change and printer status transition is recorded per job.

Jobs live in `spooler.db` (SQLite) in the data directory, which is included in the backups.

## Slicing models

Upload an STL, 3MF, OBJ or STEP model with **Queue → Slice model** (or drop it on the queue panel) and Spooler slices it with [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) in the background. Each sliced plate becomes a queued job.

- **Setup** – install OrcaSlicer (the portable build works) and set `ORCA_SLICER_PATH` in `.env`, or unpack it as `../tools/OrcaSlicer-<version>/` next to Spooler.
- **Locked profiles** – a profile is a printer + process + filament preset from OrcaSlicer's stock Elegoo presets, flattened into `profiles/<id>/` in the data directory. A locked profile never changes: its checksums are verified before every slice. Lock other combinations from the slice dialog; *0.20mm Standard · Elegoo PLA · 0.4 mm nozzle* is locked on first start.
- **Plate side** – *Textured (Side A)* slices for OrcaSlicer's Textured PEI Plate and *Smooth (Side B)* for High Temp Plate, and the job gets the matching build-plate option.
- **3MF projects** keep their own object layout; other models are auto-arranged on the plate.
- **Kept for every slice** in `slices/<id>/`: the uploaded model, the exact profile files used, the G-code and OrcaSlicer's log. The slicer version is stored with the slice and on the job's timeline.
- **Thumbnails** – Spooler draws the plate itself (OrcaSlicer's command line can't) and embeds it in the G-code at the sizes the printer profile asks for (144×144 for the Centauri Carbon), so printers that read thumbnails can show a picture of the job.

## Projects: from a model to the queue

Open **Projects** in the side navigation (or `/slicer.html`) and upload an STL, OBJ or 3MF. One page takes it through five steps, shown along the top; the step you were on is remembered per project.

1. **Model** – Spooler checks the mesh is a closed solid (up to 1 GB) and repairs what it safely can: it joins duplicate vertices, fixes flipped faces, closes open boundaries and merges overlapping shells. Inspect it in 3D, see its size, volume and roughly how many parts it needs, and set the scale for files modelled in metres, centimetres or inches, and which axis is up. A model that already fits the printer can go straight onto a plate.
2. **Split** – *Smart* cuts repeatedly split the biggest piece that doesn't fit, along the world axes or the piece's own orientation, and nudge each cut away from thin features so joints keep enough surface. *Grid* cuts slice the whole model into bed-sized blocks, optionally evenly spaced (the approach of [PrintSplitWeb](https://github.com/printsplit/PrintSplitWeb)). Drag a cut plane in the 3D preview, or type its position, and the parts are checked again; **Auto-fix** adds cuts wherever a part is still too big. The default limit of 240 × 240 × 250 mm keeps Centauri Carbon parts clear of the purge corner.
   Alignment holes are placed once every cut is known, so no cut can go through a hole, and each hole is checked to be shared by exactly two parts: 2 mm or 1 mm steel rods, 1.75 mm filament pins or your own sizes, with a smaller fallback size where walls are thin. Each part is turned to stand on a large flat face with low overhangs; the parts list shows print sizes, holes and which parts need supports or a brim, and the assembly map says which parts join, in what order, with how many rods.
3. **Plates** – **Lay out on plates** packs the parts onto as few plates as fit the printer profile's bed (clear of the purge area, turning parts a quarter turn where that helps). Parts that need supports get plates of their own with tree supports on. Look at each plate, move (M), turn (R) or scale (S, keeping proportions) objects, lay a clicked face on the bed (F) or **Auto-orient** (O) for the least support, **Arrange** (A) one plate with OrcaSlicer (Shift+A every plate), copy and paste (Ctrl+C / Ctrl+V), undo and redo (Ctrl+Z / Ctrl+Y), look from a side (0–6) or at the selection (Z), **Merge into…** another plate (only if everything fits) or **Repack plates** after moving things around. Objects off the bed or over the purge area turn red and block slicing. You can also add other models or another split model's parts.
4. **Slice** – settings stack in layers: the locked profile, the project's changes (apply to every plate), a plate's own changes (**Use on all plates** copies them to the others) and an object's own changes. Every page of OrcaSlicer's Process and Filament settings is there with its labels, units, tooltips, choices and limits; **Advanced** shows the advanced options, search finds a setting, and **Save changes as profile** locks the project's changes as a new profile. **Slice all** slices every plate that isn't up to date; a sliced plate shows its toolpaths layer by layer, coloured by feature, with filament and time per feature and its G-code to download. A plate that changes after slicing is marked out of date.
   **BrickLayers interlocking walls** (Slice step) post-processes the G-code with [BrickLayers](https://github.com/GeekDetour/BrickLayers): every other inner wall loop is raised by half a layer so walls interlock like bricks and are stronger across layer lines. Turn it on for the project, then keep that setting for each plate or turn it on or off for one plate. Set the extrusion multiplier (default 1.05) and the first layer to change (default 3). While it's on, the plate is sliced with arc fitting off and classic walls printed inner then outer (only in that plate's slice; locked profiles aren't changed). OrcaSlicer's own output is kept as `plate.orca.gcode`, and the preview, estimates and preflight use the post-processed G-code. A toggle or setting change marks the plate out of date.
5. **Print** – plates are grouped by what is happening to them (needs attention, printing, queued, ready, done). Tick the sliced plates in Ready and send them as one batch (levelling, time-lapse); a plate already waiting in the queue isn't sent twice unless you ask for another copy. **Assign printers** spreads the batch over the printers you tick; **Review & start** on a queued plate opens the job on the dashboard, where every start runs the preflight checks — start each plate when that printer's bed is clear. The global queue of jobs from every project stays on the dashboard.

**Print-farm hooks** – *Spool* (Printer and material card) lists the spools in Spoolman: choosing one switches to the closest stock filament preset for its material and applies its density, diameter, temperatures (for the chosen plate side) and cost as filament settings, and shows whether it has enough left for the sliced plates. Clearing it puts those settings back. *Print on* gives a plate its own printer and warns when that printer is offline, has another nozzle installed (Printer hardware), has another material loaded, or has too little filament left. **Slice & send** slices the plate (or every plate, when each has a printer) and queues it for that printer when it's done; the plate shows "in the queue for …", or why it couldn't be sent. **Assign printers** in the Queue step now balances the batch by estimated print time (longest plates first, each onto the printer with the least assigned). Several plates slice at once: `SLICE_WORKERS` (default 1–2 by CPU count) sets how many OrcaSlicer processes run together.

**OrcaSlicer projects (3MF)** – uploading a project saved by OrcaSlicer or BambuStudio (as a new project or with *Add model*) keeps its plates, every object's placement, rotation and scale, per-object setting changes, the plate side, and the project's differences from the chosen presets (a user's own Orca presets are applied as setting changes on top of the closest stock Elegoo preset). Modifier volumes, negative parts and support blockers are left out, mirrored objects are placed unmirrored, and the page lists what it changed. **Export 3MF** (top bar) writes the project back out the same way; OrcaSlicer slices that file to the same layers and time as Spooler does. A plain 3MF without plates or settings is still added as one model.

The queue on the dashboard groups a batch under one header with the same **Assign printers** control and a link back to its project.

The settings pages come from OrcaSlicer's source: `python tools/orca_settings.py --tag v2.4.2` regenerates `public/orca/settings.json` from `PrintConfig.cpp` and `Tab.cpp`. Values are validated against those definitions on the server, and scripts and G-code settings can't be changed from the browser. Project files live in `projects/<id>/` and the uploaded model's work files in `splits/<id>/` in the data directory; deleting a project deletes both. Each split step runs `split_engine.py` in its own process using [Manifold](https://github.com/elalish/manifold) and [trimesh](https://github.com/mikedh/trimesh), so a large model never stalls the server. Install the libraries with `pip install -r requirements.txt`.

## Preview and preflight

Starting a queued job opens a review screen: the toolpath preview beside the preflight checks. Nothing leaves the queue for a printer until the checks allow it.

- **Toolpath preview** – a 3D view of everything up to the chosen layer, or a top-down view of a single layer over the one below it, coloured by feature (walls, infill, supports…), with the bed grid and the purge zone marked.
- **Blocking checks** – printer offline or busy; file sliced for a different printer; print past the 256 × 256 mm bed or taller than 256 mm; anything printed in the front-right nozzle purge zone; nozzle or bed temperature above the Centauri Carbon's 320 °C / 110 °C limits; a nozzle size other than the one installed; commands that change printer settings, disable safety limits or turn the motors off mid-print; a G-code file that changed after it was queued.
- **Warnings you acknowledge to start** – the build plate side differs from what the file was sliced for; nozzle temperature outside the filament's range; the Spoolman spool on the printer is a different material or doesn't have enough filament left; moves beyond the normal travel range; homing mid-print or extreme speed/flow overrides; files stored on the printer, whose contents can't be inspected.
- **Information** – unrecognised commands, no preview image in the file, no spool assigned in Spoolman, nozzle size not set yet (set it per printer from the review screen).
- The server enforces the same rules: `POST /api/jobs/<id>/start` is refused while a check fails, warnings need `"acknowledge_warnings": true`, and the outcome is recorded on the job's timeline.

The analysis is cached next to each job's G-code (`jobs/<id>/analysis.json` and `toolpath.bin`) and redone if the file changes.

## Print history

**History** in the side navigation lists every print on every printer — from the queue, the file browser or the printer's own screen.

- **Recorded per print** – G-code file, printer, start and end time, duration, layers reached, outcome (*completed*, *cancelled*, *failed*, *outcome unknown*, or *printing* while it runs), filament used, the spool(s) it came from with material and colour, estimated cost, the queue job it belongs to, and your notes.
- **Cost** – grams used × the spool's price per kg in Spoolman (spool price over its initial weight, falling back to the filament's price). Shown in Spoolman's currency; left blank when no price is set.
- **Estimates vs. actual** – for queued jobs, the slicer's time and filament estimates sit next to what the print really took.
- **Search and filter** – free-text search across file, printer, material, vendor and notes; filter by printer, outcome and time range; totals for success rate, filament, cost and print hours; **Export CSV** for the current filter.
- **Troubleshooting** – expand a print to see its details, spool breakdown and the queue job's full timeline of printer status changes.
- **Print again** – copies the original queue job (same file, printer, options and notes) or queues the file stored on the printer.

Records live in `spooler.db`. Prints logged by earlier versions in `history.json` are imported automatically on first start; the old file is kept as `history.json.imported`.

## Health, recovery and backups

- **System health** – the pill in the header summarises Spoolman, printer links, backups and the supervisor; click it for details. A banner appears while the browser is reconnecting to Spooler.
- **Printer reconnects** – a dropped link is retried after 2 s, repeated failures back off to 60 s, and a link that stays silent for 65 s is closed and reopened.
- **Spoolman outages** – filament deductions that can't reach Spoolman are queued in `spoolman_outbox.json` and replayed automatically once it answers again.
- **Backups** – every 24 h, and on demand with *Back up now*, Spooler writes `backups/spooler-backup-<timestamp>.zip` containing the JSON configuration, `.env` files and integrity-checked SQLite snapshots, including Spoolman's database when `SPOOLMAN_DB_FILE` is set. The newest 14 are kept. See [`.env.example`](.env.example) for the settings.

### Restoring a backup

1. Stop everything (`.\stop-local.ps1`).
2. Unzip the archive. Copy `data/*` into Spooler's data directory and `app/.env` next to `server.py`.
3. Move Spoolman's current `data/spoolman.db` aside, then copy `sqlite/spoolman/spoolman.db` in its place.
4. Start again (`.\start-local.ps1`).

Each archive's `manifest.json` lists every file with its SHA-256, plus any database that could not be backed up.

## Protocol

| Printer | Transport | Notes |
|---------|-----------|-------|
| CC1 (Centauri Carbon 1) | SDCP v3.0 over WebSocket (port 3030) | |
| CC2 (Centauri Carbon 2) | MQTT – printer hosts its own broker (port 1883) | Requires MQTT password from printer settings |
| Prusa (PrusaLink) | HTTP REST polling (`/api/v1`) | Requires API key from PrusaLink settings; experimental |
| Klipper (Moonraker) | HTTP REST polling (`/printer/objects/query`, port 7125) | API key optional; experimental |

See [CC2_INTEGRATION.md](CC2_INTEGRATION.md) for full CC2 protocol notes.

## Ports

| Port | Purpose |
|------|---------|
| 8080 | HTTP – web UI |
| 8443 | HTTPS – web UI (self-signed cert, needed for PWA) |
| 8765 | WebSocket – browser ↔ backend (plain, HTTP) |
| 8766 | WebSocket – browser ↔ backend (WSS, HTTPS/PWA) |
| 7912 | Spoolman – filament manager UI and API |
| 3030 | WebSocket – backend ↔ printer (CC1/SDCP) |
| 3000 | UDP – printer discovery broadcast |
| 1883 | MQTT – CC2 printer broker (on the printer, not the server) |

## Stack

- **Backend** – Python 3.12, `asyncio`, `websockets`, `aiomqtt`, `bcrypt`
- **Frontend** – Vanilla HTML/CSS/JS, no build step; [three.js](https://github.com/mrdoob/three.js) r169 vendored in `public/vendor/three` for the split workspace
- **Model splitting** – `manifold3d`, `trimesh`, `numpy`, `scipy`, `networkx`
- **Spool manager** – [Spoolman](https://github.com/Donkie/Spoolman) (official Docker image)
- **Filament database** – [SpoolmanDB](https://github.com/Donkie/SpoolmanDB)

## Contributors

- [snazy2000](https://github.com/snazy2000) — modular backend refactor (auth, discovery, spoolman, printers modules), AMS/Canvas multi-material hub support

---

## Acknowledgements

CC2 support would not have been possible without these projects:

- [CentauriCarbon2](https://github.com/elegooofficial/CentauriCarbon2) by Elegoo (official) — firmware source; full MQTT method table (`method.h`), print state strings (`print_stats.cpp`), Canvas/AMS RFID filament struct (`canvas_dev.h`), sub_status codes, `gcode_move` speed/extrude factor fields
- [centauri-sentinel](https://github.com/LegalMarc/centauri-sentinel) by LegalMarc — MQTT client details, topic structure, partial-status deep-merge, MJPEG grabber
- [elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant) by danielcherubini — CC2 MQTT transport, access-code config, sub_status constants and method 1046 file metadata
- [elegoo-link](https://github.com/ELEGOO-3D/elegoo-link) by ELEGOO-3D — CC2 MQTT method reference (method 1045 thumbnail, method 1046 file detail)
- [sdcp-centauri-carbon](https://github.com/WalkerFrederick/sdcp-centauri-carbon) by WalkerFrederick — SDCP v3.0 protocol documentation (CC1)
- centauri-carbon-dashboard (open source) — CC1 print speed control reference (SDCP Cmd 403 / PrintSpeedPct)
- [Spoolman](https://github.com/Donkie/Spoolman) by Donkie — open-source filament spool manager
- [SpoolmanDB](https://github.com/Donkie/SpoolmanDB) by Donkie — filament database for catalogue import

Model splitting builds on:

- [PrintSplitWeb](https://github.com/printsplit/PrintSplitWeb) by printsplit — the balanced grid cuts and draggable cut planes that inspired the Split workspace
- [Manifold](https://github.com/elalish/manifold) and [trimesh](https://github.com/mikedh/trimesh) — robust mesh booleans, plane splits and model file I/O
- [three.js](https://github.com/mrdoob/three.js) — the 3D cut editor (MIT, vendored)

Slicing builds on:

- [BrickLayers](https://github.com/GeekDetour/BrickLayers) by Geek Detour — interlocking-wall G-code post-processing (GPL-3.0, vendored unmodified in `vendor/bricklayers`)
