#!/usr/bin/env python3
"""
Emmin - Eternal Mod Manager of Nefia
------------------------------------
A local mod manager for Elin. Python does the file work; your browser draws the list.

Run it:  py -3 Emmin.py     (or double-click Emmin.bat next to it)

Standard library only. Nothing to install, nothing phones home, nothing is written
outside your Elin config folder and this script's own settings file.
"""

import sys

if sys.version_info < (3, 8):
    print("This needs Python 3.8 or newer. You're on %s." % sys.version.split()[0])
    input("Press Enter to close.")
    raise SystemExit(1)

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

APP_ID = "2135150"
HERE = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(16)
IS_WIN = os.name == "nt"


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
def adopt_old(new: Path, old: Path):
    """Emmin used to be called Elin Mod Manager. Carry the old settings file (sort rules, saved
    lists, folder paths) over to the new name, once, before anything creates an empty new one."""
    try:
        if old.is_file() and (not new.exists() or new.stat().st_size == 0):
            if new.exists():
                new.unlink()
            old.replace(new)
            print("Carried your settings over from %s" % old.name)
    except OSError as e:
        print("Couldn't carry old settings over from %s: %s" % (old, e))


def settings_path() -> Path:
    local = HERE / "emmin_settings.json"
    adopt_old(local, HERE / "elin_mm_settings.json")
    try:  # prefer sitting next to the script, so the whole thing stays portable
        local.touch(exist_ok=True)
        return local
    except OSError:
        appdata = Path(os.environ.get("APPDATA", Path.home()))
        base = appdata / "Emmin"
        base.mkdir(parents=True, exist_ok=True)
        adopt_old(base / "settings.json", appdata / "ElinModManager" / "settings.json")
        return base / "settings.json"


SETTINGS_FILE = settings_path()

DEFAULT_SETTINGS = {
    "config_dir": "",     # the Elin install folder, which holds loadorder.txt
    "workshop_dir": "",   # ...\steamapps\workshop\content\2135150
    "local_dir": "",      # ...\steamapps\common\Elin\Package
    "profiles": [],       # [{name, saved, entries:[{path, enabled}]}]
    "seen_updates": {},   # workshop id -> timeupdated last acknowledged
    "sort_rules": {       # user sort rules, keyed by lowercase package id
        "version": 1, "usePackagePriority": True, "groups": {}, "mods": {},
    },
    "backup_keep": 10,
    "steamcmd": {"path": "", "username": "", "loggedIn": False},
    "local_copies": {},   # workshop id -> {installed: time_updated at install, via, at}
    "steam_api_key": "",  # for the Workshop browser only; never shown back in full
    "show_mature": False,
    "download_list": [],
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
    except Exception:
        pass
    return s


def save_settings(s: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), "utf-8")
    except OSError as e:
        print("Couldn't write settings: %s" % e)


SETTINGS = load_settings()


# --------------------------------------------------------------------------
# Steam discovery
# --------------------------------------------------------------------------
def steam_root() -> str:
    if IS_WIN:
        try:
            import winreg
            for hive, sub in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        val = winreg.QueryValueEx(k, "SteamPath" if hive == winreg.HKEY_CURRENT_USER
                                                  else "InstallPath")[0]
                        if val and Path(val).exists():
                            return str(Path(val))
                except OSError:
                    continue
        except Exception:
            pass
        for guess in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
            if Path(guess).exists():
                return guess
    else:
        for guess in (Path.home() / ".steam/steam", Path.home() / ".local/share/Steam",
                      Path.home() / "Library/Application Support/Steam"):
            if guess.exists():
                return str(guess)
    return ""


def parse_vdf(text: str):
    """Minimal Valve KeyValues reader. Good enough for libraryfolders.vdf and .acf files."""
    tok = re.compile(r'"((?:[^"\\]|\\.)*)"|(\{)|(\})')
    stack = [{}]
    pending = None
    for m in tok.finditer(text):
        string, open_b, close_b = m.group(1), m.group(2), m.group(3)
        if string is not None:
            string = string.replace(r"\"", '"').replace(r"\\", "\\")
            if pending is None:
                pending = string
            else:
                stack[-1][pending] = string
                pending = None
        elif open_b:
            node = {}
            if pending is not None:
                stack[-1][pending] = node
                pending = None
            stack.append(node)
        elif close_b and len(stack) > 1:
            stack.pop()
    return stack[0]


def steam_libraries():
    root = steam_root()
    libs = []
    if not root:
        return libs
    libs.append(str(Path(root)))
    vdf = Path(root) / "steamapps" / "libraryfolders.vdf"
    try:
        data = parse_vdf(vdf.read_text("utf-8", errors="replace"))
        folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
        for _, v in folders.items():
            p = v.get("path") if isinstance(v, dict) else (v if isinstance(v, str) else None)
            if p and Path(p).exists():
                libs.append(str(Path(p)))
    except Exception:
        pass
    out, seen = [], set()
    for p in libs:
        k = p.lower()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def detect_paths() -> dict:
    """loadorder.txt sits in the game install folder itself - not in AppData, whatever
    every other Unity game does. Mods are in Elin\\Package, mod settings in Elin\\BepInEx\\config."""
    found = {"config_dir": "", "workshop_dir": "", "local_dir": "", "bepinex_dir": "",
             "libraries": steam_libraries()}
    for lib in found["libraries"]:
        ws = Path(lib) / "steamapps" / "workshop" / "content" / APP_ID
        if not found["workshop_dir"] and ws.is_dir():
            found["workshop_dir"] = str(ws)
        elin = Path(lib) / "steamapps" / "common" / "Elin"
        if not elin.is_dir():
            continue
        if not found["local_dir"] and (elin / "Package").is_dir():
            found["local_dir"] = str(elin / "Package")
        if not found["config_dir"] and (elin / "loadorder.txt").is_file():
            found["config_dir"] = str(elin)
        if not found["bepinex_dir"] and (elin / "BepInEx" / "config").is_dir():
            found["bepinex_dir"] = str(elin / "BepInEx" / "config")
    # the game folder exists but has never been launched with mods, so no loadorder.txt yet
    if not found["config_dir"] and found["local_dir"]:
        found["config_dir"] = str(Path(found["local_dir"]).parent)
    return found


def effective_paths() -> dict:
    """Whatever the user set wins; anything blank gets filled in by detection."""
    det = detect_paths()
    out = {}
    for k in ("config_dir", "workshop_dir", "local_dir"):
        out[k] = SETTINGS.get(k) or det.get(k, "")
    bep = det.get("bepinex_dir", "")
    if not bep and out["config_dir"]:
        guess = Path(out["config_dir"]) / "BepInEx" / "config"
        bep = str(guess) if guess.is_dir() else ""
    out["bepinex_dir"] = bep
    out["libraries"] = det["libraries"]
    out["detected"] = det
    return out


# --------------------------------------------------------------------------
# workshop metadata (subscribed items, update times)
# --------------------------------------------------------------------------
def workshop_times(workshop_dir: str) -> dict:
    """{workshop id: unix time it was last updated}, straight from Steam's own records."""
    if not workshop_dir:
        return {}
    acf = Path(workshop_dir).parent.parent / ("appworkshop_%s.acf" % APP_ID)
    try:
        data = parse_vdf(acf.read_text("utf-8", errors="replace"))
    except Exception:
        return {}
    root = data.get("AppWorkshop", {})
    items = root.get("WorkshopItemsInstalled", {})
    out = {}
    for wid, info in items.items():
        if isinstance(info, dict):
            try:
                out[wid] = int(info.get("timeupdated", 0))
            except (TypeError, ValueError):
                pass
    return out


