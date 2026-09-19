"""Tests fuer den H265-Konverter.

    pytest                     alles
    pytest -m "not ffmpeg"     nur die reine Logik, ohne ffmpeg und ohne Kodieren

Die mit @pytest.mark.ffmpeg markierten Tests kodieren echtes (kurzes) Material und
brauchen ffmpeg/ffprobe im PATH.
"""
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import h265gui
from h265gui import (ALT_DIR, build_cmd, convert_one, discard, human_dur, human_size,
                     is_network, move_to_alt, probe, remove_original, target_for, verify)

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe nicht im PATH")


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                   check=True, creationflags=h265gui.NO_WINDOW)


@pytest.fixture(scope="session")
def sample(tmp_path_factory):
    """Kurzes h264-Video mit Tonspur."""
    p = tmp_path_factory.mktemp("sample") / "clip_h264.mkv"
    ff("-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25",
       "-f", "lavfi", "-i", "sine=duration=3",
       "-c:v", "libx264", "-c:a", "aac", str(p))
    return p


# ------------------------------------------------------------- Zielcontainer

@pytest.mark.parametrize("quelle, ziel", [
    ("film.mkv", "film.h265.mkv"),
    ("film.mp4", "film.h265.mp4"),
    ("film.m4v", "film.h265.m4v"),
    ("film.mov", "film.h265.mov"),
    ("film.ts", "film.h265.ts"),
    # Container ohne HEVC-Unterstuetzung landen in mkv
    ("film.avi", "film.h265.mkv"),
    ("film.wmv", "film.h265.mkv"),
    ("film.flv", "film.h265.mkv"),
    ("film.mpg", "film.h265.mkv"),
    ("film.webm", "film.h265.mkv"),
    # Gross-/Kleinschreibung der Endung bleibt erhalten
    ("film.MP4", "film.h265.MP4"),
    ("film.AVI", "film.h265.mkv"),
    # Punkte im Namen duerfen nicht stoeren
    ("Die.Serie.S01E01.mkv", "Die.Serie.S01E01.h265.mkv"),
])
def test_target_for(quelle, ziel):
    assert target_for(Path("/x") / quelle).name == ziel


def test_target_for_bleibt_im_selben_ordner():
    src = Path("/videos/Serie/Staffel1/E01.mp4")
    assert target_for(src).parent == src.parent


# --------------------------------------------------------------- ffmpeg-Aufruf

def leer():
    return {"codec": "h264", "dur": 10.0, "subs": []}


def test_build_cmd_nvenc_nutzt_cq():
    cmd = build_cmd(Path("a.mkv"), Path("b.mkv"), "hevc_nvenc", 24, leer())
    assert "hevc_nvenc" in cmd
    assert cmd[cmd.index("-cq") + 1] == "24"
    # ohne -b:v 0 ignoriert NVENC den CQ-Wert
    assert cmd[cmd.index("-b:v") + 1] == "0"


def test_build_cmd_libx265_nutzt_crf():
    cmd = build_cmd(Path("a.mkv"), Path("b.mkv"), "libx265", 18, leer())
    assert "libx265" in cmd
    assert cmd[cmd.index("-crf") + 1] == "18"


def test_build_cmd_kopiert_audio_immer():
    cmd = build_cmd(Path("a.mkv"), Path("b.mkv"), "libx265", 24, leer())
    assert cmd[cmd.index("-c:a") + 1] == "copy"


def test_build_cmd_meldet_fortschritt():
    cmd = build_cmd(Path("a.mkv"), Path("b.mkv"), "libx265", 24, leer())
    assert "-progress" in cmd and "pipe:1" in cmd


def test_build_cmd_mkv_kopiert_untertitel_und_anhaenge():
    info = {"codec": "h264", "dur": 1.0, "subs": ["subrip"]}
    cmd = build_cmd(Path("a.mkv"), Path("b.mkv"), "libx265", 24, info)
    assert cmd[cmd.index("-c:s") + 1] == "copy"
    assert "0:t?" in cmd          # Schriftarten/Anhaenge


def test_build_cmd_mp4_wandelt_textuntertitel():
    info = {"codec": "h264", "dur": 1.0, "subs": ["subrip"]}
    cmd = build_cmd(Path("a.mp4"), Path("b.mp4"), "libx265", 24, info)
    assert cmd[cmd.index("-c:s") + 1] == "mov_text"


