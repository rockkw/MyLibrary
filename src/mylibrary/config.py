"""config.ini loader with ${VAR} / ${VAR:-default} expansion (same style as MyMagnet)."""

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config.ini"


def expand(value: str) -> str:
    """Expand ${VAR}, ${VAR:-default} (defaults may nest) and $VAR."""
    out, i = [], 0
    while i < len(value):
        if value.startswith("${", i):
            depth, j = 1, i + 2
            while j < len(value) and depth:
                if value.startswith("${", j):
                    depth += 1
                    j += 2
                    continue
                if value[j] == "}":
                    depth -= 1
                j += 1
            inner = value[i + 2 : j - 1]
            if ":-" in inner:
                name, default = inner.split(":-", 1)
                env = os.environ.get(name)
                out.append(env if env else expand(default))
            else:
                out.append(os.environ.get(inner, ""))
            i = j
        else:
            out.append(value[i])
            i += 1
    return os.path.expandvars("".join(out))


def _list(raw: str) -> list[str]:
    items = []
    for line in raw.replace(",", "\n").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", ";")):
            items.append(expand(line))
    return items


TV_LAYOUT = "{title}{year}/Season {season}/{title}{year} - S{season}E{episode}{episode_title}{quality}"
MUSIC_LAYOUT = "{artist}/{album}/{track}{title}"
SOFTWARE_LAYOUT = "{title}/{filename}"
AUDIO_EXT = "mp3,flac,m4a,aac,ogg,opus,wav,wma,aiff,ape"
SOFTWARE_EXT = "exe,msi,deb,rpm,dmg,pkg,appimage,zip,rar,7z,tgz,tar.gz,tar.xz,tar.bz2"


def has_ext(name: str, extensions: set[str]) -> bool:
    """Match the last suffix, or the last two so "tar.gz" works."""
    parts = name.lower().split(".")
    return len(parts) > 1 and (parts[-1] in extensions or ".".join(parts[-2:]) in extensions)


@dataclass
class Config:
    db_file: Path
    log_dir: Path
    roots: list[Path]
    movies_dir: str = "Movies"
    layout: str = "{title} ({year})/{title} ({year}){edition}{quality}"
    tv_dir: str = "Shows"
    tv_layout: str = TV_LAYOUT
    music_dir: str = "Music"
    music_layout: str = MUSIC_LAYOUT
    software_dir: str = "Software"
    software_layout: str = SOFTWARE_LAYOUT
    video_ext: set[str] = field(default_factory=set)
    audio_ext: set[str] = field(default_factory=set)
    software_ext: set[str] = field(default_factory=set)
    sidecar_ext: set[str] = field(default_factory=set)
    ignore_words: list[str] = field(default_factory=list)
    min_duration_min: float = 40.0
    settle_minutes: float = 10.0
    ollama_enabled: bool = True
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "mistral"
    ollama_timeout: int = 120
    imdb_db: Path | None = None
    auto_approve: float = 0.90
    llm_max_conf: float = 0.85
    host: str = "127.0.0.1"
    port: int = 5051
    source: Path | None = None


def load(path: str | os.PathLike | None = None) -> Config:
    if not path:
        path = os.environ.get("MYLIBRARY_CONFIG") or (
            "config.ini" if Path("config.ini").exists() else REPO_CONFIG)
    path = Path(path).resolve()
    cp = configparser.ConfigParser(interpolation=None)
    if path.exists():
        cp.read(path)

    def get(sec, key, default=""):
        return expand(cp.get(sec, key, fallback=default)).strip()

    imdb = get("imdb", "db_file")
    return Config(
        db_file=Path(get("paths", "db_file", "${HOME}/.mylibrary/inventory.db")),
        log_dir=Path(get("paths", "log_dir", "${HOME}/.mylibrary/logs")),
        roots=[Path(p) for p in _list(cp.get("library", "roots", fallback=""))],
        movies_dir=get("library", "movies_dir", "Movies"),
        layout=get("library", "layout", "{title} ({year})/{title} ({year}){edition}{quality}"),
        tv_dir=get("library", "tv_dir", "Shows"),
        tv_layout=get("library", "tv_layout", TV_LAYOUT),
        music_dir=get("library", "music_dir", "Music"),
        music_layout=get("library", "music_layout", MUSIC_LAYOUT),
        software_dir=get("library", "software_dir", "Software"),
        software_layout=get("library", "software_layout", SOFTWARE_LAYOUT),
        audio_ext={e.lower().lstrip(".") for e in _list(cp.get(
            "library", "audio_extensions", fallback=AUDIO_EXT))},
        software_ext={e.lower().lstrip(".") for e in _list(cp.get(
            "library", "software_extensions", fallback=SOFTWARE_EXT))},
        video_ext={e.lower().lstrip(".") for e in _list(cp.get(
            "library", "video_extensions",
            fallback="mkv,mp4,m4v,avi,mov,wmv,mpg,mpeg,ts,m2ts,webm,iso"))},
        sidecar_ext={e.lower().lstrip(".") for e in _list(cp.get(
            "library", "sidecar_extensions", fallback="srt,sub,idx,ass,ssa,vtt,nfo"))},
        ignore_words=[w.lower() for w in _list(cp.get(
            "library", "ignore_words", fallback="sample,trailer"))],
        min_duration_min=float(get("library", "min_duration_minutes", "40")),
        settle_minutes=float(get("library", "settle_minutes", "10")),
        ollama_enabled=get("ollama", "enabled", "true").lower() in ("1", "true", "yes"),
        ollama_url=get("ollama", "url", "http://localhost:11434").rstrip("/"),
        ollama_model=get("ollama", "model", "mistral"),
        ollama_timeout=int(get("ollama", "timeout", "120")),
        imdb_db=Path(imdb) if imdb else None,
        auto_approve=float(get("thresholds", "auto_approve", "0.90")),
        llm_max_conf=float(get("thresholds", "llm_max_confidence", "0.85")),
        host=get("server", "host", "127.0.0.1"),
        port=int(get("server", "port", "5051")),
        source=path if path.exists() else None,
    )
