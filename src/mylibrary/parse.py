"""Step 2: deterministic name parsing with guessit, plus a confidence score.

Anything below the auto-approve threshold is flagged for the LLM step.
Manual (human) identifications are never overwritten.
"""

import json
import os
import re
import string
from collections import Counter, defaultdict
from datetime import date, timedelta

from guessit import guessit

from .config import has_ext
from .db import now

GENERIC = {"movie", "video", "film", "final", "cut", "untitled", "new", "copy",
           "output", "export", "clip", "vts", "title", "main", "feature", "disc"}

# guessit's bare edition names, spelled out for the file name.
EDITION_LABELS = {"Collector": "Collector's Edition", "Deluxe": "Deluxe Edition",
                  "Extended": "Extended Edition", "Fan": "Fan Edit",
                  "Limited": "Limited Edition", "Special": "Special Edition",
                  "Theatrical": "Theatrical Cut", "Ultimate": "Ultimate Edition"}
# guessit reports "Final Cut", "Redux" etc. as an alternative title, not an edition.
ALT_EDITION = re.compile(r"\b(cut|edition|version)$|^redux$", re.I)


def _guess(text: str, media_type: str = "movie") -> dict:
    try:
        g = dict(guessit(text, {"type": media_type}))
    except Exception:  # guessit can choke on odd strings
        return {}
    out = {}
    for k, v in g.items():
        if k in ("edition", "alternative_title", "episode_title") and isinstance(v, list):
            out[k] = [str(x) for x in v]
        elif k in ("season", "episode") and isinstance(v, list):
            out[k] = [x for x in v if isinstance(x, int)]
        else:
            out[k] = v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
    return out


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v] if v else []


def edition_of(g: dict) -> str | None:
    labels = [EDITION_LABELS.get(e, e) for e in _as_list(g.get("edition"))]
    labels += [string.capwords(a) for a in _as_list(g.get("alternative_title"))
               if ALT_EDITION.search(a)]
    return " - ".join(dict.fromkeys(labels)) or None


def full_title(g: dict, name: str) -> str:
    """guessit splits "Spider-Man - Brand New Day (2026)" at the dash; put the subtitle back.

    Only text before the year counts as title. Edition words are handled by edition_of.
    """
    title = (g.get("title") or "").strip()
    year_at = name.find(str(g.get("year"))) if g.get("year") else -1
    for alt in _as_list(g.get("alternative_title")):
        if not ALT_EDITION.search(alt) and -1 < name.find(alt) < year_at:
            title = f"{title} {alt}"
    return title


def _numbers(v) -> list[int]:
    return sorted(x for x in (v if isinstance(v, list) else [v]) if isinstance(x, int))


def identify_episode(path: str, reasons: list) -> dict:
    """Identify a file already known to carry a season or episode marker.

    The title is the series name. Episodes skip the LLM: a model that names movies
    adds nothing to S01E04, and it could turn a good parse into a wrong one. Anything
    uncertain goes straight to human review.
    """
    name = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(path))
    g = _guess(name, "episode")
    if not g.get("title"):  # Series/Season 01/S01E04.mkv
        parts = os.path.normpath(path).split(os.sep)[-3:]
        g = {**_guess(os.path.join(*parts), "episode"), **g}
        reasons.append("series title taken from folder name")

    title = (g.get("title") or "").strip()
    folder_title = (_guess(parent, "episode").get("title") or "").strip() if parent else ""
    if folder_title.casefold() == title.casefold():  # agatha.all.along.s01e04.mkv
        title = folder_title
    if title.islower():
        title = string.capwords(title)
    if title and g.get("country"):  # The Office (US) is not The Office (UK)
        title = f"{title} ({g['country']})"

    seasons, episodes = _numbers(g.get("season")), _numbers(g.get("episode"))
    conf = 0.95
    if len(seasons) != 1 or not episodes:
        conf = 0.5
        reasons.append("no season or episode number")
    elif len(episodes) > 1:
        conf = 0.7
        reasons.append("multi-episode file")
    if not title:
        conf = 0.0
        reasons.append("no usable title")
    numbered = len(seasons) == 1 and episodes
    return {
        "title": title or None,
        "year": g.get("year") if isinstance(g.get("year"), int) else None,
        "edition": None,
        "kind": "episode",
        "season": seasons[0] if numbered else None,
        "episode": episodes[0] if numbered else None,
        "episode_last": episodes[-1] if numbered and len(episodes) > 1 else None,
        "episode_title": " ".join(_as_list(g.get("episode_title"))) or None,
        "confidence": conf,
        "flagged": 0,
        "reasons": "; ".join(reasons),
        "guessit": g,
    }