def test_build_cmd_mp4_laesst_bilduntertitel_weg():
    # PGS passt nicht in mp4 - lieber weglassen als die Konvertierung scheitern lassen
    info = {"codec": "h264", "dur": 1.0, "subs": ["hdmv_pgs_subtitle"]}
    cmd = build_cmd(Path("a.mp4"), Path("b.mp4"), "libx265", 24, info)
    assert "-c:s" not in cmd
    assert "0:s?" not in cmd


def test_build_cmd_setzt_hvc1_nur_bei_mp4():
    info = leer()
    assert "-tag:v" in build_cmd(Path("a.mp4"), Path("b.mp4"), "libx265", 24, info)
    assert "-tag:v" not in build_cmd(Path("a.mkv"), Path("b.mkv"), "libx265", 24, info)


# ------------------------------------------------------------------- Anzeige

@pytest.mark.parametrize("n, erwartet", [
    (0, "0 B"), (512, "512 B"), (1024, "1.0 KB"),
    (1536, "1.5 KB"), (1024 ** 2, "1.0 MB"), (int(2.5 * 1024 ** 3), "2.5 GB"),
])
def test_human_size(n, erwartet):
    assert human_size(n) == erwartet


@pytest.mark.parametrize("s, erwartet", [
    (0, "?"), (-1, "?"), (5, "0:00:05"), (65, "0:01:05"),
    (3600, "1:00:00"), (5025, "1:23:45"),
])
def test_human_dur(s, erwartet):
    assert human_dur(s) == erwartet


# ------------------------------------------------------- Originale wegraeumen

def test_move_to_alt_verschiebt_und_legt_ordner_an(tmp_path):
    src = tmp_path / "film.mkv"
    src.write_text("inhalt")
    ziel = move_to_alt(src)
    assert ziel == tmp_path / ALT_DIR / "film.mkv"
    assert not src.exists()
    assert ziel.read_text() == "inhalt"


def test_move_to_alt_umgeht_namenskollision(tmp_path):
    for inhalt in ("erste", "zweite", "dritte"):
        src = tmp_path / "film.mkv"
        src.write_text(inhalt)
        move_to_alt(src)
    alt = tmp_path / ALT_DIR
    assert sorted(p.name for p in alt.iterdir()) == ["film.mkv", "film_1.mkv", "film_2.mkv"]
    assert (alt / "film.mkv").read_text() == "erste"
    assert (alt / "film_2.mkv").read_text() == "dritte"


def test_move_to_alt_ist_pro_ordner(tmp_path):
    """Gleicher Dateiname in zwei Ordnern darf sich nicht in die Quere kommen."""
    a, b = tmp_path / "S01", tmp_path / "S02"
    a.mkdir(), b.mkdir()
    (a / "E01.mkv").write_text("staffel1")
    (b / "E01.mkv").write_text("staffel2")
    move_to_alt(a / "E01.mkv")
    move_to_alt(b / "E01.mkv")
    assert (a / ALT_DIR / "E01.mkv").read_text() == "staffel1"
    assert (b / ALT_DIR / "E01.mkv").read_text() == "staffel2"


def test_remove_original_nutzt_alt_bei_netzlaufwerk(tmp_path, monkeypatch):
    monkeypatch.setattr(h265gui, "is_network", lambda p: True)
    src = tmp_path / "film.mkv"
    src.write_text("x")
    meldung = remove_original(src)
    assert not src.exists()
    assert (tmp_path / ALT_DIR / "film.mkv").exists()
    assert ALT_DIR in meldung


def test_remove_original_loescht_lokal(tmp_path, monkeypatch):
    monkeypatch.setattr(h265gui, "is_network", lambda p: False)
    monkeypatch.setattr(h265gui, "send2trash", None)   # Papierkorb im Test umgehen
    src = tmp_path / "film.mkv"
    src.write_text("x")
    remove_original(src)
    assert not src.exists()


def test_is_network_lokal(tmp_path):
    assert is_network(tmp_path) is False


def test_discard_vertraegt_fehlende_datei(tmp_path):
    assert discard(tmp_path / "gibtsnicht.mkv") is True


# ------------------------------------------------------------------ Pruefung

def test_verify_meldet_fehlende_datei(tmp_path):
    assert verify(tmp_path / "weg.mkv", 10.0)


def test_verify_meldet_leere_datei(tmp_path):
    p = tmp_path / "leer.mkv"
    p.touch()
    assert verify(p, 10.0)


def test_probe_bei_unlesbarer_datei(tmp_path):
    p = tmp_path / "kaputt.mp4"
    p.write_text("kein video")
    info = probe(p)
    assert info["codec"] == "?"
    assert info["dur"] == 0.0


