"""Quick tune: listen to you talk for ~30 s, set the gain, save it as a profile.

The one-click version of the mic test. It records whatever you say (no passage),
measures the speech with `analysis.analyse_voice`, moves the gain so speech peaks
(95th percentile of 30 ms block peaks) land on the ASR target, and saves the result
as a **profile**: a gain preset for this microphone and port, named after the place
("Home office", "Coffee shop"), carrying the noise reduction mode in force and the
numbers it was tuned on. When the correction is large (> 6 dB) it applies it and
listens once more for 15 s to confirm, because the first estimate was made far from
the target. Any clipped run, or a peak within 1 dB of full scale, takes 3 dB more
headroom.

`plan_gain` is pure (no Qt) so the CLI (`--tune`) and the GUI share it.
"""

import time

from . import analysis
from .core import fmt_gain, target_peak_db

MIN_SPEECH_S = 3.0          # active speech blocks (analysis gates hard: ~4 s in 30 s of real talk)
CONVERGED_DB = 1.5          # closer than this to target: leave the gain alone
VERIFY_OVER_DB = 6.0        # a bigger move gets a second, shorter listen
VERIFY_SECONDS = 15.0
CLIP_HEADROOM_DB = 3.0


def speech_seconds(metrics):
    return float(metrics.get("speech_ratio") or 0.0) * float(metrics.get("duration_s") or 0.0)


def clipped_runs(metrics):
    return sum(int(c.get("clipped_runs") or 0) for c in metrics.get("channels") or [])


def plan_gain(metrics, settings, curve=None, index=None, soft_percent=None):
    """What to do about the gain after one listen. Returns a dict:
    ok, reason, peak, target, change_db, and index (hardware) or soft_percent."""
    target = target_peak_db(settings)
    peak = metrics.get("speech_peak_p95_dbfs")
    heard = speech_seconds(metrics)
    if peak is None or heard < MIN_SPEECH_S:
        return {"ok": False, "reason": f"Heard only {heard:.0f} s of speech. Talk for most of the time, "
                                       "at your normal dictating distance and volume."}
    clips = clipped_runs(metrics)
    peak_max = metrics.get("speech_peak_max_dbfs")
    if clips or (peak_max is not None and peak_max >= -1.0):
        target -= CLIP_HEADROOM_DB
    want = target - peak
    plan = {"ok": True, "peak": peak, "target": target, "clipped_runs": clips, "speech_s": heard}
    if curve is not None and index is not None:
        here = curve.db(index)
        new = curve.index_for_db(here + want)
        plan.update(index=new, change_db=curve.db(new) - here, gain_db=curve.db(new),
                    at_limit=(want > 0 and new >= curve.imax) or (want < 0 and new <= curve.imin))
    elif soft_percent:
        # PipeWire software volume is cubic: dB = 60·log10(v).
        pct = max(1, min(150, int(round(soft_percent * 10 ** (want / 60.0)))))
        plan.update(soft_percent=pct, change_db=want, at_limit=pct in (1, 150))
    else:
        return {"ok": False, "reason": "This device has no gain control the tuner can move."}
    plan["converged"] = abs(plan["change_db"]) < CONVERGED_DB
    plan["verify"] = abs(plan["change_db"]) > VERIFY_OVER_DB
    return plan


def summary(metrics):
    """The numbers a profile keeps from its tune."""
    r = metrics.get("readiness") or {}
    return {
        "speech_peak_p95_dbfs": metrics.get("speech_peak_p95_dbfs"),
        "speech_rms_dbfs": metrics.get("speech_rms_dbfs"),
        "noise_dbfs": metrics.get("noise_reference_dbfs"),
        "snr_db": metrics.get("snr_db"),
        "score": r.get("score"),
    }