def identify(path: str, duration: float | None, cfg) -> dict:
    """Return {title, year, edition, kind, confidence, flagged, reasons, guessit}."""
    name = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(path))
    g_file = _guess(name)
    g_dir = _guess(parent) if parent else {}
    reasons = []

    is_sample = (any(re.search(rf"\b{re.escape(w)}\b", name.lower()) for w in cfg.ignore_words)
                 or "Sample" in str(g_file.get("other")))
    if not is_sample and (g_file.get("episode") is not None or g_file.get("season") is not None):
        return identify_episode(path, reasons)

    g = g_file
    # A clean parent folder ("Alien (1979)") often beats a messy filename.
    if not g_file.get("year") and g_dir.get("year") and g_dir.get("title"):
        g = {**g_file, "title": g_dir["title"], "year": g_dir["year"]}
        reasons.append("title/year taken from folder name")

    title = full_title(g_file, name) if g is g_file else (g.get("title") or "").strip()
    year = g.get("year") if isinstance(g.get("year"), int) else None
    edition = edition_of(g_file)
    # Only a folder naming this one film, so "Directors Cuts/" doesn't label everything in it.
    if not edition and g_dir.get("title") and year and g_dir.get("year") == year:
        edition = edition_of(g_dir)
    kind = "movie"
    conf = 0.95

    if is_sample:
        kind, reasons = "sample", reasons + ["matches ignore word"]
    if duration and duration < cfg.min_duration_min * 60 and kind == "movie":
        kind, conf = "extra", conf - 0.3
        reasons.append(f"short ({duration/60:.0f} min)")
    if not year:
        conf -= 0.35
        reasons.append("no year")
    # "01.mp4" may be one part of several, so a folder's title is not proof of a whole film.
    if g is not g_file and os.path.splitext(name)[0].isdigit():
        conf -= 0.2
        reasons.append("numbered file")
    words = re.findall(r"[a-z]+", title.lower())
    if not title or len(title) < 2 or title.isdigit():
        conf -= 0.5
        reasons.append("no usable title")
    elif words and all(w in GENERIC for w in words):
        conf -= 0.5
        reasons.append("generic title")
    if year and not (1888 <= year <= 2100):
        conf -= 0.4
        reasons.append("implausible year")

    conf = max(0.0, min(conf, 0.99))
    return {
        "title": title or None,
        "year": year,
        "edition": edition,
        "kind": kind,
        "confidence": round(conf, 2),
        "flagged": int(conf < cfg.auto_approve and kind == "movie"),
        "reasons": "; ".join(reasons),
        "guessit": g_file,
    }


PLACEHOLDER = re.compile(r"(unknown( artist| album| title)?|untitled|no title|track\s*\d*|various)$", re.I)
INSTALLERS = ("exe", "msi", "deb", "rpm", "dmg", "pkg", "appimage")
VERSION = r"v?\d+(?:\.\d+)+"
NOISE = (r"x64|x86|x86[-_]64|amd64|arm64|aarch64|win32|win64|setup|install|installer|portable"
         r"|linux|windows|macos|mac|universal|full|final|build")
