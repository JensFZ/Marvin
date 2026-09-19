"""Tests fuer den H265-Konverter.

    pytest                     alles
    pytest -m "not ffmpeg"     nur die reine Logik, ohne ffmpeg und ohne Kodieren

Die mit @pytest.mark.ffmpeg markierten Tests kodieren echtes (kurzes) Material und
brauchen ffmpeg/ffprobe im PATH.
"""
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import h265gui
from h265gui import (ALT_DIR, build_cmd, convert_one, discard, human_dur, human_size,
                     is_network, move_to_alt, probe, remove_original, saving_text,
                     target_for, verify, worth_keeping)

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


def test_build_cmd_erzwingt_mp4_muxer_bei_m4v():
    """Ohne -f mp4 waehlt ffmpeg bei .m4v den ipod-Muxer, der kein HEVC kann."""
    cmd = build_cmd(Path("a.m4v"), Path("b.m4v"), "libx265", 24, leer())
    assert cmd[cmd.index("-f") + 1] == "mp4"


def test_build_cmd_erzwingt_muxer_nur_bei_m4v():
    for ziel in ("b.mp4", "b.mkv", "b.mov"):
        assert "-f" not in build_cmd(Path("a.mkv"), Path(ziel), "libx265", 24, leer())


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


def test_remove_original_loescht_endgueltig(tmp_path, monkeypatch):
    """permanent=True: weder Papierkorb noch _alt."""
    gerufen = []
    monkeypatch.setattr(h265gui, "send2trash", lambda p: gerufen.append(p))
    monkeypatch.setattr(h265gui, "is_network", lambda p: False)
    src = tmp_path / "film.mkv"
    src.write_text("x")
    meldung = remove_original(src, permanent=True)
    assert not src.exists()
    assert gerufen == [], "Papierkorb haette nicht benutzt werden duerfen"
    assert not (tmp_path / ALT_DIR).exists()
    assert "endgueltig" in meldung


def test_remove_original_endgueltig_schlaegt_netzlaufwerk(tmp_path, monkeypatch):
    """Auch auf Netzlaufwerken gilt der ausdrueckliche Loeschwunsch, kein _alt."""
    monkeypatch.setattr(h265gui, "is_network", lambda p: True)
    src = tmp_path / "film.mkv"
    src.write_text("x")
    remove_original(src, permanent=True)
    assert not src.exists()
    assert not (tmp_path / ALT_DIR).exists()


def test_remove_original_ist_ohne_angabe_schonend(tmp_path, monkeypatch):
    """Voreinstellung darf niemals endgueltig loeschen."""
    monkeypatch.setattr(h265gui, "is_network", lambda p: True)
    src = tmp_path / "film.mkv"
    src.write_text("x")
    remove_original(src)
    assert (tmp_path / ALT_DIR / "film.mkv").exists()


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
def test_m4v_wird_konvertiert(tmp_path):
    """Regression: .m4v landete beim ipod-Muxer und scheiterte am HEVC-Header."""
    src = tmp_path / "folge.m4v"
    ff("-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
       "-f", "lavfi", "-i", "sine=duration=2",
       "-c:v", "libx264", "-c:a", "aac", str(src))
    info = probe(src)
    assert info["codec"] == "h264"
    dst = target_for(src)
    assert dst.suffix == ".m4v", "Endung soll erhalten bleiben"
    ok, msg = convert_one(src, dst, "libx265", 30, info)
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

def run_selfcheck(args):
    """Startet das Skript in einem eigenen Prozess. Ob eine Anzeige da ist, zeigt sich dort.

    Bewusst kein tk.Tk() im Pytest-Prozess als Vorpruefung: mehrere Tk-Wurzeln nacheinander
    sind hier unzuverlaessig ("Can't find a usable tk.tcl") und wuerden auch den
    Smoke-Test unten zufaellig ueberspringen lassen.
    """
    r = subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=60)
    if "TclError" in r.stderr:
        pytest.skip(f"keine Anzeige verfuegbar: {r.stderr.strip().splitlines()[-1]}")
    return r


@needs_ffmpeg
def test_selfcheck_startet_und_beendet_sich():
    """--selfcheck ist der Startcheck der Release-Exe; hier gegen das Skript."""
    r = run_selfcheck([h265gui.__file__, "--selfcheck"])
    if h265gui.send2trash is not None:
        assert r.returncode == 0, (r.returncode, r.stderr)
    else:
        assert r.returncode == 3, (r.returncode, r.stderr)


@needs_ffmpeg
def test_selfcheck_schlaegt_fehl_ohne_send2trash():
    """Eine Exe ohne send2trash wuerde Originale still endgueltig loeschen."""
    code = ("import sys, runpy; sys.modules['send2trash'] = None; "
            "sys.argv = ['h265gui.py', '--selfcheck']; "
            f"runpy.run_path({h265gui.__file__!r}, run_name='__main__')")
    r = run_selfcheck(["-c", code])
    assert r.returncode == 3, (r.returncode, r.stderr)