# --------------------------------------------------------------------------
# package.xml
# --------------------------------------------------------------------------
TAGS = ("title", "id", "author", "version", "tags", "description", "loadPriority", "builtin")


def read_package(folder: Path) -> dict:
    xml = folder / "package.xml"
    if not xml.is_file():
        for sub in sorted(folder.iterdir()) if folder.is_dir() else []:
            if sub.is_dir() and (sub / "package.xml").is_file():
                xml = sub / "package.xml"
                break
    if not xml.is_file():
        return None
    try:
        text = xml.read_text("utf-8-sig", errors="replace")
    except OSError:
        return None
    vals = {t: "" for t in TAGS}
    try:
        root = ET.fromstring(text.strip())
        for t in TAGS:
            node = root.find(t)
            if node is not None and node.text:
                vals[t] = node.text.strip()
    except ET.ParseError:  # malformed xml still usually has readable fields
        for t in TAGS:
            m = re.search(r"<%s>(.*?)</%s>" % (t, t), text, re.S | re.I)
            if m:
                vals[t] = m.group(1).strip()
        if not any(vals.values()):
            return None
    raw_p = vals["loadPriority"].strip()
    try:
        priority = float(raw_p)
        valid_p = bool(raw_p) and priority == priority and abs(priority) != float("inf")
    except ValueError:
        priority, valid_p = 0.0, False
    if not valid_p:
        priority = 0.0
    return {
        "title": vals["title"], "id": vals["id"], "author": vals["author"],
        "version": vals["version"], "tags": vals["tags"], "description": vals["description"],
        "loadPriority": priority, "hasPriority": valid_p,
        "builtin": vals["builtin"].strip().lower() == "true",
    }


def find_preview(folder: Path) -> str:
    for nm in ("preview.jpg", "preview.png", "preview.jpeg", "Preview.jpg", "Preview.png"):
        p = folder / nm
        if p.is_file():
            return str(p)
    return ""


def scan_dir(path: str, source: str) -> dict:
    out = {}
    if not path or not Path(path).is_dir():
        return out
    try:
        entries = sorted(Path(path).iterdir())
    except OSError:
        return out
    for folder in entries:
        if not folder.is_dir():
            continue
        meta = read_package(folder)
        if meta is None:
            continue
        out[str(folder).lower()] = {
            "path": str(folder), "folder": folder.name, "source": source,
            "meta": meta, "preview": find_preview(folder),
        }
    return out


# --------------------------------------------------------------------------
# loadorder.txt
# --------------------------------------------------------------------------
def loadorder_file() -> Path:
    cfg = effective_paths()["config_dir"]
    if not cfg:
        return None
    return Path(cfg) / "loadorder.txt"


def read_loadorder():
    f = loadorder_file()
    if not f or not f.is_file():
        return [], "\r\n", True, 0.0
    raw = f.read_bytes().decode("utf-8-sig", errors="replace")  # bytes, so CRLF survives
    eol = "\r\n" if "\r\n" in raw else "\n"
    trailing = raw.endswith("\n")
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        path, _, flag = line.rpartition(",")
        if not path:                       # a line with no comma at all
            path, flag = line, "1"
        entries.append({"path": path.strip().rstrip("\\"), "enabled": flag.strip() != "0"})
    return entries, eol, trailing, f.stat().st_mtime


def write_loadorder(rows, eol, trailing):
    f = loadorder_file()
    if not f:
        raise RuntimeError("No config folder set.")
    f.parent.mkdir(parents=True, exist_ok=True)
    body = eol.join("%s,%d" % (r["path"], 1 if r["enabled"] else 0) for r in rows)
    if trailing:
        body += eol
    backup = ""
    if f.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = str(f.parent / ("loadorder.backup-%s.txt" % stamp))
        shutil.copy2(str(f), backup)
        prune_backups(f.parent)
    tmp = f.with_suffix(".txt.tmp")
    tmp.write_text(body, "utf-8", newline="")
    os.replace(str(tmp), str(f))
    return backup, f.stat().st_mtime


def prune_backups(folder: Path):
    keep = int(SETTINGS.get("backup_keep", 10))
    files = sorted(folder.glob("loadorder.backup-*.txt"))
    for old in files[:max(0, len(files) - keep)]:
        try:
            old.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# BepInEx configs
#
# Only ever touches the text after " = " on lines the user changed. Comments, order,
# spacing, blank lines, BOM and line endings all come back out exactly as they went in.
# --------------------------------------------------------------------------
RANGE_RE = re.compile(r"From\s+(\S+)\s+to\s+(\S+)", re.I)
HEAD_PLUGIN = re.compile(r"Settings file was created by plugin (.+?) v(\S+)\s*$")
HEAD_GUID = re.compile(r"Plugin GUID:\s*(.+?)\s*$")
VALUE_PREFIX = re.compile(r"^([^=]*=[ \t]*)")


def cfg_dir():
    d = effective_paths().get("bepinex_dir", "")
    return Path(d) if d and Path(d).is_dir() else None


def cfg_path(name: str) -> Path:
    """Resolve a config by bare file name only - never a path someone smuggled in."""
    d = cfg_dir()
    if not d:
        raise FileNotFoundError("No BepInEx\\config folder found.")
    if not name or Path(name).name != name or not name.lower().endswith(".cfg"):
        raise ValueError("Bad config name.")
    p = d / name
    if not p.is_file():
        raise FileNotFoundError(name)
    return p


def split_cfg(raw: str):
    bom = raw.startswith("\ufeff")
    if bom:
        raw = raw[1:]
    eol = "\r\n" if "\r\n" in raw else "\n"
    return re.split(r"\r?\n", raw), eol, bom


def parse_cfg(raw: str) -> dict:
    lines, eol, bom = split_cfg(raw)
    meta = {"plugin": "", "version": "", "guid": ""}
    sections, cur = [], None

    def fresh():
        return {"desc": [], "type": "", "default": None, "acceptable": None,
                "range": None, "multi": False, "notes": []}
    pend = fresh()

    for i, line in enumerate(lines):
        st = line.strip()
        if not st:
            continue
        if st.startswith("[") and st.endswith("]"):
            cur = {"name": st[1:-1].strip(), "entries": []}
            sections.append(cur)
            pend = fresh()
            continue
        if st.startswith("##"):
            body = st[2:].strip()
            if cur is None:              # the file header, above the first section
                m = HEAD_PLUGIN.search(body)
                if m:
                    meta["plugin"], meta["version"] = m.group(1), m.group(2)
                m = HEAD_GUID.search(body)
                if m:
                    meta["guid"] = m.group(1)
                continue
            pend["desc"].append(body)
            continue
        if st.startswith("#"):
            body = st[1:].strip()
            low = body.lower()
            if low.startswith("setting type:"):
                pend["type"] = body.split(":", 1)[1].strip()
            elif low.startswith("default value:"):
                pend["default"] = body.split(":", 1)[1].strip()
            elif low.startswith("acceptable values:"):
                pend["acceptable"] = [v.strip() for v in body.split(":", 1)[1].split(",") if v.strip()]
            elif low.startswith("acceptable value range:"):
                m = RANGE_RE.search(body)
                if m:
                    pend["range"] = {"min": m.group(1), "max": m.group(2)}
            elif low.startswith("multiple values can be set"):
                pend["multi"] = True
            else:
                pend["notes"].append(body)
            continue
        if cur is not None and "=" in line:
            key, _, val = line.partition("=")
            cur["entries"].append({
                "line": i, "key": key.strip(), "value": val.strip(),
                "description": "\n".join(pend["desc"]).strip(),
                "type": pend["type"], "default": pend["default"],
                "acceptable": pend["acceptable"], "range": pend["range"],
                "multi": pend["multi"], "notes": pend["notes"],
            })
            pend = fresh()
    return {"meta": meta, "sections": sections,
            "count": sum(len(sec["entries"]) for sec in sections)}


