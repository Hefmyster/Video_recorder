"""
Registratore Schermo con Audio
- Video: ffmpeg (gdigrab) incluso tramite imageio-ffmpeg
- Audio: audio di sistema (WASAPI loopback) e/o microfono tramite soundcard
"""
import os
import subprocess
import threading
import time
import wave
import warnings
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import soundcard as sc
import imageio_ffmpeg

warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SAMPLE_RATE = 48000
CHANNELS = 2
BLOCK = 1024
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AudioRecorder(threading.Thread):
    """Registra da un dispositivo, riempiendo di silenzio i buchi (es. loopback quando nulla suona)."""

    def __init__(self, device, start_event, stop_event):
        super().__init__(daemon=True)
        self.device = device
        self.start_event = start_event
        self.stop_event = stop_event
        self.chunks = []
        self.frames = 0
        self.error = None

    def run(self):
        try:
            import comtypes  # noqa: F401  (soundcard usa COM su Windows)
        except ImportError:
            pass
        try:
            with self.device.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS, blocksize=BLOCK) as rec:
                self.start_event.wait()
                t0 = time.perf_counter()
                while not self.stop_event.is_set():
                    data = rec.record(numframes=BLOCK)
                    if data.ndim == 1:
                        data = np.column_stack([data, data])
                    data = data[:, :CHANNELS]
                    # Se il dispositivo e' rimasto indietro rispetto al tempo reale, inserisci silenzio
                    expected = int((time.perf_counter() - t0) * SAMPLE_RATE)
                    gap = expected - (self.frames + len(data))
                    if gap > SAMPLE_RATE // 10:
                        self.chunks.append(np.zeros((gap, CHANNELS), dtype=np.float32))
                        self.frames += gap
                    self.chunks.append(data.astype(np.float32))
                    self.frames += len(data)
        except Exception as e:  # noqa: BLE001
            self.error = e

    def get_audio(self):
        if not self.chunks:
            return np.zeros((0, CHANNELS), dtype=np.float32)
        return np.concatenate(self.chunks)


def write_wav(path, audio):
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