@pytest.fixture(scope="module")
def tk_root():
    """Genau ein tk.Tk() pro Testlauf - mehrere sind hier unzuverlaessig (siehe oben)."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"keine Anzeige verfuegbar: {e}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tk_root, monkeypatch):
    """Frische App in einem eigenen Toplevel. Ohne Dialoge, die einen Test blockieren koennten."""
    import tkinter as tk
    monkeypatch.setattr(h265gui.messagebox, "showerror", lambda *a, **k: None)
    win = tk.Toplevel(tk_root)
    win.withdraw()
    yield h265gui.App(win)
    win.destroy()


def drain(app):
    """Verarbeitet die Queue wie pump() - nur synchron und ohne mainloop."""
    while not app.q.empty():
        kind, payload = app.q.get()
        getattr(app, "_on_" + kind)(payload)


def logtext(app):
    return app.log.get("1.0", "end")


def test_gui_baut_sich_auf_und_scannt(app, tmp_path):
    """Smoke-Test: Fenster aufbauen, Ordner scannen, HEVC-Dateien ausgrauen."""
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
    # Endgueltiges Loeschen muss man bewusst einschalten
    assert app.permanent.get() is False


# ----------------------------------------------------------------- Platz-Schutz

@pytest.mark.parametrize("alt, neu, erwartet", [
    (1000, 500, True),      # deutlich kleiner
    (1000, 900, True),      # 10 % gespart
    (1000, 949, True),      # knapp ueber der Schwelle
    (1000, 951, False),     # knapp darunter
    (1000, 1000, False),    # gleich gross
    (1000, 1200, False),    # groesser geworden
])
def test_worth_keeping(alt, neu, erwartet):
    assert worth_keeping(alt, neu) is erwartet


def test_worth_keeping_liest_schwelle_beim_aufruf(monkeypatch):
    monkeypatch.setattr(h265gui, "MIN_SAVING", 0.5)
    assert worth_keeping(1000, 600) is False
    assert worth_keeping(1000, 400) is True


@pytest.mark.parametrize("alt, neu, erwartet", [
    (1000, 620, "-38 %"),
    (1000, 1000, "0 %"),
    (1000, 1040, "+4 %"),
    (1000, 999, "0 %"),        # unter einem halben Prozent rundet auf 0
    (0, 100, ""),              # keine Division durch null
])
def test_saving_text(alt, neu, erwartet):
    assert saving_text(alt, neu) == erwartet


def test_finished_zeigt_summe_nur_wenn_gespart(app):
    app.saved = 5 * 1024 ** 3
    app._on_finished(None)
    assert "5.0 GB gespart" in app.file_lbl.cget("text")
    app.saved = 0
    app._on_finished(None)
    assert app.file_lbl.cget("text") == "fertig"


def batch_setup(app, tmp_path):
    """Legt ein kurzes h264-Video an und traegt es wie nach einem Scan in die Liste ein."""
    src = tmp_path / "folge.mkv"
    ff("-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
       "-c:v", "libx264", str(src))
    app.folder = tmp_path
    app._on_row((src, probe(src), src.stat().st_size))
    return src


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_zu_grosses_ergebnis_laesst_original_liegen(app, tmp_path, monkeypatch):
    src = batch_setup(app, tmp_path)
    monkeypatch.setattr(h265gui, "MIN_SAVING", 0.99)   # jedes Ergebnis ist "zu gross"
    app.run_batch([str(src)], "libx265", 30, permanent=True)
    drain(app)
    assert src.exists(), "Original wurde trotz zu geringer Ersparnis entfernt"
    assert not (tmp_path / "folge.h265.mkv").exists(), "HEVC-Datei nicht verworfen"
    assert app.tree.item(str(src), "tags") == ("kept",)
    assert "BEHALTEN folge.mkv" in logtext(app)
    assert app.saved == 0


@pytest.mark.ffmpeg
@needs_ffmpeg
def test_ausreichende_ersparnis_ersetzt_original(app, tmp_path, monkeypatch):
    src = batch_setup(app, tmp_path)
    alt_size = src.stat().st_size
    monkeypatch.setattr(h265gui, "MIN_SAVING", -100.0)  # jedes Ergebnis ist "klein genug"
    app.run_batch([str(src)], "libx265", 30, permanent=True)
    drain(app)
    neu = tmp_path / "folge.h265.mkv"
    assert neu.exists() and not src.exists()
    assert app.tree.item(str(neu), "tags") == ("done",)
    assert app.tree.set(str(neu), "saved") == saving_text(alt_size, neu.stat().st_size)
    assert app.saved == alt_size - neu.stat().st_size
    assert "OK folge.mkv" in logtext(app)
