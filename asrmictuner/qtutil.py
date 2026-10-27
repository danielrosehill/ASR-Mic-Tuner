"""Small Qt helpers shared by the pages: background calls, HTML rendering of
findings and metrics, the AI review flow."""

import html
import threading

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QInputDialog, QLabel

from . import ai
from .core import fmt_gain, fmt_level

LEVEL_ICON = {"pass": ("✔", "#2ea44f"), "warn": ("▲", "#d9a340"), "fail": ("✖", "#d43333")}


class _Bridge(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)


_live = set()


def run_async(fn, on_done, on_fail=None):
    """Run fn() on a thread; deliver the result on the GUI thread."""
    bridge = _Bridge()
    _live.add(bridge)

    def finish(result):
        _live.discard(bridge)
        on_done(result)

    def fail(message):
        _live.discard(bridge)
        if on_fail:
            on_fail(message)

    bridge.done.connect(finish)
    bridge.failed.connect(fail)

    def target():
        try:
            result = fn()
        except Exception as exc:          # surfaced in the UI, never swallowed
            bridge.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        bridge.done.emit(result)

    threading.Thread(target=target, daemon=True).start()


def wrap_label(text="", rich=True):
    label = QLabel(text)
    label.setWordWrap(True)
    if rich:
        label.setTextFormat(Qt.TextFormat.RichText)
    return label


def ask_text(parent, title, prompt, default=""):
    text, ok = QInputDialog.getText(parent, title, prompt, text=default)
    return text.strip() if ok and text.strip() else None


def esc(text):
    return html.escape(str(text)) if text is not None else ""


def findings_html(findings):
    rows = []
    for f in findings or []:
        icon, colour = LEVEL_ICON.get(f.get("level"), ("•", "#888"))
        rows.append(f"<tr><td style='color:{colour};padding-right:6px'>{icon}</td>"
                    f"<td>{esc(f.get('text'))}</td></tr>")
    return "<table>" + "".join(rows) + "</table>" if rows else ""


def _lv(value):
    return fmt_level(value) if value is not None else "—"


def _num(value, unit="", digits=1):
    if value is None:
        return "—"
    return f"{value:.{digits}f}{unit}"


def metric_rows(m):
    """(label, value) pairs worth showing for a measurement's metrics."""
    kind = m.get("kind")
    rows = []
    if kind == "room":
        hum = m.get("hum") or {}
        rows += [
            ("Noise floor (p10)", _lv(m.get("noise_floor_p10_dbfs"))),
            ("Noise RMS", _lv(m.get("noise_rms_dbfs"))),
            ("A-weighted", _lv(m.get("noise_a_weighted_dbfs"))),
            ("Noise p90", _lv(m.get("noise_p90_dbfs"))),
            ("Stationarity", _num(m.get("stationarity_db"), " dB σ")),
            ("Transients", str(m.get("transients", "—"))),
            ("Rumble <150 Hz", _lv(m.get("low_rumble_dbfs"))),
            ("Speech band 300–3400", _lv(m.get("speech_band_dbfs"))),
            ("Hiss 4–16 kHz", _lv(m.get("hiss_dbfs"))),
            ("Spectral flatness", _num(m.get("spectral_flatness"), "", 3)),
            ("Mains hum", (f"{hum.get('strongest_hz', 0):.0f} Hz, +{hum.get('prominence_db', 0):.0f} dB"
                           if hum.get("present") else "none")),
            ("Character", esc(m.get("character", "—"))),
        ]
    elif kind == "voice":
        rows += [
            ("Speech peak (p95)", _lv(m.get("speech_peak_p95_dbfs"))),
            ("Speech peak (max)", _lv(m.get("speech_peak_max_dbfs"))),
            ("Speech RMS", _lv(m.get("speech_rms_dbfs"))),
            ("SNR", _num(m.get("snr_db"), " dB") + f" <small>vs {esc(m.get('noise_reference_source', ''))}</small>"),
            ("Crest factor", _num(m.get("crest_factor_db"), " dB")),
            ("Dynamic range", _num(m.get("dynamic_range_db"), " dB")),
            ("Speech in sample", _num((m.get("speech_ratio") or 0) * 100, " %", 0)),
            ("Pitch (F0) median", _num(m.get("f0_median_hz"), " Hz", 0)),
            ("Pitch range p10–p90", f"{_num(m.get('f0_p10_hz'), '', 0)}–{_num(m.get('f0_p90_hz'), ' Hz', 0)}"),
            ("Spectral centroid", _num(m.get("spectral_centroid_hz"), " Hz", 0)),
            ("Spectral tilt", _num(m.get("spectral_tilt_db_per_octave"), " dB/oct")),
            ("Bandwidth", _num((m.get("bandwidth_hz") or 0) / 1000, " kHz")),
            ("Rumble vs body", _num(m.get("low_vs_body_db"), " dB")),
            ("Presence vs body", _num(m.get("presence_vs_body_db"), " dB")),
            ("Sibilance vs body", _num(m.get("sibilance_vs_body_db"), " dB")),
            ("Plosive pops", str(m.get("plosive_pops", "—"))),
        ]
    elif kind == "calibration":
        rows += [
            ("Mode", esc(m.get("mode", "—"))),
            ("Noise floor", _lv(m.get("noise_dbfs"))),
            ("Speech RMS", _lv(m.get("speech_rms_dbfs"))),
            ("Speech peak", _lv(m.get("speech_peak_dbfs"))),
            ("Separation", _num(m.get("separation_db"), " dB")),
            ("Recommended", (f"index {m['recommended_index']} ({fmt_gain(m.get('recommended_db'))})"
                             if m.get("recommended_index") is not None else "—")),
        ]
    elif kind == "snapshot":
        rows += [
            ("Noise floor", _lv(m.get("noise_dbfs"))),
            ("Speech peak", _lv(m.get("speech_peak_dbfs"))),
            ("Clipped runs", str(m.get("clipped_runs", "—"))),
            ("Monitored", _num(m.get("monitored_s"), " s", 0)),
        ]
    if kind in ("room", "voice"):
        for n, ch in enumerate(m.get("channels") or []):
            rows.append((f"Ch {n + 1} RMS / peak", f"{_lv(ch.get('rms_dbfs'))} / {_lv(ch.get('peak_dbfs'))}"))
            if ch.get("clipped_runs"):
                rows.append((f"Ch {n + 1} clipping",
                             f"<span style='color:#d43333'>{ch['clipped_samples']} samples, "
                             f"{ch['clipped_runs']} runs</span>"))
        if m.get("channel_imbalance_db") is not None:
            rows.append(("Channel imbalance", _num(m.get("channel_imbalance_db"), " dB")))
    return rows


