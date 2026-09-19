#!/usr/bin/env python3
"""H265-Konverter: Ordner scannen, Codecs anzeigen, ausgewaehlte Dateien per ffmpeg nach HEVC umkodieren.

Start:  python h265gui.py
Test:   pytest
"""
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".webm",
             ".mpg", ".mpeg", ".ts", ".m2ts"}
TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}
# Container, die HEVC aufnehmen koennen. Alles andere (avi, wmv, flv, mpg, webm)
# wird nach mkv geschrieben - avi schluckt den Stream sonst als 'rawvideo'.
HEVC_OK = {".mkv", ".mp4", ".m4v", ".mov", ".ts", ".m2ts"}
ALT_DIR = "_alt"        # Ablage fuer Originale auf Netzlaufwerken (dort gibt es keinen Papierkorb)
MIN_SAVING = 0.05       # Original wird nur ersetzt, wenn HEVC mindestens so viel kleiner ist
DRIVE_REMOTE = 4
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None


# ---------------------------------------------------------------- ffmpeg-Teil

def probe(path):
    """{'codec','dur','subs'} - codec '?' wenn ffprobe die Datei nicht lesen kann."""
    info = {"codec": "?", "dur": 0.0, "subs": []}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type,codec_name:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
        d = json.loads(r.stdout or "{}")
    except Exception:
        return info
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and info["codec"] == "?":
            info["codec"] = s.get("codec_name", "?")
        elif s.get("codec_type") == "subtitle":
            info["subs"].append(s.get("codec_name", ""))
    try:
        info["dur"] = float(d.get("format", {}).get("duration") or 0)
    except ValueError:
        pass
    return info


def discard(path):
    """Teil-/Fehlerdatei loeschen. Nach terminate() haelt Windows das Handle noch kurz,
    deshalb ein paar Versuche statt eines sofortigen PermissionError."""
    for _ in range(15):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            time.sleep(0.2)
    return False


def target_for(src):
    """Zielpfad <name>.h265<ext>. Container ohne HEVC-Unterstuetzung werden zu mkv."""
    ext = src.suffix if src.suffix.lower() in HEVC_OK else ".mkv"
    return src.with_name(f"{src.stem}.h265{ext}")


def build_cmd(src, dst, encoder, quality, info):
    ext = dst.suffix.lower()
    mp4ish = ext in {".mp4", ".m4v", ".mov"}
    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(src),
           "-map", "0:v:0", "-map", "0:a?"]
    if info["subs"]:
        if ext == ".mkv":
            cmd += ["-map", "0:s?", "-map", "0:t?", "-c:s", "copy"]
        elif mp4ish and all(s in TEXT_SUBS for s in info["subs"]):
            cmd += ["-map", "0:s?", "-c:s", "mov_text"]
        # sonst: Untertitel weglassen, der Zielcontainer kann sie nicht aufnehmen
    if encoder == "hevc_nvenc":
        cmd += ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr",
                "-cq", str(quality), "-b:v", "0"]
    else:
        cmd += ["-c:v", "libx265", "-preset", "medium", "-crf", str(quality)]
    cmd += ["-c:a", "copy"]
    if mp4ish:
        cmd += ["-tag:v", "hvc1"]
    if ext == ".m4v":
        # Anhand der Endung waehlt ffmpeg sonst den ipod-Muxer, der kein HEVC kennt
        # ("Could not find tag for codec hevc in stream #0").
        cmd += ["-f", "mp4"]
    cmd += ["-progress", "pipe:1", "-nostats", str(dst)]
    return cmd


