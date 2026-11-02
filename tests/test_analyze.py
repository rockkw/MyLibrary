import pytest

from conftest import tree_digest
from mylibrary import analyze, tree
from test_pipeline import add_files, run_to_plan
from test_tree import flat
from test_web import client  # noqa: F401


def build(lib, **kw):
    return analyze.build(lib["con"], lib["cfg"], **kw)


def word(data, name):
    return next((w for w in data["words"] if w["word"] == name), None)


def test_tokens_are_letter_runs_of_three_or_more():
    assert analyze.tokens("The_Matrix.1999.x265-GRP ab Beyoncé") == {"the", "matrix", "grp", "beyoncé"}


def test_a_word_counts_once_per_file(lib):
    add_files(lib, "Zebra/zebra.zebra.mkv", "Zebra/zebra2.mkv")
    assert word(build(lib), "zebra")["count"] == 2


def test_a_word_needs_two_files(lib):
    add_files(lib, "only/onceonly.mkv")
    assert word(build(lib), "onceonly") is None
    add_files(lib, "only/onceonly2.mkv")
    assert word(build(lib), "onceonly")


def test_noise_is_removed(lib):
    add_files(lib, "Movies/Shows/Music/Software/Adult/Season 01/a.XXX.x265.mkv",
              "Movies/Shows/Music/Software/Adult/Season 01/b.XXX.x265.mkv")
    found = {w["word"] for w in build(lib)["words"]}
    assert not found & {"movies", "shows", "music", "software", "adult", "season", "xxx", "mkv",
                        "the", "bluray", "srt"}


def test_folders_count_so_artists_are_found(lib):
    add_files(lib, "Music/Gamma Band/Album/01 - One.mp3", "Music/Gamma Band/Album/02 - Two.mp3")
    assert word(build(lib), "gamma")["count"] == 2


def test_the_root_path_does_not_count(lib):
    add_files(lib, "a/x1.mkv", "a/x2.mkv")
    assert word(build(lib), lib["root"].name) is None


@pytest.mark.parametrize("name", ["the_quokka_a.mkv", "the.quokka.b.mkv", "THE-QUOKKA c.mkv"])
def test_separators_do_not_hide_a_word(lib, name):
    add_files(lib, name, "quokka.other.mkv")
    w = word(build(lib), "quokka")
    assert len(w["paths"]) == w["count"] == 2


def test_every_keyword_lists_exactly_the_files_it_counted(lib):
    add_files(lib, "Some_Long_Name.mkv", "Some.Long.Name.2.mkv")
    run_to_plan(lib)
    on_disk = {n["path"] for n in analyze.files_in(tree.build(lib["con"], lib["cfg"])["roots"][0])}
    for w in build(lib)["words"]:
        assert len(set(w["paths"])) == w["count"]
        assert set(w["paths"]) <= on_disk


def test_at_most_eighty_words_most_common_first(lib):
    names = [f"w{chr(97 + i // 26)}{chr(97 + i % 26)}q" for i in range(100)]
    for i, name in enumerate(names):
        add_files(lib, *[f"{name}{n}.mkv" for n in range(10 if i == 50 else 2)])
    words = build(lib)["words"]
    assert len(words) == analyze.MAX_WORDS
    assert words[0]["word"] == names[50]
    assert [w["count"] for w in words] == sorted((w["count"] for w in words), reverse=True)


def test_planned_arrivals_and_hidden_files_are_not_counted(lib):
    add_files(lib, "._ghost.mkv", "._ghost2.mkv")
    run_to_plan(lib)
    data = build(lib)
    built = tree.build(lib["con"], lib["cfg"])
    assert built["moves"] > 0
    arrivals = {p for (p, s) in flat(built["roots"][0]) if s == "in"}
    assert arrivals and not arrivals & {p for w in data["words"] for p in w["paths"]}
    assert word(data, "ghost") is None


def test_totals_match_the_tree(lib):
    add_files(lib, "a/song.mp3", "a/tool.exe")
    run_to_plan(lib)
    data, built = build(lib), tree.build(lib["con"], lib["cfg"])
    assert {t["label"].lower(): t["count"] for t in data["by_type"]} == built["totals"]
    assert data["total"] == sum(built["totals"].values())
    assert sum(s["count"] for s in data["by_state"]) == data["total"]
    assert {s["label"]: s["count"] for s in data["by_state"]}["Moving"] == built["moves"]


def test_average_size(lib):
    (lib["root"] / "walrus1.mkv").write_bytes(b"x" * 100)
    (lib["root"] / "walrus2.mkv").write_bytes(b"x" * 300)
    assert word(build(lib), "walrus")["avg_size"] == 200


def test_several_roots_and_the_root_filter(lib, tmp_path):
    other = tmp_path / "second"
    other.mkdir()
    (other / "okapi one.mkv").write_bytes(b"x")
    (other / "okapi two.mkv").write_bytes(b"x")
    add_files(lib, "okapi three.mkv")
    lib["cfg"].roots.append(other)
    assert word(build(lib), "okapi")["count"] == 3
    only = build(lib, root=str(other))
    assert word(only, "okapi")["count"] == 2 and only["total"] == 2
    assert only["roots"] == [str(lib["root"]), str(other)]


def test_missing_root_and_empty_library(lib, tmp_path):
    lib["cfg"].roots.clear()
    lib["cfg"].roots.append(tmp_path / "gone")
    data = build(lib)
    assert data["missing"] == [str(tmp_path / "gone")]
    assert data["words"] == [] and data["total"] == 0 and data["by_state"] == []


def test_building_changes_nothing(lib):
    run_to_plan(lib)
    before = tree_digest(lib["root"])
    paths = sorted(str(p) for p in lib["root"].rglob("*"))
    proposals = lib["con"].execute("SELECT id, status FROM proposals ORDER BY id").fetchall()
    build(lib)
    assert tree_digest(lib["root"]) == before
    assert sorted(str(p) for p in lib["root"].rglob("*")) == paths
    assert lib["con"].execute("SELECT id, status FROM proposals ORDER BY id").fetchall() == proposals


def test_endpoint_is_read_only(client, lib):  # noqa: F811
    d = client.get(f"/api/analyze?root={lib['root']}").get_json()
    assert d["root"] == str(lib["root"]) and d["total"] > 0
    assert client.post("/api/analyze").status_code == 405


def test_tab_sits_after_tree(client):  # noqa: F811
    page = client.get("/").get_data(as_text=True)
    assert page.index('data-page="tree"') < page.index('data-page="analyze"') < page.index('data-page="review"')
    assert 'id="analyze-root"' in page