TITLE_END = re.compile(rf"(?<=.)(?:[-_. ])({VERSION}|{NOISE})(?=$|[-_. ])", re.I)
GENERIC_STEM = re.compile(r"(setup|install|installer|autorun|start|update|data\d*)$", re.I)
SOFTWARE_WORDS = re.compile(rf"(?:^|[-_. ])({VERSION}|x64|x86|amd64|arm64|win32|win64|setup|install"
                            r"|installer|windows|linux|server|desktop|ubuntu|debian|fedora|kali)"
                            r"(?=$|[-_. ])", re.I)
MULTI_PART = re.compile(r"\.part\d+\.rar$|\.7z\.\d+$", re.I)


def _tag_text(tags: dict, *keys) -> str | None:
    for key in keys:
        value = str(tags.get(key) or "").strip()
        if value and not PLACEHOLDER.fullmatch(value):
            return value
    return None


def _tag_number(tags: dict, *keys) -> tuple[int | None, int | None]:
    """Parse "3/12" into (3, 12). ID3 and Vorbis tags both use this form."""
    for key in keys:
        m = re.fullmatch(r"\s*(\d+)\s*(?:/\s*(\d+))?\s*", str(tags.get(key) or ""))
        if m:
            return int(m[1]), int(m[2]) if m[2] else None
    return None, None


def identify_music(tags: dict) -> dict:
    """Identify a track from embedded tags. The title is the track title.

    Filenames are not trusted here. Tracks skip the LLM: a wrong artist is worse than
    a file left for review. Album artist wins so a guest verse stays in the same folder.
    """
    artist = _tag_text(tags, *ALBUM_ARTIST_KEYS)
    if not artist and str(tags.get("compilation") or "").lower() in ("1", "true", "yes"):
        artist = VARIOUS
    artist = artist or _tag_text(tags, "artist")
    album, title = _tag_text(tags, "album"), _tag_text(tags, "title")
    track, _ = _tag_number(tags, "track", "tracknumber")
    disc, discs = _tag_number(tags, "disc", "discnumber")
    year = re.search(r"\b(\d{4})\b", str(tags.get("date") or tags.get("year") or ""))
    missing = [n for n, v in (("artist", artist), ("album", album), ("title", title)) if not v]
    reasons = []
    conf = 0.95
    if not track:
        conf = 0.7
        reasons.append("no track number")
    if missing:
        conf = 0.5
        reasons.insert(0, "missing tags: " + ", ".join(missing))
    return {
        "title": title, "year": int(year[1]) if year else None, "edition": None, "kind": "music",
        "artist": artist, "album": album, "track": track,
        "disc": disc if (discs or 0) > 1 or (disc or 0) > 1 else None,
        "confidence": conf, "flagged": 0, "reasons": "; ".join(reasons), "guessit": {},
    }


VARIOUS = "Various Artists"
DISC_FOLDER = re.compile(r"(?:cd|dis[ck])[\s._-]*(\d+)$", re.I)
PLACED_DISC = re.compile(r"(\d+)-\d+ - ")
GUEST = re.compile(r"\s*(?:[(\[]?\b(?:feat|ft|featuring|with)\b\.?|[,&;/+]|\sx\s|\sand\s).*$", re.I)
ALBUM_ARTIST_KEYS = ("album_artist", "albumartist", "album artist")


def _primary_artist(artist: str) -> str:
    return GUEST.sub("", artist or "").strip().casefold()


def _is_compilation(rows: list, cfg) -> bool:
    """True when an album's tracks have no shared artist.

    An album artist on every track wins, but a partial or mixed one proves nothing: the
    Billboard set in the first real library has 34 untagged tracks beside per-track ones.
    Guests do not count, so a solo album with features stays with its artist.
    """
    placed = os.path.join(cfg.music_dir, VARIOUS) + os.sep
    if any(placed in r["path"] for r in rows):
        return True
    shared = {_tag_text(json.loads(r["tags"] or "{}"), *ALBUM_ARTIST_KEYS) for r in rows}
    if len(shared) == 1 and None not in shared:
        return False
    artists = Counter(_primary_artist(r["artist"]) for r in rows if r["artist"])
    return len(artists) >= 3 and artists.most_common(1)[0][1] <= len(rows) / 2


