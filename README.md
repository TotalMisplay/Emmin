# Emmin — Eternal Mod Manager of Nefia

A mod manager for **Elin**, in the spirit of RimPy and RimSort. It edits the game's own `loadorder.txt`, lets you arrange mods by dragging instead of clicking arrows, edits mod settings without a text editor, and can browse and download from the Steam Workshop.

Also on Nexus Mods: <https://www.nexusmods.com/elin/mods/129>

## What it does

**Load order**
- Two panes, like RimPy: mods that are off on the left (A–Z), the load order on the right. Drag between and within them.
- Move a mod anywhere without dragging: select it, then Alt+click where it should go, or type a slot number.
- Select several mods and they move as one block.
- Flags mods that are new, updated, missing from disk, or sharing an id with another mod.

**Sort rules**
- Per-mod rules: Load first, Load last, Load after, Load before.
- Groups, each with its own priority.
- One Sort button applies everything, reports rules that contradict each other, and can be undone with Ctrl+Z.
- Rules can be exported and imported to share them.

**Config editor**
- Edits the settings files mods keep in `Elin\BepInEx\config`.
- Each setting gets a proper control (switch, slider, dropdown, colour picker) with its description, default and allowed range.
- Only the values you change are rewritten; every comment and line in the file is left as it was.

**Steam Workshop**
- A built-in Workshop browser that uses Steam's official Web API, so it keeps working when Valve redesigns their site. Search, sort, tag filters, full mod pages with screenshots, and a list of each mod's required mods.
- A download list. Add from the browser, from pasted links or IDs, or from whole collections.
- Downloads go through SteamCMD straight into `Elin\Package`, where the Steam client can't update or delete them.
- **Keep a local copy** copies a mod you already have into `Elin\Package`. It works even after the mod has been removed from the Workshop.
- **Check for updates** flags newer versions, and mods that have been pulled from the Workshop.

**Lists**
- Save named mod lists, and export or import them as files.

## Requirements

- Windows.
- **Python 3.8 or newer.** Standard library only: there's nothing to `pip install`.
- Any modern web browser.
- Optional, for downloading: **SteamCMD** (Emmin can install it for you) and a Steam account that owns Elin. Elin's Workshop doesn't allow anonymous downloads.
- Optional, for browsing: a free **Steam Web API key** from <https://steamcommunity.com/dev/apikey>.

## Install and run

1. Put `Emmin.py`, `Emmin.bat` and `ui.html` together in any folder.
2. Double-click `Emmin.bat`. It finds a suitable Python on its own, starts Emmin, and opens it in your browser.
3. Leave the black console window open while you use Emmin. Close it, or press **Quit** in the app, when you're done.

Elin reads `loadorder.txt` when it launches, so restart the game after saving.

## Is it safe? Exactly what it does on your PC

Everything is plain, readable text; there's nothing compiled or hidden. In detail:

**Network**
- Emmin runs a small web server that only listens on `127.0.0.1` (your own PC), on a random port with a random access token each launch. Nothing outside your computer can reach it.
- It only ever contacts Valve:
  - `api.steampowered.com`, for Workshop details, search and update checks.
  - `steamcdn-a.akamaihd.net`, to download SteamCMD, and only if you click **Install SteamCMD for me**.
  - SteamCMD itself talks to Steam when you download.
- Mod thumbnails load in your browser from Steam's image servers.
- No analytics, no telemetry, nothing sent anywhere else.

**Files it writes**
- `Elin\loadorder.txt`, when you save. A timestamped backup of the old file goes next to it first, and the last 10 are kept.
- `Elin\BepInEx\config\*.cfg`: only the values you change. Backups go in `.emmin-backups`, the last 5 per mod.
- `Elin\Package\<workshop id>`: mods you download or copy. Downloads arrive in a staging folder next to SteamCMD first, then get moved into Package. No symlinks or junctions.
- Its own folder:
  - `emmin_settings.json`: your settings.
  - `emmin_steam_login.bat`: created when you log in to Steam.
  - `steamcmd\`: only if you let Emmin install SteamCMD.

**Programs it starts**
- SteamCMD, only when you use the Workshop features. It runs hidden, except for the login window.
- Windows Explorer, for **Open folder**.
- `steam://` links, for **Play** and **Open in Steam**.

**Your Steam password is never seen by Emmin.** Logging in happens in SteamCMD's own window, and SteamCMD remembers the login itself.

**About antivirus or site-scan flags:** `Emmin.bat` searches your PC for Python and runs it, and `Emmin.py` starts a local server and opens folders and Steam links. Automated scanners are wary of exactly that behaviour, even when it's harmless. Everything it does is listed above, and you can read both files in any text editor.

## Keep your settings file private

`emmin_settings.json` contains your Steam Web API key and Steam account name. Don't share it or upload it anywhere. If it ever leaks, revoke the key at <https://steamcommunity.com/dev/apikey> and make a new one.

## Good to know

- **Mods you turn off move to the bottom of `loadorder.txt`.** A disabled mod isn't loaded, so its position doesn't matter. Keeping every enabled mod together at the top is what lets two panes describe one file. It changes nothing about how Elin reads the file.
- **Close Elin before editing configs.** BepInEx can rewrite them while the game runs.
- **Mods in `Elin\Package` are yours to manage.** Steam won't update them; use **Check for updates** in Emmin instead.
- **Old settings carry over.** If you used Emmin back when it was called "Elin Mod Manager", your old `elin_mm_settings.json` is carried over on first launch.

## Credits

Designed and tested by **Total Misplay**. Code written by **Claude** (Anthropic).

## Permissions

Do whatever you want with it.