def metrics_html(m, columns=2):
    rows = metric_rows(m or {})
    if not rows:
        return ""
    per = (len(rows) + columns - 1) // columns
    cols = [rows[i * per:(i + 1) * per] for i in range(columns)]
    cells = []
    for r in range(per):
        tds = []
        for col in cols:
            if r < len(col):
                label, value = col[r]
                tds.append(f"<td style='color:palette(mid);padding-right:8px'>{esc(label)}</td>"
                           f"<td style='padding-right:22px'><b>{value}</b></td>")
            else:
                tds.append("<td></td><td></td>")
        cells.append("<tr>" + "".join(tds) + "</tr>")
    return "<table cellspacing='2'>" + "".join(cells) + "</table>"


def key_result(measurement):
    """One short string summarising a measurement, for list views."""
    m = measurement.get("metrics") or {}
    kind = measurement.get("kind")
    if kind == "room":
        return f"floor {_lv(m.get('noise_floor_p10_dbfs'))}" + (", hum" if (m.get("hum") or {}).get("present") else "")
    if kind == "voice":
        score = (m.get("readiness") or {}).get("score")
        return f"score {score}, peak {_lv(m.get('speech_peak_p95_dbfs'))}, SNR {_num(m.get('snr_db'), ' dB', 0)}"
    if kind == "calibration":
        return f"peak {_lv(m.get('speech_peak_dbfs'))}, floor {_lv(m.get('noise_dbfs'))}"
    if kind == "snapshot":
        return f"floor {_lv(m.get('noise_dbfs'))}, peak {_lv(m.get('speech_peak_dbfs'))}"
    return ""


def ai_html(reply):
    if not reply:
        return ""
    if reply.get("_error"):
        return f"<p style='color:#d43333'>{esc(reply['_error'])}</p>"
    parts = [f"<p><b>{esc(reply.get('verdict', ''))}</b></p>"]
    if reply.get("score_comment"):
        parts.append(f"<p>{esc(reply['score_comment'])}</p>")
    recs = reply.get("recommendations") or []
    if recs:
        colour = {"high": "#d43333", "medium": "#d9a340", "low": "#3b7ad6"}
        items = "".join(
            f"<li><span style='color:{colour.get(r.get('priority'), '#888')}'>"
            f"[{esc(r.get('priority', ''))}]</span> <b>{esc(r.get('title', ''))}</b> — "
            f"{esc(r.get('detail', ''))}</li>" for r in recs)
        parts.append(f"<ul>{items}</ul>")
    if reply.get("suggested_gain_db") is not None:
        parts.append(f"<p>Suggested gain: <b>{fmt_gain(reply['suggested_gain_db'])}</b></p>")
    chain = reply.get("asr_chain_suggestions") or []
    if chain:
        parts.append("<p>For the dictation chain: " + "; ".join(esc(c) for c in chain) + "</p>")
    if reply.get("voice_notes"):
        parts.append(f"<p><i>{esc(reply['voice_notes'])}</i></p>")
    usage = reply.get("_usage") or {}
    cost = usage.get("cost")
    parts.append(f"<p><small>{esc(reply.get('_model', ''))}"
                 + (f" · {usage.get('prompt_tokens', '?')}+{usage.get('completion_tokens', '?')} tokens" if usage else "")
                 + (f" · ${cost:.4f}" if isinstance(cost, (int, float)) else "")
                 + "</small></p>")
    return "".join(parts)


def ask_ai(session, measurement, browser, button=None):
    """Run an AI review for a stored measurement; render into `browser` and
    keep the reply in the history database."""
    if button is not None:
        button.setEnabled(False)
    browser.setHtml("<p><i>Asking the model…</i></p>")
    context = session.ai_context(measurement)
    settings = dict(session.settings)
    sample = measurement.get("sample_path")

    def done(reply):
        if button is not None:
            button.setEnabled(True)
        session.store.update_measurement(measurement["id"], ai=reply)
        measurement["ai"] = reply
        browser.setHtml(ai_html(reply))
        session.history_changed.emit()

    def failed(message):
        if button is not None:
            button.setEnabled(True)
        browser.setHtml(ai_html({"_error": message}))

    run_async(lambda: ai.review(settings, context, sample), done, failed)