def convert_one(src, dst, encoder, quality, info, on_progress=None,
                cancel=None, proc_sink=None):
    """Kodiert src nach dst. Gibt (ok, meldung) zurueck. dst wird bei Fehler entfernt."""
    cmd = build_cmd(src, dst, encoder, quality, info)
    dur = info["dur"]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                             stdin=subprocess.DEVNULL, text=True, bufsize=1,
                             creationflags=NO_WINDOW)
        if proc_sink:
            proc_sink(p)
        for line in p.stdout:
            if cancel is not None and cancel.is_set():
                p.terminate()
                break
            if on_progress and dur > 0 and line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                on_progress(min(100.0, us / 10000.0 / dur))
        p.wait()
        errf.seek(0)
        err = errf.read().strip()

    if cancel is not None and cancel.is_set():
        discard(dst)
        return False, "abgebrochen"
    if p.returncode != 0:
        discard(dst)
        return False, err or f"ffmpeg beendet mit Code {p.returncode}"

    problem = verify(dst, dur)
    if problem:
        discard(dst)
        return False, problem
    return True, ""


def verify(dst, src_dur):
    """Leere Meldung = Zieldatei ist in Ordnung."""
    if not dst.exists() or dst.stat().st_size == 0:
        return "Zieldatei fehlt oder ist leer"
    out = probe(dst)
    if out["codec"] != "hevc":
        return f"Zieldatei hat Codec {out['codec']}, erwartet hevc"
    if src_dur > 0 and abs(out["dur"] - src_dur) > max(1.0, src_dur * 0.01):
        return f"Laufzeit weicht ab: {out['dur']:.1f}s statt {src_dur:.1f}s"
    return ""


def is_network(path):
    """True fuer UNC-Pfade und verbundene Netzlaufwerke - dort gibt es keinen Papierkorb."""
    if os.name != "nt":
        return False
    try:
        p = str(Path(path).resolve())
    except OSError:
        p = str(path)
    if p.startswith("\\\\"):
        return True
    drive = os.path.splitdrive(p)[0]
    if not drive:
        return False
    return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE


def move_to_alt(src):
    """Original nach <ordner>/_alt/ verschieben. Gibt den neuen Pfad zurueck."""
    alt = src.parent / ALT_DIR
    alt.mkdir(exist_ok=True)
    target = alt / src.name
    n = 1
    while target.exists():
        target = alt / f"{src.stem}_{n}{src.suffix}"
        n += 1
    shutil.move(str(src), str(target))
    return target


def remove_original(src, permanent=False):
    """Original wegraeumen. Gibt Hinweistext zurueck.

    permanent=True loescht sofort und unwiderruflich - weder Papierkorb noch _alt.
    Sonst: Netzlaufwerk -> nach _alt/ verschieben, weil Windows dort am Papierkorb
    vorbei endgueltig loescht. Lokal -> Papierkorb (bzw. loeschen, falls send2trash
    fehlt).
    """
    if permanent:
        os.remove(src)
        return "Original endgueltig geloescht"
    if is_network(src):
        target = move_to_alt(src)
        return f"Netzlaufwerk: Original nach {ALT_DIR}/{target.name} verschoben"
    if send2trash:
        send2trash(str(src))
        return "Original in den Papierkorb verschoben"
    os.remove(src)
    return "Original geloescht (kein Papierkorb, send2trash fehlt)"


# -------------------------------------------------------------------- Helfer

def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_dur(s):
    if s <= 0:
        return "?"
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def worth_keeping(src_size, dst_size):
    """True, wenn das HEVC-Ergebnis klein genug ist, um das Original zu ersetzen.

    Bei bereits gut komprimierten Quellen kann HEVC groesser werden; dann waere der Tausch
    Platzverlust plus Risiko fuer nichts. Gelesen wird MIN_SAVING erst beim Aufruf.
    """
    return dst_size <= src_size * (1 - MIN_SAVING)


def saving_text(old_size, new_size):
    """Ersparnis fuer die Liste, z.B. '-38 %'. Positiv ('+4 %'), falls doch groesser."""
    if old_size <= 0:
        return ""
    pct = round((new_size / old_size - 1) * 100)
    return f"{pct:+d} %" if pct else "0 %"