def _main_artist(rows: list) -> str | None:
    """The artist behind most tracks, in its plainest spelling, or None if no one leads."""
    named = [r["artist"] for r in rows if r["artist"]]
    if not named:
        return None
    top, count = Counter(map(_primary_artist, named)).most_common(1)[0]
    return min((a for a in named if _primary_artist(a) == top), key=len) \
        if count > len(rows) / 2 else None


def _disc_of(row, folders_per_disc: int, cfg) -> int | None:
    folder = DISC_FOLDER.search(os.path.basename(os.path.dirname(row["path"])))
    if folder and folders_per_disc > 1:
        return int(folder[1])
    placed = PLACED_DISC.match(os.path.basename(row["path"]))
    in_library = f"{os.sep}{cfg.music_dir}{os.sep}" in row["path"]
    return int(placed[1]) if placed and in_library else None


def group_albums(con, cfg) -> int:
    """Settle what one track cannot show alone: compilations and disc folders.

    Runs over already scanned tracks after each parse. A release is a folder, with
    CD1 and DISC 2 style folders counted as one, so same-named albums stay apart.
    """
    releases = defaultdict(list)
    for r in con.execute("""SELECT f.id, f.path, f.tags, p.artist, p.album, p.disc FROM parses p
                            JOIN files f ON f.id=p.file_id WHERE f.missing=0 AND p.kind='music'
                            AND p.source='guessit' AND p.album IS NOT NULL"""):
        folder = os.path.dirname(r["path"])
        releases[os.path.dirname(folder) if DISC_FOLDER.search(folder) else folder].append(r)
    changed = 0
    for tracks in releases.values():
        names = Counter(r["album"] for r in tracks).most_common()
        albums = defaultdict(list)
        for r in tracks:  # a cut-off tag belongs with the full album name
            albums[next((n for n, _ in names if n.startswith(r["album"])), r["album"])].append(r)
        for album, rows in albums.items():
            main = VARIOUS if _is_compilation(rows, cfg) else _main_artist(rows)
            folders = len({os.path.dirname(r["path"]) for r in rows})
            for r in rows:
                artist = main or r["artist"]
                disc = r["disc"] or _disc_of(r, folders, cfg)
                if (artist, album, disc) != (r["artist"], r["album"], r["disc"]):
                    con.execute("UPDATE parses SET artist=?, album=?, disc=? WHERE file_id=?",
                                (artist, album, disc, r["id"]))
                    changed += 1
    return changed


SCENE_DOTTED = re.compile(r"^([a-z0-9][\w-]*)\.(?:(\d{2})\.(\d{2})\.(\d{2})|((?:19|20)\d{2}))(?=\.)"
                          r"(.*?)\.*(?:\.xxx\b|\.\d{3,4}p\b|$)", re.I)
SCENE_MARK = re.compile(r"\bxxx\b", re.I)
SCENE_DATE = r"\((\d{2})\.(\d{2})\.(\d{2}|\d{4})\)"
SCENE_BRACKET = re.compile(rf"^\[([^\]]+)\]\s*(.+?)\s*{SCENE_DATE}", re.I)
SCENE_DASHED = re.compile(rf"^(\S+) - (.+?)\s*{SCENE_DATE}", re.I)
SCENE_PLACED = re.compile(r"^(.+?) - (\d{4}-\d{2}-\d{2})(?: - (.+?))??(?: - (.+?))?(?: - \d{3,4}p)?$")
STAGE_NAME = re.compile(r"\d|[a-z][A-Z]")