# ------------------------------------------------------- echtes Kodieren

@pytest.mark.ffmpeg
@needs_ffmpeg
def test_probe_liest_codec_und_dauer(sample):
    info = probe(sample)
    assert info["codec"] == "h264"
    assert 2.5 < info["dur"] < 3.5


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_konvertierung_erzeugt_hevc_und_behaelt_audio(sample, tmp_path):
    dst = tmp_path / "out.mkv"
    fortschritt = []
    ok, msg = convert_one(sample, dst, "libx265", 30, probe(sample),
                          on_progress=fortschritt.append)
    assert ok, msg
    assert fortschritt and max(fortschritt) > 0, "kein Fortschritt gemeldet"
    out = probe(dst)
    assert out["codec"] == "hevc"
    assert abs(out["dur"] - probe(sample)["dur"]) < 0.5
    codecs = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
         "-of", "csv=p=0", str(dst)], capture_output=True, text=True,
        creationflags=h265gui.NO_WINDOW).stdout
    assert "aac" in codecs, f"Tonspur verloren: {codecs!r}"


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_avi_wird_nach_mkv_konvertiert(tmp_path):
    """avi kann kein HEVC - ffmpeg schreibt sonst klaglos rawvideo hinein."""
    src = tmp_path / "alt.avi"
    ff("-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
       "-c:v", "mpeg4", str(src))
    assert probe(src)["codec"] == "mpeg4"
    dst = target_for(src)
    assert dst.suffix == ".mkv"
    ok, msg = convert_one(src, dst, "libx265", 30, probe(src))
    assert ok, msg
    assert probe(dst)["codec"] == "hevc"


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_untertitel_bleiben_in_mkv(tmp_path):
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHallo\n")
    src = tmp_path / "mit_subs.mkv"
    ff("-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
       "-i", str(srt), "-map", "0:v", "-map", "1:s",
       "-c:v", "libx264", "-c:s", "srt", str(src))
    info = probe(src)
    assert info["subs"] == ["subrip"]
    dst = tmp_path / "out.mkv"
    ok, msg = convert_one(src, dst, "libx265", 30, info)
    assert ok, msg
    assert probe(dst)["subs"] == ["subrip"]


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_abbruch_raeumt_teildatei_weg(sample, tmp_path):
    dst = tmp_path / "abgebrochen.mkv"
    cancel = threading.Event()
    cancel.set()            # sofort abbrechen, damit der Test nicht vom Tempo abhaengt
    ok, msg = convert_one(sample, dst, "libx265", 30, probe(sample), cancel=cancel)
    assert not ok
    assert msg == "abgebrochen"
    assert not dst.exists(), "Teil-Datei nicht aufgeraeumt"


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_kaputte_eingabe_hinterlaesst_nichts(tmp_path):
    src = tmp_path / "kaputt.mp4"
    src.write_text("kein video")
    dst = target_for(src)
    ok, msg = convert_one(src, dst, "libx265", 30, probe(src))
    assert not ok and msg
    assert not dst.exists()
    assert src.exists(), "Original wurde angefasst"


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_verify_erkennt_zu_kurze_ausgabe(sample, tmp_path):
    """Faengt Faelle ab, in denen ffmpeg trotz abgeschnittener Ausgabe 0 zurueckgibt."""
    dst = tmp_path / "out.mkv"
    ok, _ = convert_one(sample, dst, "libx265", 30, probe(sample))
    assert ok
    assert verify(dst, src_dur=600.0), "zu kurze Datei haette auffallen muessen"
    assert not verify(dst, src_dur=probe(sample)["dur"])


# ----------------------------------------------------------------------- GUI

def test_gui_baut_sich_auf_und_scannt(tmp_path):
    """Smoke-Test: Fenster aufbauen, Ordner scannen, HEVC-Dateien ausgrauen."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"keine Anzeige verfuegbar: {e}")
    try:
        app = h265gui.App(root)
        # Scan ohne Thread, damit der Test deterministisch bleibt
        (tmp_path / "a.mkv").write_text("x")
        (tmp_path / ALT_DIR).mkdir()
        (tmp_path / ALT_DIR / "weggeraeumt.mkv").write_text("x")
        app.folder = tmp_path
        app.scan()
        eintraege = []
        while not app.q.empty():
            art, last = app.q.get()
            if art == "row":
                eintraege.append(last[0].name)
        assert eintraege == ["a.mkv"], f"_alt haette uebersprungen werden muessen: {eintraege}"
    finally:
        root.destroy()
