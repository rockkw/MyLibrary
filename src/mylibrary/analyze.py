"""Keywords across the library: MyMagnet's Analyze page for files on disk. Read only.

Everything comes from tree.build, so the numbers are the Tree tab's numbers. Each keyword lists
its own files, so what a click shows is exactly what was counted.
"""

import os
import re
from collections import defaultdict

from . import tree

MIN_FILES = 2
MAX_WORDS = 80
WORD = re.compile(r"[^\W\d_]{3,}")  # letters only, so x265 and 1080p fall away
STOPWORDS = {
    "the", "and", "for", "with", "from", "was", "are", "been", "has", "have", "that", "this",
    "not", "but", "did", "its", "you", "your", "all", "out", "how", "when", "who", "what",
    "feat", "edition", "version",
    "aac", "amzn", "atmos", "avc", "bdrip", "bit", "bluray", "brrip", "com", "ddp", "divx", "dts",
    "dvd", "dvdrip", "extended", "flac", "hdr", "hdrip", "hdtv", "hevc", "internal", "mkv", "mp",
    "org", "net", "proper", "rarbg", "remastered", "remux", "repack", "sdr", "theatrical",
    "truehd", "unrated", "web", "webrip", "www", "xvid", "xxx", "yify", "yts",
    "season",
}
STATES = (("out", "Moving"), ("decide", "Needs a decision"), ("downloading", "Still downloading"),
          ("untracked", "Not tracked"), ("skipped", "Skipped"), ("ignored", "Ignored"),
          ("", "No change"))


def tokens(text: str) -> set:
    return {w.lower() for w in WORD.findall(text)}


def noise(cfg) -> set:
    """Words that say nothing about a file: the stopwords, extensions and layout folders."""
    folders = (cfg.movies_dir, cfg.tv_dir, cfg.music_dir, cfg.software_dir, cfg.adult_dir)
    return (STOPWORDS | cfg.video_ext | cfg.audio_ext | cfg.software_ext | cfg.sidecar_ext
            | tokens(" ".join(folders)))


def keywords_of(path: str, root: str, skip: set) -> set:
    """The words in a file's path below its root, so artist and album folders count."""
    return tokens(os.path.splitext(os.path.relpath(path, root))[0]) - skip


def files_in(node: dict):
    """Every file on disk under a node. Planned arrivals are not files yet."""
    for c in node["children"]:
        if c["type"] == "dir":
            yield from files_in(c)
        elif c["state"] != "in":
            yield c


def build(con, cfg, root: str = "") -> dict:
    data = tree.build(con, cfg)
    roots = [r for r in data["roots"] if not root or r["path"] == root]
    skip = noise(cfg)
    found, sizes, states = defaultdict(list), defaultdict(int), defaultdict(int)
    for r in roots:
        for f in files_in(r):
            states[f["state"]] += 1
            for word in keywords_of(f["path"], r["path"], skip):
                found[word].append(f["path"])
                sizes[word] += f["size"]
    words = [{"word": w, "count": len(paths), "avg_size": round(sizes[w] / len(paths)),
              "paths": sorted(paths)}
             for w, paths in found.items() if len(paths) >= MIN_FILES]
    words.sort(key=lambda w: (-w["count"], w["word"]))
    return {"root": root, "roots": [r["path"] for r in data["roots"]], "missing": data["missing"],
            "error": data["error"], "words": words[:MAX_WORDS],
            "total": sum(states.values()),
            "by_type": [{"label": g.capitalize(), "count": sum(r["counts"][g] for r in roots)}
                        for g in tree.GROUPS],
            "by_state": [{"label": label, "count": states[key]} for key, label in STATES
                         if states[key]]}