def _real_date(y: int, m: int, d: int) -> str | None:
    """An ISO date, or None if impossible or still to come. A guess never passes as a date."""
    try:
        when = date(y, m, d)
    except ValueError:
        return None
    return when.isoformat() if when <= date.today() + timedelta(days=1) else None


def _bracket_date(a: str, b: str, c: str) -> str | None:
    """(17.05.2026) is day first, (26.10.16) is year first, in every real file so far."""
    return _real_date(int(c), int(b), int(a)) if len(c) == 4 else _real_date(2000 + int(a), int(b), int(c))


def _people_and_title(words: list[str]) -> tuple[str | None, str | None]:
    """Split "Kat Perfect And Ann Lee New Girls" with no separator to go on.

    Names are usually two words, joined by And. A wrong guess moves a dash, not any words.
    """
    prefix = []
    if len(words) > 1 and words[0].casefold() == "episode" and words[1].isdigit():
        prefix, words = words[:2], words[2:]
    people, i = [], 0
    while i < len(words):
        size = 1 if STAGE_NAME.search(words[i]) or i + 1 == len(words) else 2
        people.append(" ".join(words[i:i + size]))
        i += size
        if i < len(words) and words[i].casefold() == "and":
            i += 1
        elif not (i + 2 < len(words) and words[i + 2].casefold() == "and"):
            break  # "Ann Lee Kim Roe And Sue Poe" lists three people
    return ", ".join(people) or None, " ".join(prefix + words[i:]) or None


def _spoken(text: str) -> tuple[str | None, str | None]:
    """Split the part of a named scene that follows the site."""
    text = text.strip(" -")
    if " - " in text:
        people, title = text.split(" - ", 1)
        return people.strip(), title.strip() or None
    if "," in text:
        return text, None
    return _people_and_title(text.split())


def _titled(text: str | None) -> str | None:
    return string.capwords(text) if text and text.islower() else text


def scene_from_name(name: str) -> dict | None:
    """Read a file stem or folder name. Returns the scene fields, or None if it is not one."""
    m = SCENE_DOTTED.match(name)
    if m and SCENE_MARK.search(name):
        when = _real_date(2000 + int(m[2]), int(m[3]), int(m[4])) if m[2] else None
        people, title = _people_and_title([w for w in m[6].split(".") if w])
        return {"site": m[1], "scene_date": when, "performers": people, "title": title,
                "year": int(when[:4]) if when else int(m[5]) if m[5] else None, "marked": True}
    m = SCENE_BRACKET.match(name) or SCENE_DASHED.match(name)
    if m:
        when = _bracket_date(m[3], m[4], m[5])
        people, title = _spoken(m[2])
        return {"site": m[1].strip(), "scene_date": when, "performers": people, "title": title,
                "year": int(when[:4]) if when else None, "marked": False}
    return None


def scene_placed(stem: str) -> dict | None:
    m = SCENE_PLACED.match(stem)
    return m and {"site": m[1], "scene_date": m[2], "performers": m[3] or m[4],
                  "title": m[4] if m[3] else None, "year": int(m[2][:4]), "marked": True}


def identify_scene(path: str, origin: str | None, cfg) -> dict | None:
    """Identify a scene from its original name. The release folder spells the site best."""
    name = os.path.basename(path)
    if any(re.search(rf"\b{re.escape(w)}\b", name.lower()) for w in cfg.ignore_words):
        return None
    if f"{os.sep}{cfg.adult_dir}{os.sep}" in path and not origin:
        placed = scene_placed(os.path.splitext(name)[0])
        if placed:
            return {**placed, "kind": "scene", "confidence": 0.95, "flagged": 0, "reasons": ""}
    start = origin or path
    own = scene_from_name(os.path.splitext(os.path.basename(start))[0])
    folder = scene_from_name(os.path.basename(os.path.dirname(start)))
    found = own or folder
    if folder and (not own or own["scene_date"] == folder["scene_date"]):
        found = folder
    if not found:
        return None
    found = {**found, "site": found["site"] if not found["site"].islower()
             else string.capwords(found["site"]), "title": _titled(found["title"]),
             "performers": _titled(found["performers"])}
    reasons = []
    conf = 0.95
    if not found["marked"]:
        conf = 0.8
        reasons.append("no XXX marker: check it is a scene")
    if not found["scene_date"]:
        conf = 0.6
        reasons.append("no scene date")
    if not (found["performers"] or found["title"]):
        conf = 0.5
        reasons.append("no performer or title")
    return {**found, "kind": "scene", "confidence": conf, "flagged": 0, "reasons": "; ".join(reasons)}