def describe(plan, before_text):
    if not plan.get("ok"):
        return plan["reason"]
    if plan.get("index") is not None:
        after = fmt_gain(plan["gain_db"])
    else:
        after = f"{plan['soft_percent']}%"
    text = (f"Speech peaks {plan['peak']:.0f} dBFS, target {plan['target']:.0f}. "
            + ("Gain already right" if plan["converged"] else f"Gain {before_text} → {after} ({plan['change_db']:+.1f} dB)"))
    if plan.get("clipped_runs"):
        text += f"; {plan['clipped_runs']} clipped runs, so 3 dB extra headroom"
    if plan.get("at_limit") and not plan["converged"]:
        text += "; the gain control is at its limit, move closer to (or away from) the mic"
    return text + "."


# ---------------------------------------------------------------------------
# GUI controller
# ---------------------------------------------------------------------------

try:
    from PyQt6.QtCore import QObject, QTimer, pyqtSignal
except ImportError:          # CLI-only installs
    QObject = object


if QObject is not object:
    class QuickTune(QObject):
        """Runs a tune on the session's analyser. Signals carry plain text."""
        progress = pyqtSignal(int, str)         # percent, text
        finished = pyqtSignal(str, str)         # profile label, result text
        failed = pyqtSignal(str)

        def __init__(self, session):
            super().__init__()
            self.s = session
            self.running = False
            self._pass = 0
            self._until = 0.0
            self._length = 0.0
            self._place = ""
            self._notes = []
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)

        def start(self, place=""):
            if self.running:
                self.cancel()
                return
            if self.s.device is None:
                self.failed.emit("No microphone selected.")
                return
            self._place = (place or self.s.location() or "Default").strip()
            self._notes = []
            self.running = True
            self.s.acquire("tune")
            self.s.ensure_capture()
            self._listen(float(self.s.settings.get("tune_seconds", 30)), 1)

        def cancel(self, message="Quick tune cancelled."):
            if not self.running:
                return
            self._timer.stop()
            if self.s.analyser:
                self.s.analyser.end_record()
            self.running = False
            self.s.release("tune")
            self.failed.emit(message)

        def _listen(self, seconds, n):
            a = self.s.analyser
            if not a or not a.alive:
                self.cancel("No capture stream.")
                return
            self._pass, self._length = n, seconds
            self._until = time.monotonic() + seconds
            a.begin_record()
            self._timer.start(200)
            self._tick()

        def _tick(self):
            remaining = self._until - time.monotonic()
            if remaining > 0:
                done = 1 - remaining / self._length
                what = "Talk normally, as you dictate" if self._pass == 1 else "Once more to confirm, keep talking"
                self.progress.emit(int(100 * done), f"{what}… {remaining:.0f} s")
                return
            self._timer.stop()
            a = self.s.analyser
            audio = a.end_record() if a else None
            rate = a.rate if a else 48000
            if audio is None or len(audio) < rate:
                self.cancel("Too little audio captured.")
                return
            self.progress.emit(100, "Measuring…")
            room = self.s.active_room_profile()
            room_metrics = room.get("metrics") if room else None
            from .qtutil import run_async
            run_async(lambda: analysis.analyse_voice(audio, rate, room=room_metrics),
                      self._measured, lambda msg: self.cancel(msg))

        def _measured(self, metrics):
            if not self.running:
                return
            s = self.s
            before = (fmt_gain(s.gain_db()) if s.gain is not None
                      else f"{s.device.pw_percent}%")
            plan = plan_gain(metrics, s.settings, s.curve, s.gain.index if s.gain is not None else None,
                             s.device.pw_percent if s.soft_only else None)
            s.log("voice", metrics, notes=f"Quick tune pass {self._pass} ({self._place})",
                  duration_s=metrics.get("duration_s"))
            if not plan["ok"]:
                self.cancel(plan["reason"])
                return
            self._notes.append(describe(plan, before))
            if not plan["converged"]:
                ok = s.set_index(plan["index"]) if plan.get("index") is not None else s.set_soft(plan["soft_percent"])
                if not ok:
                    self.cancel("The gain change was refused.")
                    return
            if plan["verify"] and self._pass == 1:
                self._listen(VERIFY_SECONDS, 2)
                return
            self.running = False
            s.release("tune")
            s.save_profile(self._place, summary(metrics))
            self.finished.emit(self._place, " ".join(self._notes) + f" Saved as profile “{self._place}”.")