class App:
    def __init__(self, root):
        self.root = root
        root.title("Registratore Schermo")
        root.resizable(False, False)

        self.output_dir = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Videos"))
        self.sys_audio = tk.BooleanVar(value=True)
        self.mic_audio = tk.BooleanVar(value=True)
        self.fps = tk.IntVar(value=30)
        self.minimize = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Pronto")
        self.timer = tk.StringVar(value="00:00:00")

        self.speakers = sc.all_speakers()
        self.mics = sc.all_microphones()
        default_spk = sc.default_speaker().name
        default_mic = sc.default_microphone().name if self.mics else ""

        f = ttk.Frame(root, padding=12)
        f.grid()

        ttk.Label(f, text="Cartella di salvataggio:").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.output_dir, width=42).grid(row=1, column=0, columnspan=2, sticky="we")
        ttk.Button(f, text="Sfoglia...", command=self.browse).grid(row=1, column=2, padx=(6, 0))

        ttk.Checkbutton(f, text="Audio di sistema", variable=self.sys_audio).grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.spk_combo = ttk.Combobox(f, values=[s.name for s in self.speakers], state="readonly", width=34)
        self.spk_combo.set(default_spk)
        self.spk_combo.grid(row=2, column=1, columnspan=2, sticky="we", pady=(10, 0))

        ttk.Checkbutton(f, text="Microfono", variable=self.mic_audio).grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.mic_combo = ttk.Combobox(f, values=[m.name for m in self.mics], state="readonly", width=34)
        self.mic_combo.set(default_mic)
        self.mic_combo.grid(row=3, column=1, columnspan=2, sticky="we", pady=(4, 0))

        ttk.Label(f, text="FPS:").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(f, textvariable=self.fps, values=[15, 24, 30, 60], state="readonly", width=6).grid(
            row=4, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(f, text="Riduci a icona durante la registrazione", variable=self.minimize).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Label(f, textvariable=self.timer, font=("Segoe UI", 22, "bold")).grid(row=6, column=0, columnspan=3, pady=10)

        self.start_btn = ttk.Button(f, text="● Avvia registrazione", command=self.start)
        self.start_btn.grid(row=7, column=0, columnspan=2, sticky="we")
        self.stop_btn = ttk.Button(f, text="■ Ferma", command=self.stop, state="disabled")
        self.stop_btn.grid(row=7, column=2, sticky="we", padx=(6, 0))

        ttk.Label(f, textvariable=self.status, foreground="gray").grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))

        root.bind("<Control-F9>", lambda e: self.stop() if self.recording else self.start())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.recording = False

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.output_dir.get())
        if d:
            self.output_dir.set(d)

    def start(self):
        out_dir = self.output_dir.get()
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.final_path = os.path.join(out_dir, f"Registrazione_{stamp}.mp4")
        self.tmp_video = os.path.join(out_dir, f".tmp_video_{stamp}.mkv")
        self.tmp_audio = os.path.join(out_dir, f".tmp_audio_{stamp}.wav")

        self.start_event = threading.Event()
        self.stop_event = threading.Event()
        self.audio_threads = []
        try:
            if self.sys_audio.get():
                spk = next(s for s in self.speakers if s.name == self.spk_combo.get())
                loop = sc.get_microphone(spk.id, include_loopback=True)
                self.audio_threads.append(AudioRecorder(loop, self.start_event, self.stop_event))
            if self.mic_audio.get() and self.mics:
                mic = next(m for m in self.mics if m.name == self.mic_combo.get())
                self.audio_threads.append(AudioRecorder(mic, self.start_event, self.stop_event))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Errore audio", str(e))
            return

        for t in self.audio_threads:
            t.start()
        time.sleep(0.3)  # lascia aprire i dispositivi audio

        cmd = [FFMPEG, "-y", "-f", "gdigrab", "-framerate", str(self.fps.get()), "-draw_mouse", "1",
               "-i", "desktop", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
               "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", self.tmp_video]
        self.ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self.start_event.set()
        self.t_start = time.time()
        self.recording = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status.set("Registrazione in corso...  (Ctrl+F9 per fermare quando la finestra è attiva)")
        self.tick()
        if self.minimize.get():
            self.root.after(500, self.root.iconify)

    def tick(self):
        if not self.recording:
            return
        s = int(time.time() - self.t_start)
        self.timer.set(f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}")
        if self.ffmpeg.poll() is not None:
            self.recording = False
            self.stop_event.set()
            messagebox.showerror("Errore", "ffmpeg si è fermato inaspettatamente.")
            self.reset_ui()
            return
        self.root.after(250, self.tick)

    def stop(self):
        if not self.recording:
            return
        self.recording = False
        self.stop_btn.config(state="disabled")
        self.status.set("Salvataggio in corso...")
        self.root.deiconify()
        threading.Thread(target=self.finish, daemon=True).start()

    def finish(self):
        try:
            try:
                self.ffmpeg.communicate(b"q", timeout=15)
            except subprocess.TimeoutExpired:
                self.ffmpeg.kill()
            self.stop_event.set()
            for t in self.audio_threads:
                t.join(timeout=5)

            errors = [str(t.error) for t in self.audio_threads if t.error]
            tracks = [t.get_audio() for t in self.audio_threads if not t.error]
            tracks = [a for a in tracks if len(a)]

            if tracks:
                n = max(len(a) for a in tracks)
                mix = np.zeros((n, CHANNELS), dtype=np.float32)
                for a in tracks:
                    mix[:len(a)] += a
                write_wav(self.tmp_audio, mix)
                cmd = [FFMPEG, "-y", "-i", self.tmp_video, "-i", self.tmp_audio,
                       "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                       "-shortest", self.final_path]
            else:
                cmd = [FFMPEG, "-y", "-i", self.tmp_video, "-c:v", "copy", self.final_path]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)

            for p in (self.tmp_video, self.tmp_audio):
                if os.path.exists(p):
                    os.remove(p)
            msg = f"Salvato: {self.final_path}"
            if errors:
                msg += "\nAvvisi audio: " + "; ".join(errors)
            self.root.after(0, lambda: self.done(msg))
        except Exception as e:  # noqa: BLE001
            self.root.after(0, lambda: self.done(f"Errore: {e}"))

    def done(self, msg):
        self.status.set(msg)
        self.reset_ui()
        if os.path.exists(self.final_path) and messagebox.askyesno("Fatto", msg + "\n\nAprire la cartella?"):
            subprocess.Popen(["explorer", "/select,", self.final_path])

    def reset_ui(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    def on_close(self):
        if self.recording and not messagebox.askyesno("Esci", "Registrazione in corso. Fermarla e salvare?"):
            return
        if self.recording:
            self.stop()
            self.root.after(1000, self.wait_and_close)
        else:
            self.root.destroy()

    def wait_and_close(self):
        if self.stop_btn["state"] == "disabled" and self.start_btn["state"] == "normal":
            self.root.destroy()
        else:
            self.root.after(500, self.wait_and_close)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