def find_scenes(con, cfg) -> int:
    """Recognise scenes among scanned files, from their original names.

    Runs after each parse so LLM answers and files already moved need no --redo. Only a
    person's edit outranks it, and the original name comes from the first recorded move.
    """
    rows = con.execute("""SELECT f.id, f.path, p.kind, p.site, p.scene_date, p.performers, p.title,
                                 p.confidence,
                                 (SELECT m.src FROM moves m WHERE m.file_id=f.id AND m.is_sidecar=0
                                  AND m.status='moved' ORDER BY m.id LIMIT 1) AS origin
                          FROM files f JOIN parses p ON p.file_id=f.id
                          WHERE f.missing=0 AND p.source<>'manual'
                          AND p.kind NOT IN ('music','software')""").fetchall()
    changed = 0
    for r in rows:
        s = identify_scene(r["path"], r["origin"], cfg)
        if not s or all(s[k] == r[k] for k in ("kind", "site", "scene_date", "performers",
                                               "title", "confidence")):
            continue
        con.execute("""UPDATE parses SET kind='scene', title=?, year=?, edition=NULL, season=NULL,
                       episode=NULL, episode_last=NULL, episode_title=NULL, site=?, scene_date=?,
                       performers=?, source='guessit', confidence=?, flagged=0, reasons=?,
                       llm_json=NULL, imdb_id=NULL, updated_at=? WHERE file_id=?""",
                    (s["title"], s["year"], s["site"], s["scene_date"], s["performers"],
                     s["confidence"], s["reasons"], now(), r["id"]))
        changed += 1
    return changed


def _software_title(stem: str) -> tuple[str, bool]:
    """Return (product name, whether a dotted version followed it)."""
    m = TITLE_END.search(stem)
    title = re.sub(r"[-_. ]+", " ", stem[:m.start()] if m else stem).strip()
    if title.islower():
        title = string.capwords(title)
    return title, bool(m and re.fullmatch(VERSION, m[1], re.I))


def _archive_stem(name: str) -> str:
    stem = re.sub(r"\.(tar\.\w+|part\d+\.rar|7z\.\d+)$", "", name, flags=re.I)
    return stem if stem != name else os.path.splitext(name)[0]


def identify_software(path: str, root: str, cfg, reasons: list | None = None) -> dict:
    """Identify software from its name. Nothing here reads the file.

    Only an installer with a clear name and version can pass without a human, so a
    stray setup.exe or an archive that could hold anything always gets looked at.
    """
    name = os.path.basename(path)
    parent = os.path.dirname(path)
    reasons = list(reasons or [])
    title, versioned = _software_title(_archive_stem(name))
    # Already in Software/Title/: the folder is the answer, so a rescan changes nothing.
    if os.path.dirname(parent) == os.path.join(root, cfg.software_dir):
        title, conf = os.path.basename(parent), 0.95
    else:
        if not title or GENERIC_STEM.fullmatch(title):
            folder = os.path.basename(parent) if parent != root else ""
            title = _software_title(folder)[0]
            versioned = False
            reasons.append("title taken from folder name" if title else "no usable title")
        installer = name.rsplit(".", 1)[-1].lower() in INSTALLERS
        conf = 0.92 if installer and versioned and not reasons else 0.6
        if not installer:
            reasons.append("contents unknown")
        if MULTI_PART.search(name):
            reasons.append("multi-part archive: check every part moves together")
    return {
        "title": title or None, "year": None, "edition": None, "kind": "software",
        "confidence": conf, "flagged": int(conf < cfg.auto_approve),
        "reasons": "; ".join(reasons), "guessit": {},
    }