def list_configs() -> dict:
    d = cfg_dir()
    if not d:
        return {"dir": "", "files": []}
    out = []
    for p in sorted(d.glob("*.cfg"), key=lambda x: x.name.lower()):
        try:
            parsed = parse_cfg(p.read_bytes().decode("utf-8", errors="replace"))
        except OSError:
            continue
        out.append({"name": p.name, "mtime": p.stat().st_mtime, "count": parsed["count"],
                    "sections": len(parsed["sections"]), **parsed["meta"]})
    return {"dir": str(d), "files": out}


def read_config(name: str) -> dict:
    p = cfg_path(name)
    data = parse_cfg(p.read_bytes().decode("utf-8", errors="replace"))
    data.update({"name": name, "mtime": p.stat().st_mtime, "path": str(p)})
    return data


def backup_config(p: Path):
    bdir = p.parent / ".emmin-backups"
    bdir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")        # microseconds: no collisions
    shutil.copy2(str(p), str(bdir / ("%s.%s.cfg" % (p.stem, stamp))))
    # match this plugin's backups exactly, so "a.b" never prunes "a.b.extra"
    mine = re.compile(re.escape(p.stem) + r"\.\d{8}-\d{6}-\d{6}\.cfg$")
    olds = sorted(x for x in bdir.iterdir() if mine.match(x.name))
    for old in olds[:max(0, len(olds) - 5)]:
        try:
            old.unlink()
        except OSError:
            pass


def write_config(name: str, changes, mtime=None, force=False) -> dict:
    p = cfg_path(name)
    if not force and mtime and abs(p.stat().st_mtime - float(mtime)) > 0.001:
        raise ChangedOnDisk()
    raw = p.read_bytes().decode("utf-8", errors="replace")
    lines, eol, bom = split_cfg(raw)
    for ch in changes:
        i = int(ch["line"])
        if not (0 <= i < len(lines)) or "=" not in lines[i] \
                or lines[i].partition("=")[0].strip() != ch["key"]:
            raise ChangedOnDisk()          # the file moved under us; don't write into the wrong line
        val = str(ch["value"]).replace("\r", " ").replace("\n", " ").strip()
        lines[i] = VALUE_PREFIX.match(lines[i]).group(1) + val
    backup_config(p)
    tmp = p.with_suffix(".cfg.emmin-tmp")
    tmp.write_bytes((("\ufeff" if bom else "") + eol.join(lines)).encode("utf-8"))
    os.replace(str(tmp), str(p))
    return read_config(name)


class ChangedOnDisk(Exception):
    pass


# --------------------------------------------------------------------------
# Steam Workshop: SteamCMD downloads, local copies, update checks
#
# SteamCMD always downloads into its own folder tree. Emmin points it at a staging folder
# beside steamcmd.exe, then MOVES each finished mod into Elin\Package\<workshop id>.
# No links of any kind: the files live in Package, where the Steam client can't delete them.
# --------------------------------------------------------------------------
STEAMCMD_ZIP = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
STEAM_API = "https://api.steampowered.com/ISteamRemoteStorage/%s/v1/"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,64}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
DL_OK_RE = re.compile(r"Success\.\s+Downloaded item (\d+)")
DL_FAIL_RE = re.compile(r"ERROR!\s+Download item (\d+) failed \(([^)]*)\)")
NEEDS_LOGIN_MARKS = ("password:", "steam guard code", "two-factor code", "two factor code",
                     "cached credentials not found", "login failure", "invalid password",
                     "invalidpassword", "accountlogindeniedneedtwofactor", "failed login")
LOGIN_OK_MARKS = ("waiting for user info...ok", "logged in ok")
LOGIN_DEADLINE = 300   # seconds to confirm a login before assuming SteamCMD is stuck at a prompt we can't see


class NeedsLogin(Exception):
    pass


class JobCancelled(Exception):
    pass


def steamcmd_exe() -> str:
    cfg = (SETTINGS.get("steamcmd") or {}).get("path", "")
    names = ("steamcmd.exe",) if IS_WIN else ("steamcmd.sh", "steamcmd")
    cands = []
    if cfg:
        p = Path(cfg)
        cands += [p] if p.suffix else [p / n for n in names]
    cands += [HERE / "steamcmd" / n for n in names]
    if IS_WIN:
        cands += [Path(r"C:\steamcmd\steamcmd.exe")]
    for c in cands:
        if c.is_file():
            return str(c)
    found = shutil.which("steamcmd")
    return found or ""


def steam_username() -> str:
    return (SETTINGS.get("steamcmd") or {}).get("username", "")


def local_copies() -> dict:
    return SETTINGS.setdefault("local_copies", {})


