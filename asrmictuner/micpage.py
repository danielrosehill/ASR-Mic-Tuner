"""Profiles tab, top: "About this microphone" (human notes) and its reference clips (v3.0).

Data and rules live in mics.py; docs/mics.md describes both.
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)

from . import analysis, mics, store
from .core import fmt_gain
from .qtutil import esc, findings_html, run_async, wrap_label


class AboutBox(QGroupBox):
    """Maker, model, type, connection and free notes for the selected mic.
    Shared across computers: it travels with the mic's profile file."""

    def __init__(self, session, parent=None):
        super().__init__("About this microphone", parent)
        self.s = session
        lay = QVBoxLayout(self)
        lay.addWidget(wrap_label(
            "<small>What you know about this mic that its USB descriptor doesn't. Kept with the "
            "mic (not the computer), so it syncs with its profiles.</small>"))
        form = QFormLayout()
        self.maker, self.model = QLineEdit(), QLineEdit()
        self.type, self.connection = QComboBox(), QComboBox()
        for combo, items in ((self.type, mics.TYPES), (self.connection, mics.CONNECTIONS)):
            for value, label in items:
                combo.addItem(label, value)
        self.notes = QPlainTextEdit()
        self.notes.setMaximumHeight(70)
        self.descriptor = wrap_label()
        form.addRow("Maker", self.maker)
        form.addRow("Model", self.model)
        form.addRow("Type", self.type)
        form.addRow("Connection", self.connection)
        form.addRow("Notes", self.notes)
        form.addRow("Reports itself as", self.descriptor)
        lay.addLayout(form)
        row = QHBoxLayout()
        save = QPushButton("Save notes")
        save.clicked.connect(self.save)
        row.addWidget(save)
        row.addStretch(1)
        self.bench = wrap_label()
        lay.addLayout(row)
        lay.addWidget(self.bench)
        self.s.device_changed.connect(self.reload)
        self.s.mics_changed.connect(self.reload)
        self.reload()

    def reload(self):
        d = self.s.device
        self.setEnabled(d is not None)
        if d is None:
            return
        saved = mics.about(d.key)
        shown = saved or mics.guess_about(d)
        self.maker.setText(shown.get("maker", ""))
        self.model.setText(shown.get("model", ""))
        for combo, value in ((self.type, shown.get("type", "")), (self.connection, shown.get("connection", ""))):
            combo.setCurrentIndex(max(0, combo.findData(value)))
        self.notes.setPlainText(saved.get("notes", ""))
        ident = d.ident or {}
        ids = ":".join(x.replace("0x", "") for x in (ident.get("vendor_id"), ident.get("product_id")) if x)
        self.descriptor.setText(
            f"<small>{esc(d.description)}"
            + (f" · {esc(ident.get('vendor_name', ''))} {esc(ident.get('product_name', ''))}" if ids else "")
            + (f" · USB {ids}" if ident.get("bus") == "usb" and ids else "")
            + ("" if saved else " · <i>not saved yet: fields above are guesses</i>") + "</small>")
        bench = (mics.profiles.read_all().get(d.key) or {}).get("benchmarks") or []
        if bench:
            b = bench[-1]
            self.bench.setText(f"<small>Last benchmark {esc(b['at'][:16].replace('T', ' '))} on "
                               f"{esc(b.get('computer', ''))}: <b>{b.get('score')}</b> ({esc(b.get('grade', ''))}), "
                               f"SNR {b.get('snr_db')} dB, gain {fmt_gain(b.get('gain_db'))}. "
                               f"{len(bench)} benchmark(s) in total.</small>")
        else:
            self.bench.setText("<small>No benchmark yet: Measure → mic test, then "
                               "<i>Keep as reference clip</i>, then Benchmark below.</small>")

    def save(self):
        d = self.s.device
        if d is None:
            return
        mics.set_about(d.key, ident=d.ident, maker=self.maker.text(), model=self.model.text(),
                       type=self.type.currentData(), connection=self.connection.currentData(),
                       notes=self.notes.toPlainText())
        self.s.status.emit(f"Saved notes for {mics.display_name(d.key, d.ident, d.description)}.")
        self.s.mics_changed.emit()