def iso_is_software(path: str, width) -> bool | None:
    """True or False when the signs agree, None when an ISO is unclear.

    Film ISOs carry a year, source or resolution; software ones a version or OS name.
    """
    name = os.path.basename(path)
    g = _guess(name)
    video = bool(width or g.get("year") or g.get("screen_size") or g.get("source"))
    software = bool(SOFTWARE_WORDS.search(os.path.splitext(name)[0]))
    return None if video == software else software


def identify_file(r, cfg) -> dict:
    """Pick the identifier for one files row."""
    path = r["path"]
    if has_ext(path, cfg.audio_ext):
        return identify_music(json.loads(r["tags"] or "{}"))
    if has_ext(path, cfg.software_ext) and not has_ext(path, cfg.video_ext):
        return identify_software(path, r["root"], cfg)
    if path.lower().endswith(".iso"):
        verdict = iso_is_software(path, r["width"])
        if verdict is not False:
            return identify_software(path, r["root"], cfg, None if verdict else
                                     ["unclear ISO: video or software"])
    return identify(path, r["duration"], cfg)


def parse_all(con, cfg, redo=False, log=print) -> dict:
    rows = con.execute("""
        SELECT f.id, f.path, f.root, f.duration, f.width, f.tags, p.source, p.updated_at,
               f.scanned_at
        FROM files f LEFT JOIN parses p ON p.file_id = f.id
        WHERE f.missing = 0""").fetchall()
    stats = {"parsed": 0, "flagged": 0, "kept_manual": 0, "kept_llm": 0}
    for r in rows:
        if r["source"] == "manual":
            stats["kept_manual"] += 1
            continue
        if r["source"] == "llm" and not redo and (r["updated_at"] or "") >= (r["scanned_at"] or ""):
            stats["kept_llm"] += 1
            continue
        if r["source"] == "guessit" and not redo and (r["updated_at"] or "") >= (r["scanned_at"] or ""):
            continue
        info = identify_file(r, cfg)
        con.execute("""
            INSERT INTO parses (file_id, title, year, edition, kind, season, episode, episode_last,
                                episode_title, artist, album, track, disc, source, confidence,
                                flagged, reasons, guessit_json, llm_json, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'guessit', ?,?,?,?, NULL, ?)
            ON CONFLICT(file_id) DO UPDATE SET title=excluded.title, year=excluded.year,
                edition=excluded.edition, kind=excluded.kind, season=excluded.season,
                episode=excluded.episode, episode_last=excluded.episode_last,
                episode_title=excluded.episode_title, artist=excluded.artist, album=excluded.album,
                track=excluded.track, disc=excluded.disc, source='guessit', confidence=excluded.confidence,
                flagged=excluded.flagged, reasons=excluded.reasons,
                guessit_json=excluded.guessit_json, llm_json=NULL, imdb_id=NULL,
                updated_at=excluded.updated_at
        """, (r["id"], info["title"], info["year"], info["edition"], info["kind"],
              info.get("season"), info.get("episode"), info.get("episode_last"),
              info.get("episode_title"), info.get("artist"), info.get("album"),
              info.get("track"), info.get("disc"), info["confidence"],
              info["flagged"], info["reasons"], json.dumps(info["guessit"]), now()))
        stats["parsed"] += 1
        stats["flagged"] += info["flagged"]
    stats["album_updates"] = group_albums(con, cfg)
    stats["scenes"] = find_scenes(con, cfg)
    con.commit()
    log(f"parsed {stats['parsed']} files, {stats['flagged']} flagged for the LLM, "
        f"{stats['album_updates']} tracks grouped into albums, {stats['scenes']} scenes updated")
    return stats