# ---------- the Steam Web API: public, no key needed for these two calls ----------
def steam_api(method: str, fields: dict) -> dict:
    import urllib.request
    import urllib.parse
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(STEAM_API % method, data=data, headers={"User-Agent": "Emmin"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def fetch_details(ids) -> dict:
    """{id: {result, title, time_updated, file_size, banned, app}} - result 1 means it's live."""
    ids = [str(i) for i in ids if str(i).isdigit()]
    out = {}
    for n in range(0, len(ids), 100):
        batch = ids[n:n + 100]
        fields = {"itemcount": len(batch)}
        for i, wid in enumerate(batch):
            fields["publishedfileids[%d]" % i] = wid
        resp = steam_api("GetPublishedFileDetails", fields).get("response", {})
        for d in resp.get("publishedfiledetails", []) or []:
            wid = str(d.get("publishedfileid", ""))
            out[wid] = {
                "result": int(d.get("result", 0) or 0),
                "title": d.get("title", ""),
                "time_updated": int(d.get("time_updated", 0) or 0),
                "file_size": int(d.get("file_size", 0) or 0),
                "banned": bool(d.get("banned", 0)),
                "app": str(d.get("consumer_app_id", "")),
            }
    return out


def expand_collections(ids) -> tuple:
    """Turns any collection ids into the items inside them. Returns (items, collections)."""
    ids = [str(i) for i in ids]
    if not ids:
        return [], {}
    fields = {"collectioncount": len(ids)}
    for i, wid in enumerate(ids):
        fields["publishedfileids[%d]" % i] = wid
    try:
        resp = steam_api("GetCollectionDetails", fields).get("response", {})
    except Exception:
        return ids, {}
    kids, cols = {}, {}
    for c in resp.get("collectiondetails", []) or []:
        children = [str(ch.get("publishedfileid")) for ch in (c.get("children") or [])
                    if ch.get("publishedfileid")]
        if children:
            kids[str(c.get("publishedfileid"))] = children
    out = []
    for wid in ids:
        if wid in kids:
            cols[wid] = len(kids[wid])
            out += kids[wid]
        else:
            out.append(wid)
    seen, uniq = set(), []
    for w in out:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq, cols


def parse_ids(text: str):
    found = []
    for m in re.finditer(r"[?&]id=(\d+)|\b(\d{6,})\b", text or ""):
        wid = m.group(1) or m.group(2)
        if wid not in found:
            found.append(wid)
    return found


def installed_ids() -> dict:
    """Which workshop ids are on disk, and where: {'workshop': set, 'package': set}."""
    p = effective_paths()
    out = {"workshop": set(), "package": set()}
    for key, d in (("workshop", p["workshop_dir"]), ("package", p["local_dir"])):
        if d and Path(d).is_dir():
            for c in Path(d).iterdir():
                if c.is_dir() and c.name.isdigit():
                    out[key].add(c.name)
    return out


# ---------- one background job at a time ----------
JOB = {"id": 0, "kind": "", "running": False, "lines": [], "items": {}, "summary": "",
       "error": "", "needsLogin": False, "ok": [], "started": 0, "finished": 0}
JOB_LOCK = threading.Lock()
_PROC = {"p": None, "cancel": False}


def jlog(msg: str):
    msg = ANSI_RE.sub("", msg).rstrip()
    if msg:
        JOB["lines"].append(msg)
        del JOB["lines"][:-500]


def start_job(kind: str, fn, *args) -> bool:
    with JOB_LOCK:
        if JOB["running"]:
            return False
        JOB.update({"id": JOB["id"] + 1, "kind": kind, "running": True, "lines": [], "items": {},
                    "summary": "", "error": "", "needsLogin": False, "ok": [],
                    "started": time.time(), "finished": 0})
        _PROC["cancel"] = False

    def wrap():
        try:
            fn(*args)
        except NeedsLogin:
            JOB["needsLogin"] = True
            JOB["error"] = "SteamCMD needs you to log in. Use \"Log in to Steam\" in the SteamCMD panel."
            jlog("!! " + JOB["error"])
        except JobCancelled:
            JOB["error"] = "Cancelled."
            jlog("!! Cancelled.")
        except Exception as e:
            JOB["error"] = str(e) or e.__class__.__name__
            jlog("!! " + JOB["error"])
        finally:
            JOB["running"] = False
            JOB["finished"] = time.time()
            _PROC["p"] = None
    threading.Thread(target=wrap, daemon=True).start()
    return True


def run_steamcmd(args, timeout=3600, on_line=None) -> str:
    """Runs SteamCMD with no console and no stdin. Reads raw bytes rather than lines, because
    SteamCMD's password prompt has no newline and a line reader would wait on it forever."""
    import queue
    exe = steamcmd_exe()
    if not exe:
        raise RuntimeError("SteamCMD isn't set up yet.")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
    p = subprocess.Popen([exe] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, cwd=str(Path(exe).parent), creationflags=flags)
    _PROC["p"] = p
    q = queue.Queue()

    def pump():
        fd = p.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                q.put(None)
                return
            q.put(chunk)
    threading.Thread(target=pump, daemon=True).start()

    buf, everything, start = "", [], time.time()
    wants_login = "+login" in args
    logged_in = False

    def stop():
        try:
            p.kill()
        except OSError:
            pass
    while True:
        try:
            chunk = q.get(timeout=0.5)
        except queue.Empty:
            chunk = b""
        if chunk is None:
            break
        if chunk:
            buf += chunk.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = ANSI_RE.sub("", line).replace("\r", "").strip()
                if not line:
                    continue
                everything.append(line)
                jlog(line)
                if on_line:
                    on_line(line)
                low = line.lower()
                if any(m in low for m in LOGIN_OK_MARKS):
                    logged_in = True
                if any(m in low for m in NEEDS_LOGIN_MARKS) and "anonymous" not in low:
                    stop()
                    raise NeedsLogin()
            if any(m in buf.lower() for m in NEEDS_LOGIN_MARKS):   # a prompt, waiting with no newline
                stop()
                raise NeedsLogin()
            if any(m in buf.lower() for m in LOGIN_OK_MARKS):
                logged_in = True
        # SteamCMD can hold output back when piped. If it never confirms a login, assume it's
        # sitting at a password prompt we can't see, rather than waiting out the full timeout.
        if wants_login and not logged_in and time.time() - start > LOGIN_DEADLINE:
            stop()
            raise NeedsLogin()
        if _PROC["cancel"]:
            stop()
            raise JobCancelled()
        if time.time() - start > timeout:
            stop()
            raise RuntimeError("SteamCMD took too long and was stopped.")
    p.wait()
    if buf.strip():
        everything.append(ANSI_RE.sub("", buf).strip())
        jlog(buf)
    return "\n".join(everything)


# ---------- the jobs ----------
def job_install_steamcmd():
    import urllib.request
    import zipfile
    import io
    if not IS_WIN:
        raise RuntimeError("Automatic install is Windows-only. Install SteamCMD yourself and point Emmin at it.")
    dest = HERE / "steamcmd"
    dest.mkdir(exist_ok=True)
    jlog("Downloading SteamCMD from Valve...")
    with urllib.request.urlopen(STEAMCMD_ZIP, timeout=60) as r:
        data = r.read()
    zipfile.ZipFile(io.BytesIO(data)).extractall(str(dest))
    SETTINGS.setdefault("steamcmd", {})["path"] = str(dest / "steamcmd.exe")
    save_settings(SETTINGS)
    jlog("Unpacked to %s. First run: SteamCMD updates itself, which takes a minute or two..." % dest)
    run_steamcmd(["+quit"], timeout=1200)
    JOB["summary"] = "SteamCMD is installed. Next: log in to Steam."


def job_test_login():
    user = steam_username()
    if not user:
        raise RuntimeError("Enter your Steam username first.")
    out = run_steamcmd(["+login", user, "+quit"], timeout=180).lower()
    ok = any(m in out for m in LOGIN_OK_MARKS)
    SETTINGS.setdefault("steamcmd", {})["loggedIn"] = ok
    save_settings(SETTINGS)
    if not ok:
        raise NeedsLogin()
    JOB["summary"] = "Logged in as %s. SteamCMD will remember it." % user


def staging_dir() -> Path:
    return Path(steamcmd_exe()).parent / "emmin_staging"


def move_into_package(src: Path, wid: str) -> Path:
    """Swap a finished download into Package\\<id>. The old copy is only deleted once the new
    one is fully in place, and put back if anything goes wrong."""
    pkg = effective_paths()["local_dir"]
    if not pkg or not Path(pkg).is_dir():
        raise RuntimeError("No Elin\\Package folder found. Check Folders.")
    dst = Path(pkg) / wid
    old = Path(pkg) / (wid + ".emmin-old")
    if old.exists():
        shutil.rmtree(str(old), ignore_errors=True)
    if dst.exists():
        dst.rename(old)
    try:
        shutil.move(str(src), str(dst))
    except Exception:
        if old.exists() and not dst.exists():
            old.rename(dst)
        raise
    if old.exists():
        shutil.rmtree(str(old), ignore_errors=True)
    return dst


def job_download(ids):
    user = steam_username()
    if not user:
        raise RuntimeError("Enter your Steam username and log in first.")
    ids = [i for i in ids if str(i).isdigit()]
    if not ids:
        raise RuntimeError("Nothing to download.")
    stage = staging_dir()
    # Wipe SteamCMD's memory of earlier downloads: we move files out after every run, and if it
    # still believed they were installed it would report Success and fetch nothing.
    shutil.rmtree(str(stage / "steamapps" / "workshop"), ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    for i in ids:
        JOB["items"][i] = "queued"
    args = ["+force_install_dir", str(stage), "+login", user]
    for i in ids:
        args += ["+workshop_download_item", APP_ID, i, "validate"]
    args += ["+quit"]

    def seen(line):
        m = DL_OK_RE.search(line)
        if m and m.group(1) in JOB["items"]:
            JOB["items"][m.group(1)] = "downloaded"
        m = DL_FAIL_RE.search(line)
        if m and m.group(1) in JOB["items"]:
            JOB["items"][m.group(1)] = "failed: " + m.group(2)
    jlog("Downloading %d mod%s with SteamCMD..." % (len(ids), "" if len(ids) == 1 else "s"))
    run_steamcmd(args, timeout=3600, on_line=seen)

    times = {}
    try:
        times = {k: v["time_updated"] for k, v in fetch_details(ids).items()}
    except Exception:
        pass
    content = stage / "steamapps" / "workshop" / "content" / APP_ID
    for i in ids:
        src = content / i
        if JOB["items"].get(i) != "downloaded":
            if JOB["items"].get(i) == "queued":
                JOB["items"][i] = "failed: no result from SteamCMD"
            continue
        if not src.is_dir():
            JOB["items"][i] = "failed: SteamCMD said yes but left no files"
            continue
        try:
            dst = move_into_package(src, i)
            JOB["items"][i] = "installed"
            JOB["ok"].append(i)
            local_copies()[i] = {"installed": times.get(i, 0), "via": "steamcmd",
                                 "at": datetime.now().isoformat(timespec="seconds")}
            jlog("Moved %s into %s" % (i, dst))
        except Exception as e:
            JOB["items"][i] = "failed: " + str(e)
    save_settings(SETTINGS)
    bad = [i for i in ids if JOB["items"].get(i) != "installed"]
    JOB["summary"] = "%d installed into Package" % len(JOB["ok"]) + (", %d failed" % len(bad) if bad else "")


def job_copy_local(ids):
    """Copy Workshop mods you already have into Package. No SteamCMD, no internet: this is the
    one that still works after an author pulls their mod."""
    p = effective_paths()
    ws, pkg = p["workshop_dir"], p["local_dir"]
    if not ws or not pkg:
        raise RuntimeError("Workshop or Package folder not found. Check Folders.")
    times = workshop_times(ws)
    for i in ids:
        JOB["items"][i] = "queued"
    for i in ids:
        src = Path(ws) / i
        if not src.is_dir():
            JOB["items"][i] = "failed: not in your Workshop folder"
            continue
        tmp = Path(pkg) / (i + ".emmin-new")
        try:
            if tmp.exists():
                shutil.rmtree(str(tmp))
            jlog("Copying %s..." % i)
            shutil.copytree(str(src), str(tmp))
            move_into_package(tmp, i)
            local_copies()[i] = {"installed": times.get(i, 0), "via": "copy",
                                 "at": datetime.now().isoformat(timespec="seconds")}
            JOB["items"][i] = "installed"
            JOB["ok"].append(i)
        except Exception as e:
            shutil.rmtree(str(tmp), ignore_errors=True)
            JOB["items"][i] = "failed: " + str(e)
        if _PROC["cancel"]:
            raise JobCancelled()
    save_settings(SETTINGS)
    JOB["summary"] = "%d copied into Package" % len(JOB["ok"])


def open_login_window() -> str:
    """SteamCMD in its own console so the password and Steam Guard code go straight into
    SteamCMD. Emmin never sees them. Afterwards SteamCMD remembers the login."""
    exe, user = steamcmd_exe(), steam_username()
    if not exe:
        raise RuntimeError("SteamCMD isn't set up yet.")
    if not USERNAME_RE.match(user or ""):
        raise RuntimeError("That doesn't look like a Steam account name (letters, numbers, underscores).")
    if not IS_WIN:
        raise RuntimeError("Run this in a terminal: %s +login %s +quit" % (exe, user))
    bat = HERE / "emmin_steam_login.bat"
    bat.write_text('@echo off\r\ntitle Emmin - log in to Steam\r\n'
                   'echo Type your Steam password (and Steam Guard code if asked) below.\r\n'
                   'echo Emmin never sees them: this window IS SteamCMD.\r\necho.\r\n'
                   '"%s" +login %s +quit\r\necho.\r\n'
                   'echo If you saw "Waiting for user info...OK" you are logged in.\r\n'
                   'echo Close this window and press "Check login" in Emmin.\r\npause\r\n' % (exe, user),
                   encoding="utf-8")
    os.startfile(str(bat))
    return str(bat)


def workshop_status() -> dict:
    """What the Workshop says about every id on disk, for the update check."""
    have = installed_ids()
    ids = sorted(have["workshop"] | have["package"])
    details = fetch_details(ids) if ids else {}
    ws_times = workshop_times(effective_paths()["workshop_dir"])
    return {"details": details, "workshopTimes": ws_times, "localCopies": local_copies(),
            "checked": time.time()}


# --------------------------------------------------------------------------
# Workshop browser: Steam's official Web API, never page scraping. The redesigned Workshop
# page draws its listings with JavaScript and its markup keeps changing (that's what broke
# RimSort's buttons); the API returns the same data and doesn't change when the site does.
# Search needs a free Web API key: steamcommunity.com/dev/apikey
# --------------------------------------------------------------------------
KEY_RE = re.compile(r"^[0-9A-Fa-f]{32}$")
SORTS = {"trend": 3, "subscribed": 9, "recent": 1, "updated": 21, "top": 0, "relevance": 12}
_NAMES = {}          # steamid -> persona name, cached for the session


class BadKey(Exception):
    pass


def api_key() -> str:
    return SETTINGS.get("steam_api_key", "") or ""


def steam_get(path: str, params: dict) -> dict:
    import urllib.request
    import urllib.parse
    import urllib.error
    url = "https://api.steampowered.com/%s/?%s" % (path, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"User-Agent": "Emmin"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise BadKey()
        raise RuntimeError("Steam answered %d" % e.code)


def _bool(v):
    return "true" if v else "false"


def persona_names(ids) -> dict:
    want = [i for i in {str(x) for x in ids if x} if i not in _NAMES]
    key = api_key()
    for n in range(0, len(want), 100):
        try:
            r = steam_get("ISteamUser/GetPlayerSummaries/v2",
                          {"key": key, "steamids": ",".join(want[n:n + 100])})
            for p in (r.get("response", {}).get("players") or []):
                _NAMES[str(p.get("steamid"))] = p.get("personaname", "")
        except Exception:
            break
    return {str(i): _NAMES.get(str(i), "") for i in ids if i}


def _card(d: dict, have: dict, listed: set) -> dict:
    wid = str(d.get("publishedfileid", ""))
    vote = d.get("vote_data") or {}
    tags = [t.get("display_name") or t.get("tag") for t in (d.get("tags") or []) if (t.get("tag") or t.get("display_name"))]
    return {
        "id": wid, "title": d.get("title", ""), "preview": d.get("preview_url", ""),
        "desc": (d.get("short_description") or d.get("file_description") or "").strip(),
        "creator": str(d.get("creator", "")),
        "subs": int(d.get("subscriptions", 0) or 0), "favs": int(d.get("favorited", 0) or 0),
        "score": float(vote.get("score", 0) or 0), "up": int(vote.get("votes_up", 0) or 0),
        "down": int(vote.get("votes_down", 0) or 0),
        "size": int(d.get("file_size", 0) or 0), "updated": int(d.get("time_updated", 0) or 0),
        "created": int(d.get("time_created", 0) or 0), "tags": tags,
        "requires": [str(c.get("publishedfileid")) for c in (d.get("children") or []) if c.get("publishedfileid")],
        "mature": bool(d.get("maybe_inappropriate_sex") or d.get("maybe_inappropriate_violence")),
        "live": int(d.get("result", 1) or 1) == 1 and not d.get("banned"),
        "subscribed": wid in have["workshop"], "inPackage": wid in have["package"], "listed": wid in listed,
    }


def browse(q="", sort="trend", days=7, page=1, tags=(), mature=False) -> dict:
    key = api_key()
    if not key:
        raise BadKey()
    q = (q or "").strip()
    qt = SORTS.get(sort, 3)
    if q and sort == "relevance":
        qt = 12
    params = {"key": key, "appid": APP_ID, "query_type": qt, "page": max(1, int(page)),
              "numperpage": 30, "return_vote_data": "true", "return_tags": "true",
              "return_children": "true", "return_short_description": "true",
              "strip_description_bbcode": "true", "return_details": "true"}
    if q:
        params["search_text"] = q
    if qt == 3:
        params["days"] = int(days) if int(days) > 0 else 3650
    for i, t in enumerate([t for t in tags if t]):
        params["requiredtags[%d]" % i] = t
    if tags:
        params["match_all_tags"] = "true"
    resp = steam_get("IPublishedFileService/QueryFiles/v1", params).get("response", {})
    have, listed = installed_ids(), set(SETTINGS.get("download_list_ids", []))
    cards = [_card(d, have, listed) for d in (resp.get("publishedfiledetails") or [])]
    hidden = 0
    if not mature:
        hidden = sum(1 for c in cards if c["mature"])
        cards = [c for c in cards if not c["mature"]]
    names = persona_names([c["creator"] for c in cards])
    for c in cards:
        c["author"] = names.get(c["creator"], "")
    return {"items": cards, "total": int(resp.get("total", 0) or 0), "page": int(page), "hiddenMature": hidden}


def item_details(wid: str) -> dict:
    """One mod in full: description, screenshots, and the mods it says it needs."""
    key = api_key()
    if not key:
        raise BadKey()
    base = {"key": key, "includetags": "true", "includeadditionalpreviews": "true",
            "includechildren": "true", "includevotes": "true", "strip_description_bbcode": "true"}
    r = steam_get("IPublishedFileService/GetDetails/v1", {**base, "publishedfileids[0]": wid})
    det = (r.get("response", {}).get("publishedfiledetails") or [{}])[0]
    have, listed = installed_ids(), set(SETTINGS.get("download_list_ids", []))
    card = _card(det, have, listed)
    card["desc"] = (det.get("file_description") or card["desc"] or "").strip()
    card["shots"] = [p.get("url") for p in (det.get("previews") or [])
                     if p.get("url") and int(p.get("preview_type", 0) or 0) == 0]
    card["author"] = persona_names([card["creator"]]).get(card["creator"], "")
    reqs = []
    if card["requires"]:
        f = dict(base)
        for i, c in enumerate(card["requires"]):
            f["publishedfileids[%d]" % i] = c
        r2 = steam_get("IPublishedFileService/GetDetails/v1", f)
        by = {str(d.get("publishedfileid")): d for d in (r2.get("response", {}).get("publishedfiledetails") or [])}
        for c in card["requires"]:
            d = by.get(c, {"publishedfileid": c, "result": 9})
            x = _card(d, have, listed)
            reqs.append({k: x[k] for k in ("id", "title", "preview", "size", "live", "subscribed", "inPackage", "listed")})
    card["requiresInfo"] = reqs
    return card


def check_key(key: str) -> None:
    if not KEY_RE.match(key or ""):
        raise BadKey()
    steam_get("IPublishedFileService/QueryFiles/v1", {"key": key, "appid": APP_ID, "query_type": 1,
                                                       "numperpage": 1, "page": 1})


# ---------- the download list: kept in settings so it survives restarts ----------
def get_list() -> list:
    return SETTINGS.setdefault("download_list", [])


def set_list(items) -> list:
    clean, seen = [], set()
    for it in items or []:
        wid = str((it or {}).get("id", ""))
        if wid.isdigit() and wid not in seen:
            seen.add(wid)
            clean.append({"id": wid, "title": str(it.get("title", ""))[:200],
                          "size": int(it.get("size", 0) or 0), "preview": str(it.get("preview", ""))[:500]})
    SETTINGS["download_list"] = clean
    SETTINGS["download_list_ids"] = [c["id"] for c in clean]
    save_settings(SETTINGS)
    return clean


# --------------------------------------------------------------------------
# build the list the UI shows
# --------------------------------------------------------------------------
def build_state() -> dict:
    paths = effective_paths()
    entries, eol, trailing, mtime = read_loadorder()
    disk = {}
    disk.update(scan_dir(paths["workshop_dir"], "workshop"))
    disk.update(scan_dir(paths["local_dir"], "local"))
    # Fallback when a line's exact path doesn't match (e.g. the Steam library moved drives):
    # match by folder name, but within the folder type the line points into. Package copies of
    # Workshop mods are named by Workshop id, so a Workshop line must never land on its Package twin.
    by_folder = {(rec["source"], rec["folder"].lower()): rec for rec in disk.values()}

    def fallback(path):
        low = path.lower().replace("/", "\\")
        leaf = low.rstrip("\\").split("\\")[-1]
        src = "workshop" if "\\workshop\\" in low else "local"
        rec = by_folder.get((src, leaf))
        if rec is None and not leaf.isdigit():      # a named folder is unambiguous either way
            rec = by_folder.get(("local" if src == "workshop" else "workshop", leaf))
        return rec
    times = workshop_times(paths["workshop_dir"])
    seen = SETTINGS.get("seen_updates", {})
    first_run = not seen

    def decorate(rec, path, enabled, is_new):
        leaf = rec["folder"] if rec else Path(path.replace("\\", "/")).name
        src = rec["source"] if rec else ("workshop" if "\\workshop\\" in path.lower()
                                          or "/workshop/" in path.lower() else "local")
        wid = leaf if leaf.isdigit() else ""
        copy = local_copies().get(wid) if src == "local" and wid else None
        updated_at = (copy or {}).get("installed", 0) if src == "local" else times.get(wid, 0)
        was = seen.get(wid) if src == "workshop" else None
        return {
            "path": path,
            "folder": rec["folder"] if rec else Path(path.replace("\\", "/")).name,
            "enabled": enabled,
            "source": rec["source"] if rec else ("workshop" if "\\workshop\\" in path.lower() else "local"),
            "meta": rec["meta"] if rec else None,
            "hasPreview": bool(rec and rec["preview"]),
            "missing": rec is None,
            "isNew": is_new,
            "workshopId": wid,
            "updatedAt": updated_at,
            "updated": bool(wid and updated_at and was is not None and updated_at > was),
            "localCopy": bool(copy) or (src == "local" and bool(wid)),
        }

    rows, used = [], set()
    for e in entries:
        k = e["path"].lower()
        rec = disk.get(k) or fallback(e["path"])
        if rec:
            used.add(rec["path"].lower())
        rows.append(decorate(rec, e["path"], e["enabled"], False))

    for k, rec in sorted(disk.items()):
        if k in used or rec["meta"]["builtin"]:
            continue
        rows.append(decorate(rec, rec["path"], False, True))

    counts = {}
    for r in rows:
        mid = (r["meta"] or {}).get("id") or ""
        if mid:
            counts[mid] = counts.get(mid, 0) + 1
    for r in rows:
        mid = (r["meta"] or {}).get("id") or ""
        r["dup"] = bool(mid and counts[mid] > 1)

    if first_run and times:  # don't flag everything as freshly updated the first time
        SETTINGS["seen_updates"] = dict((k, v) for k, v in times.items())
        save_settings(SETTINGS)
        for r in rows:
            r["updated"] = False

    return {
        "rows": rows, "eol": eol, "trailing": trailing, "mtime": mtime,
        "paths": {k: paths[k] for k in ("config_dir", "workshop_dir", "local_dir")},
        "bepinexDir": paths.get("bepinex_dir", ""),
        "detected": paths["detected"], "libraries": paths["libraries"],
        "loadorderFile": str(loadorder_file() or ""),
        "loadorderExists": bool(loadorder_file() and loadorder_file().is_file()),
        "profiles": SETTINGS.get("profiles", []),
        "steamcmd": {"exe": steamcmd_exe(), "username": steam_username(),
                     "loggedIn": bool((SETTINGS.get("steamcmd") or {}).get("loggedIn"))},
        "rules": SETTINGS.get("sort_rules") or DEFAULT_SETTINGS["sort_rules"],
        "settingsFile": str(SETTINGS_FILE),
        "platform": "windows" if IS_WIN else sys.platform,
    }


# --------------------------------------------------------------------------
# shell helpers
# --------------------------------------------------------------------------
def reveal(path: str):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if IS_WIN:
        if p.is_dir():
            os.startfile(str(p))
        else:
            subprocess.Popen(["explorer", "/select,", str(p)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R" if p.is_file() else "", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])


def launch_game():
    open_uri("steam://rungameid/%s" % APP_ID)


def open_uri(url: str):
    if IS_WIN:
        os.startfile(url)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", url])
    else:
        subprocess.Popen(["xdg-open", url])


def inside_allowed(path: Path) -> bool:
    paths = effective_paths()
    roots = [paths["workshop_dir"], paths["local_dir"], paths["config_dir"]]
    try:
        rp = path.resolve()
    except OSError:
        return False
    for root in roots:
        if not root:
            continue
        try:
            rp.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------
UI_FILE = HERE / "ui.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "Emmin"

    def log_message(self, *a):
        pass  # keep the console quiet

    # -- plumbing ----------------------------------------------------------
    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _authed(self, q) -> bool:
        return (q.get("token", [""])[0] == TOKEN
                or self.headers.get("X-Token", "") == TOKEN)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                html = UI_FILE.read_text("utf-8")
            except OSError:
                return self._send(500, "ui.html is missing. Keep it in the same folder as the script.",
                                  "text/plain")
            return self._send(200, html, "text/html; charset=utf-8")
        if not self._authed(q):
            return self._send(403, "no", "text/plain")
        if u.path == "/api/state":
            try:
                return self._json(build_state())
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if u.path == "/api/workshop/list":
            return self._json({"items": get_list()})
        if u.path == "/api/workshop/key":
            k = api_key()
            return self._json({"set": bool(k), "hint": ("…" + k[-4:]) if k else "",
                               "mature": bool(SETTINGS.get("show_mature"))})
        if u.path == "/api/steamcmd":
            sc = SETTINGS.get("steamcmd") or {}
            return self._json({"exe": steamcmd_exe(), "username": sc.get("username", ""),
                               "loggedIn": bool(sc.get("loggedIn"))})
        if u.path == "/api/job":
            j = {k: v for k, v in JOB.items()}
            j["lines"] = JOB["lines"][-300:]
            return self._json(j)
        if u.path == "/api/configs":
            try:
                return self._json(list_configs())
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if u.path == "/api/config":
            try:
                return self._json(read_config(unquote(q.get("name", [""])[0])))
            except (FileNotFoundError, ValueError) as e:
                return self._json({"error": str(e)}, 404)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if u.path == "/api/preview":
            raw = unquote(q.get("path", [""])[0])
            folder = Path(raw)
            if not folder.is_dir() or not inside_allowed(folder):
                return self._send(404, b"", "image/png")
            img = find_preview(folder)
            if not img:
                return self._send(404, b"", "image/png")
            data = Path(img).read_bytes()
            ctype = "image/png" if img.lower().endswith(".png") else "image/jpeg"
            return self._send(200, data, ctype, {"Cache-Control": "max-age=60"})
        return self._send(404, "{}", "application/json")

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            return self._send(403, "no", "text/plain")
        body = self._body()

        if u.path == "/api/save":
            try:
                current = read_loadorder()[3]
                if body.get("mtime") and abs(current - float(body["mtime"])) > 0.001:
                    return self._json({"conflict": True,
                                       "message": "loadorder.txt changed on disk since this list was loaded."}, 409)
                backup, mtime = write_loadorder(body.get("rows", []),
                                                body.get("eol", "\r\n"),
                                                bool(body.get("trailing", True)))
                acknowledge_updates()
                return self._json({"ok": True, "backup": backup, "mtime": mtime})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if u.path == "/api/force-save":
            try:
                backup, mtime = write_loadorder(body.get("rows", []),
                                                body.get("eol", "\r\n"),
                                                bool(body.get("trailing", True)))
                acknowledge_updates()
                return self._json({"ok": True, "backup": backup, "mtime": mtime})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if u.path == "/api/paths":
            for k in ("config_dir", "workshop_dir", "local_dir"):
                if k in body:
                    SETTINGS[k] = (body[k] or "").strip().rstrip("\\")
            save_settings(SETTINGS)
            return self._json({"ok": True})

        if u.path == "/api/profiles":
            SETTINGS["profiles"] = body.get("profiles", [])
            save_settings(SETTINGS)
            return self._json({"ok": True})

        if u.path == "/api/steamcmd/config":
            sc = SETTINGS.setdefault("steamcmd", {})
            if "path" in body:
                sc["path"] = (body.get("path") or "").strip().strip('"')
            if "username" in body:
                user = (body.get("username") or "").strip()
                if user and not USERNAME_RE.match(user):
                    return self._json({"error": "Steam account names are letters, numbers and underscores."}, 400)
                if user != sc.get("username"):
                    sc["loggedIn"] = False
                sc["username"] = user
            save_settings(SETTINGS)
            return self._json({"ok": True, "exe": steamcmd_exe()})
        if u.path in ("/api/steamcmd/install", "/api/steamcmd/test", "/api/workshop/download",
                      "/api/workshop/copy"):
            fn = {"/api/steamcmd/install": (job_install_steamcmd, "install", ()),
                  "/api/steamcmd/test": (job_test_login, "login", ()),
                  "/api/workshop/download": (job_download, "download", ([str(i) for i in body.get("ids", [])],)),
                  "/api/workshop/copy": (job_copy_local, "copy", ([str(i) for i in body.get("ids", [])],)),
                  }[u.path]
            if not start_job(fn[1], fn[0], *fn[2]):
                return self._json({"error": "Something else is already running. Wait for it to finish."}, 409)
            return self._json({"ok": True, "job": JOB["id"]})
        if u.path == "/api/job/cancel":
            _PROC["cancel"] = True
            p = _PROC.get("p")
            if p:
                try:
                    p.kill()
                except OSError:
                    pass
            return self._json({"ok": True})
        if u.path == "/api/steamcmd/login-window":
            try:
                open_login_window()
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if u.path == "/api/workshop/resolve":
            try:
                raw = parse_ids(body.get("text", ""))
                if not raw:
                    return self._json({"error": "No Workshop ids or links found in that."}, 400)
                items, cols = expand_collections(raw)
                det = fetch_details(items)
                have = installed_ids()
                out = []
                for i in items:
                    d = det.get(i, {"result": 0})
                    out.append({"id": i, **d, "subscribed": i in have["workshop"],
                                "inPackage": i in have["package"],
                                "wrongGame": bool(d.get("app")) and d.get("app") != APP_ID})
                return self._json({"items": out, "collections": cols})
            except Exception as e:
                return self._json({"error": "Couldn't reach Steam: %s" % e}, 502)
        if u.path == "/api/workshop/key":
            k = (body.get("key") or "").strip()
            if "mature" in body:
                SETTINGS["show_mature"] = bool(body.get("mature"))
                save_settings(SETTINGS)
                if not k:
                    return self._json({"ok": True})
            if not k:
                SETTINGS["steam_api_key"] = ""
                save_settings(SETTINGS)
                return self._json({"ok": True, "set": False})
            try:
                check_key(k)
            except BadKey:
                return self._json({"error": "Steam didn't accept that key. It should be 32 letters and numbers, copied from steamcommunity.com/dev/apikey."}, 400)
            except Exception as e:
                return self._json({"error": "Couldn't reach Steam to check it: %s" % e}, 502)
            SETTINGS["steam_api_key"] = k
            save_settings(SETTINGS)
            return self._json({"ok": True, "set": True, "hint": "…" + k[-4:]})
        if u.path == "/api/workshop/browse":
            try:
                return self._json(browse(body.get("q", ""), body.get("sort", "trend"), body.get("days", 7),
                                         body.get("page", 1), body.get("tags", []),
                                         bool(SETTINGS.get("show_mature"))))
            except BadKey:
                return self._json({"error": "badkey"}, 401)
            except Exception as e:
                return self._json({"error": "Couldn't reach Steam: %s" % e}, 502)
        if u.path == "/api/workshop/item":
            try:
                wid = str(body.get("id", ""))
                if not wid.isdigit():
                    return self._json({"error": "Bad id."}, 400)
                return self._json(item_details(wid))
            except BadKey:
                return self._json({"error": "badkey"}, 401)
            except Exception as e:
                return self._json({"error": "Couldn't reach Steam: %s" % e}, 502)
        if u.path == "/api/workshop/list":
            return self._json({"items": set_list(body.get("items", []))})
        if u.path == "/api/workshop/check":
            try:
                return self._json(workshop_status())
            except Exception as e:
                return self._json({"error": "Couldn't reach Steam: %s" % e}, 502)
        if u.path == "/api/config-save":
            try:
                data = write_config(body.get("name", ""), body.get("changes", []),
                                    body.get("mtime"), bool(body.get("force")))
                return self._json({"ok": True, "config": data})
            except ChangedOnDisk:
                return self._json({"conflict": True}, 409)
            except (FileNotFoundError, ValueError) as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if u.path == "/api/open-cfg":
            try:
                reveal(str(cfg_path(body.get("name", ""))))
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if u.path == "/api/rules":
            rules = body.get("rules")
            if not isinstance(rules, dict):
                return self._json({"error": "Bad rules payload."}, 400)
            SETTINGS["sort_rules"] = rules
            save_settings(SETTINGS)
            return self._json({"ok": True})

        if u.path == "/api/seen":
            acknowledge_updates()
            return self._json({"ok": True})

        if u.path == "/api/open":
            try:
                target = body.get("path", "")
                if target and not inside_allowed(Path(target)):
                    return self._json({"error": "That's outside your mod folders."}, 400)
                reveal(target)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if u.path == "/api/open-bepinex":
            try:
                bep = effective_paths().get("bepinex_dir", "")
                if not bep:
                    return self._json({"error": "No BepInEx\\config folder found in the Elin install."}, 404)
                reveal(bep)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if u.path == "/api/open-config":
            try:
                reveal(effective_paths()["config_dir"])
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if u.path == "/api/open-steam":
            wid = str(body.get("id", "")).strip()
            if not wid.isdigit():
                return self._json({"error": "That mod has no workshop id."}, 400)
            try:
                # the same link RimPy and RimSort use: Steam opens the item page in its own client
                open_uri("steam://url/CommunityFilePage/%s" % wid)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": "Couldn't reach Steam: %s" % e}, 500)

        if u.path == "/api/launch":
            try:
                launch_game()
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if u.path == "/api/quit":
            threading.Thread(target=lambda: (time.sleep(0.4), HTTPD.shutdown()), daemon=True).start()
            return self._json({"ok": True})

        return self._send(404, "{}", "application/json")


def acknowledge_updates():
    times = workshop_times(effective_paths()["workshop_dir"])
    if times:
        SETTINGS["seen_updates"] = dict(times)
        save_settings(SETTINGS)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
HTTPD = None


def main():
    global HTTPD
    HTTPD = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = HTTPD.server_address[1]
    url = "http://127.0.0.1:%d/?token=%s" % (port, TOKEN)

    paths = effective_paths()
    print("Emmin - Eternal Mod Manager of Nefia")
    print("-" * 46)
    print("Elin folder : %s" % (paths["config_dir"] or "not found - set it in the app"))
    print("workshop    : %s" % (paths["workshop_dir"] or "not found - set it in the app"))
    print("local mods  : %s" % (paths["local_dir"] or "not found - set it in the app"))
    print("loadorder   : %s" % ("found" if (loadorder_file() and loadorder_file().is_file())
                                else "NOT FOUND - check the folder above"))
    print("-" * 46)
    print("Open in your browser if it didn't open by itself:")
    print("  %s" % url)
    print("\nLeave this window open while you work. Ctrl+C here, or Quit in the app, shuts it down.")

    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        HTTPD.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        HTTPD.server_close()
        print("\nClosed.")


if __name__ == "__main__":
    main()