class ReferencesBox(QGroupBox):
    """Saved recordings of this mic that can be replayed, re-scored or tuned from."""

    def __init__(self, session, parent=None):
        super().__init__("Reference clips for this microphone", parent)
        from .pages import _item, _table, _when
        self._item, self._when = _item, _when
        self.s = session
        lay = QVBoxLayout(self)
        lay.addWidget(wrap_label(
            "<small>Recordings kept so the mic can be re-analysed, benchmarked or tuned without "
            "anyone speaking again. They stay on this computer (not synced). "
            "<code>asr-mic-tuner --benchmark</code> records one; on Measure, a mic test can be kept "
            "as one.</small>"))
        self.table = _table(["Name", "Recorded", "Length", "Gain", "Port", "Place"])
        self.table.setMaximumHeight(160)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        for label, fn in (("Play", self._play), ("Benchmark", self._benchmark), ("Delete", self._delete)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.result = wrap_label()
        lay.addWidget(self.result)
        self.s.device_changed.connect(self.reload)
        self.s.mics_changed.connect(self.reload)
        self.reload()

    def reload(self):
        d = self.s.device
        self.refs = mics.references(d.key) if d else []
        self.table.setRowCount(len(self.refs))
        for r, ref in enumerate(self.refs):
            gain = fmt_gain(ref.get("gain_db")) if ref.get("gain_db") is not None else f"{ref.get('soft_percent')}%"
            for c, text in enumerate((ref.get("name", ""), self._when(ref.get("recorded")),
                                      f"{ref.get('seconds') or 0:.0f} s", gain, ref.get("port") or "",
                                      ref.get("place") or "")):
                self.table.setItem(r, c, self._item(text, ref["id"]))

    def _selected(self):
        row = self.table.currentRow()
        return self.refs[row] if 0 <= row < len(self.refs) else None

    def _play(self):
        ref = self._selected()
        if ref:
            store.play(ref["audio"])

    def _benchmark(self):
        ref = self._selected()
        if not ref:
            return
        self.result.setText("Analysing…")

        def work():
            audio, rate = mics.read_audio(ref["audio"])
            card = mics.scorecard(analysis.analyse_voice(audio, rate), rate, audio.shape[1])
            mics.record_benchmark(ref["mic_key"], card, place=ref.get("place", ""), port=ref.get("port"),
                                  gain_db=ref.get("gain_db"), soft_percent=ref.get("soft_percent"),
                                  reference_id=ref["id"])
            return card

        def done(card):
            self.result.setText(f"<b>ASR readiness {card['score']} ({esc(card['grade'])})</b> at gain "
                                f"{fmt_gain(ref.get('gain_db'))}"
                                + findings_html(card["findings"] + card["asr_checks"]))
            self.s.mics_changed.emit()

        run_async(work, done, lambda msg: self.result.setText(f"<span style='color:#d43333'>{esc(msg)}</span>"))

    def _delete(self):
        ref = self._selected()
        if not ref:
            return
        if QMessageBox.question(self, "Delete reference clip",
                                f"Delete “{ref.get('name')}” ({Path(ref['audio']).name})?") \
                != QMessageBox.StandardButton.Yes:
            return
        mics.delete_reference(ref)
        self.s.mics_changed.emit()


def keep_as_reference(session, measurement, name=None):
    """File a Measure-tab voice sample as a reference clip of its mic."""
    m = measurement
    path = m.get("sample_path")
    key = m.get("mic_key") or (session.device.key if session.device else None)
    if not path or not Path(path).exists() or not key:
        return None
    audio, rate = mics.read_audio(path)
    ref = mics.add_reference(path, key, device_label=m.get("device_label") or "", port=getattr(session.device, "port", None),
                             gain_control=m.get("gain_control"), gain_index=m.get("gain_index"),
                             gain_db=m.get("gain_db"), soft_percent=m.get("soft_percent"), rate=rate,
                             channels=audio.shape[1], seconds=round(len(audio) / rate, 2),
                             place=m.get("location") or "", name=name, notes=m.get("notes") or "",
                             recorded=m.get("created"))
    session.mics_changed.emit()
    return ref