# ----------------------------------------------------------------------- GUI

class App:
    def __init__(self, root):
        self.root = root
        root.title("H265-Konverter")
        root.geometry("980x640")
        self.q = queue.Queue()
        self.info = {}          # iid (= Pfad als str) -> probe-dict
        self.folder = None
        self.cancel = threading.Event()
        self.proc = None
        self.busy = False
        self.saved = 0          # Bytes, die der laufende Batch gespart hat

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="Ordner waehlen...", command=self.pick_folder).pack(side="left")
        self.folder_lbl = ttk.Label(top, text="kein Ordner gewaehlt")
        self.folder_lbl.pack(side="left", padx=10)

        bar = ttk.Frame(root, padding=(8, 0))
        bar.pack(fill="x")
        ttk.Label(bar, text="Encoder:").pack(side="left")
        self.encoder = tk.StringVar(value="hevc_nvenc")
        ttk.Combobox(bar, textvariable=self.encoder, width=12, state="readonly",
                     values=["hevc_nvenc", "libx265"]).pack(side="left", padx=(4, 12))
        ttk.Label(bar, text="Qualitaet (CQ/CRF):").pack(side="left")
        self.quality = tk.IntVar(value=24)
        ttk.Spinbox(bar, from_=14, to=35, textvariable=self.quality,
                    width=4).pack(side="left", padx=(4, 12))
        ttk.Button(bar, text="Alle nicht-HEVC waehlen",
                   command=self.select_convertible).pack(side="left", padx=(0, 12))
        self.permanent = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Originale endgueltig loeschen",
                        variable=self.permanent).pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="Abbrechen", state="disabled",
                                     command=self.on_cancel)
        self.cancel_btn.pack(side="right")
        self.start_btn = ttk.Button(bar, text="Konvertieren", command=self.on_start)
        self.start_btn.pack(side="right", padx=6)

        self.tree = ttk.Treeview(root, columns=("codec", "size", "dur", "saved"),
                                 selectmode="extended")
        self.tree.heading("#0", text="Datei")
        self.tree.column("#0", width=480)
        for c, t, w in (("codec", "Codec", 90), ("size", "Groesse", 100),
                        ("dur", "Laufzeit", 90), ("saved", "Ersparnis", 80)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="e")
        self.tree.tag_configure("already", foreground="#909090")
        self.tree.tag_configure("failed", foreground="#c00000")
        self.tree.tag_configure("kept", foreground="#b36b00")
        self.tree.tag_configure("done", foreground="#008000")
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)

        prog = ttk.Frame(root, padding=(8, 0))
        prog.pack(fill="x")
        self.file_lbl = ttk.Label(prog, text="bereit")
        self.file_lbl.pack(anchor="w")
        self.file_bar = ttk.Progressbar(prog, maximum=100)
        self.file_bar.pack(fill="x")
        self.total_lbl = ttk.Label(prog, text="")
        self.total_lbl.pack(anchor="w")
        self.total_bar = ttk.Progressbar(prog, maximum=100)
        self.total_bar.pack(fill="x", pady=(0, 6))

        self.log = tk.Text(root, height=7, state="disabled", wrap="word")
        self.log.pack(fill="x", padx=8, pady=(0, 8))

        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            messagebox.showerror("ffmpeg fehlt",
                                 "ffmpeg und ffprobe wurden im PATH nicht gefunden.")
        if send2trash is None:
            self.write_log("Hinweis: send2trash nicht installiert - Originale werden "
                           "endgueltig geloescht statt in den Papierkorb verschoben. "
                           "Abhilfe: pip install send2trash")
        root.after(100, self.pump)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # -- Ausgabe

    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def pump(self):
        """Einzige Stelle, an der Worker die GUI erreichen - tkinter ist nicht threadsafe."""
        try:
            while True:
                kind, payload = self.q.get_nowait()
                getattr(self, "_on_" + kind)(payload)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def _on_row(self, p):
        path, info, size = p
        info["size"] = size
        self.info[str(path)] = info
        self.tree.insert("", "end", iid=str(path),
                         text=str(path.relative_to(self.folder)),
                         values=(info["codec"], human_size(size), human_dur(info["dur"]), ""),
                         tags=("already",) if info["codec"] == "hevc" else ())

    def _on_scandone(self, n):
        self.file_lbl.configure(text=f"{n} Videodateien gefunden")
        self.set_busy(False)

    def _on_log(self, text):
        self.write_log(text)

    def _on_fileprog(self, p):
        name, pct = p
        self.file_bar["value"] = pct
        self.file_lbl.configure(text=f"{name} - {pct:.0f} %")

    def _on_totalprog(self, p):
        i, n = p
        self.total_bar["value"] = i / n * 100
        self.total_lbl.configure(text=f"Datei {i} von {n}")

    def _on_mark(self, p):
        iid, tag = p
        if self.tree.exists(iid):
            self.tree.item(iid, tags=(tag,))

    def _on_swap(self, p):
        """Zeile des Originals durch die konvertierte Datei ersetzen."""
        old_iid, dst, old_size = p
        if not self.tree.exists(old_iid):
            return
        idx = self.tree.index(old_iid)
        self.tree.delete(old_iid)
        self.info.pop(old_iid, None)
        info = probe(dst)
        info["size"] = new_size = dst.stat().st_size
        self.info[str(dst)] = info
        self.saved += old_size - new_size
        self.tree.insert("", idx, iid=str(dst),
                         text=str(dst.relative_to(self.folder)),
                         values=(info["codec"], human_size(new_size),
                                 human_dur(info["dur"]), saving_text(old_size, new_size)),
                         tags=("done",))

    def _on_finished(self, _):
        self.set_busy(False)
        self.file_bar["value"] = 0
        text = "fertig"
        if self.saved > 0:
            text += f" - {human_size(self.saved)} gespart"
        self.file_lbl.configure(text=text)

    def set_busy(self, busy):
        self.busy = busy
        self.start_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    # -- Aktionen

    def pick_folder(self):
        if self.busy:
            return
        d = filedialog.askdirectory(title="Ordner mit Videodateien waehlen")
        if not d:
            return
        self.folder = Path(d)
        self.folder_lbl.configure(text=str(self.folder))
        self.tree.delete(*self.tree.get_children())
        self.info.clear()
        self.total_bar["value"] = 0
        self.total_lbl.configure(text="")
        self.set_busy(True)
        self.file_lbl.configure(text="scanne...")
        if is_network(self.folder):
            self.write_log(f"Netzlaufwerk erkannt - dort gibt es keinen Papierkorb. "
                           f"Originale werden nach <ordner>\\{ALT_DIR}\\ verschoben, "
                           f"sofern 'endgueltig loeschen' nicht angehakt ist.")
        threading.Thread(target=self.scan, daemon=True).start()

    def scan(self):
        files = sorted(p for p in self.folder.rglob("*")
                       if p.is_file() and p.suffix.lower() in VIDEO_EXT
                       and ALT_DIR not in p.parts)   # weggeraeumte Originale nicht erneut anbieten
        with ThreadPoolExecutor(max_workers=8) as pool:
            for path, info in zip(files, pool.map(probe, files)):
                self.q.put(("row", (path, info, path.stat().st_size)))
        self.q.put(("scandone", len(files)))

    def select_convertible(self):
        self.tree.selection_set([iid for iid in self.tree.get_children()
                                 if "already" not in self.tree.item(iid, "tags")])

    def on_start(self):
        if self.busy:
            return
        todo = [iid for iid in self.tree.selection()
                if "already" not in self.tree.item(iid, "tags")]
        if not todo:
            messagebox.showinfo("Nichts zu tun",
                                "Keine konvertierbaren Dateien ausgewaehlt.")
            return
        # tkinter-Variablen nur hier im Main-Thread auslesen und mitgeben
        permanent = self.permanent.get()
        if permanent and not messagebox.askokcancel(
                "Originale endgueltig loeschen?",
                f"{len(todo)} Original(e) werden nach erfolgreicher Konvertierung "
                f"sofort geloescht - ohne Papierkorb und ohne {ALT_DIR}.\n\n"
                f"Das laesst sich nicht rueckgaengig machen.",
                icon="warning", default="cancel"):
            return
        self.cancel.clear()
        self.saved = 0
        self.set_busy(True)
        threading.Thread(target=self.run_batch,
                         args=(todo, self.encoder.get(), self.quality.get(), permanent),
                         daemon=True).start()

    def on_cancel(self):
        self.cancel.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.q.put(("log", "Abbruch angefordert..."))

    def run_batch(self, todo, encoder, quality, permanent=False):
        n = len(todo)
        try:
            for i, iid in enumerate(todo, 1):
                if self.cancel.is_set():
                    break
                src = Path(iid)
                self.q.put(("totalprog", (i, n)))
                self.q.put(("fileprog", (src.name, 0)))
                dst = target_for(src)
                if not src.exists():
                    self.fail(iid, f"UEBERSPRUNGEN {src.name}: nicht mehr vorhanden")
                    continue
                if dst.exists():
                    self.fail(iid, f"UEBERSPRUNGEN {src.name}: {dst.name} existiert bereits")
                    continue
                ok, msg = convert_one(
                    src, dst, encoder, quality, self.info[iid],
                    on_progress=lambda pct, nm=src.name: self.q.put(("fileprog", (nm, pct))),
                    cancel=self.cancel,
                    proc_sink=lambda p: setattr(self, "proc", p))
                if not ok:
                    self.fail(iid, f"FEHLER {src.name}: {msg}")
                    continue
                src_size, dst_size = src.stat().st_size, dst.stat().st_size
                if not worth_keeping(src_size, dst_size):
                    discard(dst)
                    self.q.put(("log", (
                        f"BEHALTEN {src.name}: HEVC spart zu wenig "
                        f"({human_size(dst_size)} statt {human_size(src_size)}, "
                        f"mindestens {MIN_SAVING:.0%} noetig) - Original bleibt")))
                    self.q.put(("mark", (iid, "kept")))
                    continue
                try:
                    note = remove_original(src, permanent)
                except Exception as e:
                    self.fail(iid, f"WARNUNG {src.name}: konvertiert nach {dst.name}, "
                                   f"aber Original nicht entfernt ({e})")
                    continue
                self.q.put(("log", f"OK {src.name} -> {dst.name} "
                                   f"({saving_text(src_size, dst_size)}, {note})"))
                self.q.put(("swap", (iid, dst, src_size)))
        except Exception as e:
            self.q.put(("log", f"ABBRUCH durch internen Fehler: {e!r}"))
        finally:
            # muss in jedem Fall raus, sonst bleibt die GUI auf 'busy' haengen
            self.q.put(("finished", None))

    def fail(self, iid, msg):
        self.q.put(("log", msg))
        self.q.put(("mark", (iid, "failed")))

    def on_close(self):
        if self.busy and not messagebox.askokcancel(
                "Beenden?", "Es laeuft noch eine Konvertierung. Wirklich beenden?"):
            return
        self.cancel.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.root.destroy()


if __name__ == "__main__":
    selfcheck = "--selfcheck" in sys.argv
    root = tk.Tk()
    App(root)
    if selfcheck:
        # Startcheck fuer die CI: Fenster baut sich auf, mainloop laeuft, sauberes Ende.
        root.after(300, root.destroy)
    root.mainloop()
    if selfcheck and send2trash is None:
        # Eine Exe ohne send2trash wuerde Originale still endgueltig loeschen.
        sys.exit(3)
