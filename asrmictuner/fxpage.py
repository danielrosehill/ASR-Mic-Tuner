"""ASR Effects tab: build EasyEffects input chains from the speech-relevant
effects, start from a template, and bind presets to microphones.

Everything here edits EasyEffects' preset and autoload files (effects.py);
EasyEffects does the processing. Calls to its command line go through
run_async because each one spawns a process (a Flatpak sandbox, ~1 s).
"""

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from . import effects
from .effects import EFFECTS, TEMPLATES
from .pages import _item, _table
from .qtutil import esc, run_async, wrap_label

NEW_PRESET = "(new preset)"


class EffectsPage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.s = session
        self.ee = effects.EasyEffects()
        self.chain = []
        self.raw = None
        self._form_widgets = []

        root = QVBoxLayout(self)
        root.addWidget(wrap_label(
            "<small>Input-side clean-up for dictation, run by <b>EasyEffects</b>. This tab writes "
            "EasyEffects presets and per-microphone bindings; ASR Mic Tuner itself processes no audio. "
            "Set the analog gain first (Monitor tab): no effect here can repair a clipped or buried signal.</small>"))

        # -- EasyEffects status --
        statusbox = QGroupBox("EasyEffects")
        sl = QVBoxLayout(statusbox)
        self.state = wrap_label()
        sl.addWidget(self.state)
        row = QHBoxLayout()
        self.start_btn = QPushButton("Start in background")
        self.start_btn.clicked.connect(self._start)
        row.addWidget(self.start_btn)
        self.open_btn = QPushButton("Open EasyEffects")
        self.open_btn.clicked.connect(lambda: self.ee.open_window())
        row.addWidget(self.open_btn)
        self.bypass = QCheckBox("Bypass all effects")
        self.bypass.setToolTip("EasyEffects' global bypass: compare processed and raw without unloading the preset")
        self.bypass.toggled.connect(self._bypass)
        row.addWidget(self.bypass)
        row.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        sl.addLayout(row)
        root.addWidget(statusbox)

        # -- chain editor --
        editbox = QGroupBox("Input chain")
        el = QVBoxLayout(editbox)
        top = QHBoxLayout()
        top.addWidget(QLabel("Preset"))
        self.preset = QComboBox()
        self.preset.activated.connect(lambda _i: self._open_preset())
        top.addWidget(self.preset, 2)
        top.addSpacing(12)
        top.addWidget(QLabel("Template"))
        self.template = QComboBox()
        for name, blurb, _steps in TEMPLATES:
            self.template.addItem(name)
            self.template.setItemData(self.template.count() - 1, blurb, Qt.ItemDataRole.ToolTipRole)
        top.addWidget(self.template, 2)
        use = QPushButton("Use template")
        use.clicked.connect(self._use_template)
        top.addWidget(use)
        el.addLayout(top)
        self.template_blurb = wrap_label()
        self.template.currentIndexChanged.connect(
            lambda i: self.template_blurb.setText(f"<small>{esc(TEMPLATES[i][1])}</small>"))
        self.template_blurb.setText(f"<small>{esc(TEMPLATES[0][1])}</small>")
        el.addWidget(self.template_blurb)

        cols = QHBoxLayout()
        lib = QVBoxLayout()
        lib.addWidget(QLabel("<b>Effects</b>"))
        self.library = QListWidget()
        for kind, spec in EFFECTS.items():
            item = QListWidgetItem(spec["name"])
            item.setData(Qt.ItemDataRole.UserRole, kind)
            item.setToolTip(spec["blurb"])
            self.library.addItem(item)
        self.library.itemDoubleClicked.connect(lambda _i: self._add())
        self.library.currentItemChanged.connect(self._show_blurb)
        lib.addWidget(self.library, 1)
        add = QPushButton("Add to chain →")
        add.clicked.connect(self._add)
        lib.addWidget(add)
        cols.addLayout(lib, 2)

        mid = QVBoxLayout()
        mid.addWidget(QLabel("<b>Chain</b> (top runs first)"))
        self.chain_list = QListWidget()
        self.chain_list.currentRowChanged.connect(self._select)
        mid.addWidget(self.chain_list, 1)
        moves = QHBoxLayout()
        for text, slot in (("↑", lambda: self._move(-1)), ("↓", lambda: self._move(1)), ("Remove", self._remove)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            moves.addWidget(b)
        mid.addLayout(moves)
        cols.addLayout(mid, 2)

        right = QVBoxLayout()
        right.addWidget(QLabel("<b>Settings</b>"))
        self.blurb = wrap_label()
        right.addWidget(self.blurb)
        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        right.addWidget(self.form_host)
        right.addStretch(1)
        cols.addLayout(right, 3)
        el.addLayout(cols)

        self.summary = wrap_label()
        el.addWidget(self.summary)
        save_row = QHBoxLayout()
        save_row.addWidget(QLabel("Save as"))
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Dictation")
        save_row.addWidget(self.name, 2)
        save = QPushButton("Save preset")
        save.clicked.connect(lambda: self._save(load=False))
        save_row.addWidget(save)
        save_load = QPushButton("Save and load")
        save_load.setToolTip("Save, then tell EasyEffects to load it as the input preset now")
        save_load.clicked.connect(lambda: self._save(load=True))
        save_row.addWidget(save_load)
        el.addLayout(save_row)
        root.addWidget(editbox)

        # -- bindings --
        bindbox = QGroupBox("Microphone bindings (EasyEffects autoload)")
        bl = QVBoxLayout(bindbox)
        bl.addWidget(wrap_label(
            "<small>EasyEffects loads the bound preset whenever that microphone and port become the input. "
            "Bindings are per port: the internal mic and a headset jack on the same card are separate.</small>"))
        self.bind_table = _table(["Microphone", "Port", "Preset"])
        self.bind_table.setMinimumHeight(110)
        bl.addWidget(self.bind_table)
        brow = QHBoxLayout()
        self.bind_label = QLabel()
        brow.addWidget(self.bind_label)
        self.bind_preset = QComboBox()
        brow.addWidget(self.bind_preset, 1)
        bind = QPushButton("Bind")
        bind.clicked.connect(self._bind)
        brow.addWidget(bind)
        unbind = QPushButton("Remove selected binding")
        unbind.clicked.connect(self._unbind)
        brow.addWidget(unbind)
        bl.addLayout(brow)
        root.addWidget(bindbox)
        root.addStretch(1)

        self.s.device_changed.connect(self._update_bind_label)
        self._shown_once = False

    # -- lifecycle --
    def showEvent(self, event):
        super().showEvent(event)
        if not self._shown_once:
            self._shown_once = True
            self.refresh()

    def refresh(self):
        self.ee = effects.EasyEffects()
        installed = self.ee.installed
        for w in (self.start_btn, self.open_btn, self.bypass):
            w.setEnabled(installed)
        if not installed:
            self.state.setText("<span style='color:#d43333'>EasyEffects is not installed.</span> "
                               "Install it from Flathub (<code>com.github.wwmm.easyeffects</code>) or the archive.")
            return
        presets = self.ee.presets()
        current = self.preset.currentText()
        self.preset.clear()
        self.preset.addItem(NEW_PRESET)
        self.preset.addItems(presets)
        if current and self.preset.findText(current) >= 0:
            self.preset.setCurrentText(current)
        self.bind_preset.clear()
        self.bind_preset.addItems(presets)
        self._fill_bindings()
        self._update_bind_label()
        self._render_chain()
        where = f"{self.ee.kind}, presets in <code>{esc(self.ee.presets_dir)}</code>"
        self.state.setText(f"Checking… <small>({where})</small>")
        ee = self.ee

        def probe():
            running = ee.running()
            if not running:
                return running, "", False
            return running, ee.last_loaded(), ee.bypassed()

        def show(result):
            running, last, bypassed = result
            if running:
                text = "<span style='color:#2ea44f'>Running</span>"
                text += f" · input preset <b>{esc(last)}</b>" if last else " · no input preset loaded"
            else:
                text = ("<span style='color:#b07d00'>Not running</span>: presets and bindings can still be "
                        "edited; loading needs it running")
            self.state.setText(f"{text} <small>({where})</small>")
            self.start_btn.setVisible(not running)
            self.bypass.blockSignals(True)
            self.bypass.setChecked(bypassed)
            self.bypass.setEnabled(running)
            self.bypass.blockSignals(False)

        run_async(probe, show, lambda msg: self.state.setText(f"<span style='color:#d43333'>{esc(msg)}</span>"))

    def _start(self):
        self.ee.start()
        self.s.status.emit("Starting EasyEffects in the background…")
        QTimer.singleShot(3000, self.refresh)

    def _bypass(self, on):
        ee = self.ee
        run_async(lambda: ee.set_bypass(on),
                  lambda r: self.s.status.emit("Effects bypassed." if on else "Effects active."))

    # -- the chain --
    def _open_preset(self):
        name = self.preset.currentText()
        if name == NEW_PRESET:
            self.chain, self.raw = [], None
            self.name.clear()
        else:
            try:
                self.chain, self.raw = self.ee.read_preset(name)
            except (OSError, ValueError) as exc:
                self.s.status.emit(f"Could not read preset {name}: {exc}")
                return
            self.name.setText(name)
        self._render_chain()

    def _use_template(self):
        name = self.template.currentText()
        if self.chain and QMessageBox.question(
                self, "Replace chain", f"Replace the current chain with “{name}”?") != QMessageBox.StandardButton.Yes:
            return
        self.chain = effects.from_template(name)
        if not self.name.text().strip():
            self.name.setText(f"ASR {name}")
        self._render_chain(select=0)

    def _add(self):
        item = self.library.currentItem()
        if item is None:
            return
        kind = item.data(Qt.ItemDataRole.UserRole)
        entry = effects.new_entry(kind, self.chain)
        # A limiter belongs last; everything else goes before it.
        at = len(self.chain)
        if kind != "limiter":
            while at > 0 and self.chain[at - 1]["kind"] == "limiter":
                at -= 1
        self.chain.insert(at, entry)
        self._render_chain(select=at)

    def _remove(self):
        row = self.chain_list.currentRow()
        if 0 <= row < len(self.chain):
            del self.chain[row]
            self._render_chain(select=min(row, len(self.chain) - 1))

    def _move(self, delta):
        row = self.chain_list.currentRow()
        new = row + delta
        if 0 <= row < len(self.chain) and 0 <= new < len(self.chain):
            self.chain[row], self.chain[new] = self.chain[new], self.chain[row]
            self._render_chain(select=new)

    def _render_chain(self, select=None):
        self.chain_list.blockSignals(True)
        self.chain_list.clear()
        for entry in self.chain:
            text = effects.describe(entry)
            if entry["settings"].get("bypass"):
                text += "  (bypassed)"
            self.chain_list.addItem(text)
        self.chain_list.blockSignals(False)
        if select is not None and 0 <= select < len(self.chain):
            self.chain_list.setCurrentRow(select)
        else:
            self._select(-1)
        self._update_summary()

    def _update_summary(self):
        if not self.chain:
            self.summary.setText("<small>Empty chain. Pick a template or add effects.</small>")
            return
        steps = " → ".join(esc(effects.describe(e)) for e in self.chain)
        self.summary.setText(f"<small><b>Mic</b> → {steps} → <b>apps</b></small>")

    def _show_blurb(self, item, _prev=None):
        if item is not None and self.chain_list.currentRow() < 0:
            spec = EFFECTS[item.data(Qt.ItemDataRole.UserRole)]
            self.blurb.setText(f"<small>{esc(spec['blurb'])}</small>")

    def _select(self, row):
        while self.form.rowCount():
            self.form.removeRow(0)
        if not (0 <= row < len(self.chain)):
            self.blurb.setText("<small>Select an effect in the chain to change its settings.</small>")
            return
        entry = self.chain[row]
        spec = EFFECTS.get(entry["kind"])
        if spec is None:
            self.blurb.setText(f"<small><code>{esc(entry['id'])}</code> is not one of the speech effects "
                               "this tab edits. It is kept as it is; change it in EasyEffects.</small>")
            return
        self.blurb.setText(f"<small>{esc(spec['blurb'])}</small>")
        settings = entry["settings"]
        bypass = QCheckBox("Bypass this effect")
        bypass.setChecked(bool(settings.get("bypass")))
        bypass.toggled.connect(lambda on: self._set(row, "bypass", on))
        self.form.addRow("", bypass)
        for key, label, kind, lo, hi, default, unit in spec["params"]:
            value = effects.get_value(settings, key, default)
            if kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(value))
                w.toggled.connect(lambda on, key=key: self._set(row, key, on))
            elif isinstance(kind, list):
                w = QComboBox()
                w.addItems(kind)
                if w.findText(str(value)) < 0:
                    w.addItem(str(value))
                w.setCurrentText(str(value))
                w.currentTextChanged.connect(lambda text, key=key: self._set(row, key, text))
            else:
                w = QSpinBox() if kind == "int" else QDoubleSpinBox()
                if kind == "float":
                    w.setDecimals(3 if hi - lo < 1 else 1)
                    w.setSingleStep((hi - lo) / 100 if hi - lo < 1 else 1.0)
                w.setRange(lo, hi)
                w.setValue(int(value) if kind == "int" else float(value))
                if unit:
                    w.setSuffix(f" {unit}")
                w.valueChanged.connect(lambda v, key=key: self._set(row, key, v))
            self.form.addRow(label, w)

    def _set(self, row, key, value):
        effects.set_value(self.chain[row]["settings"], key, value)
        item = self.chain_list.item(row)
        if item is not None:
            entry = self.chain[row]
            item.setText(effects.describe(entry) + ("  (bypassed)" if entry["settings"].get("bypass") else ""))
        self._update_summary()

    def _save(self, load):
        name = self.name.text().strip()
        if not self.ee.installed:
            self.s.status.emit("EasyEffects is not installed.")
            return
        if not name or "/" in name:
            self.s.status.emit("Give the preset a name (no slashes).")
            return
        if not self.chain:
            self.s.status.emit("The chain is empty.")
            return
        if name != self.preset.currentText() and name in self.ee.presets() and QMessageBox.question(
                self, "Overwrite preset", f"An input preset “{name}” already exists. Overwrite it?"
                ) != QMessageBox.StandardButton.Yes:
            return
        raw = self.raw if name == self.preset.currentText() else None
        path = self.ee.write_preset(name, self.chain, raw)
        self.raw = self.ee.read_preset(name)[1]
        self.refresh()
        self.preset.setCurrentText(name)
        if not load:
            self.s.status.emit(f"Saved {path}")
            return
        ee = self.ee

        def done(result):
            ok, out = result
            self.s.status.emit(f"Loaded “{name}” in EasyEffects." if ok else f"EasyEffects: {out or 'load failed'}")
            self.refresh()

        if not ee.running():
            self.s.status.emit(f"Saved {path}. EasyEffects is not running: start it, then load.")
            return
        run_async(lambda: ee.load(name), done)

    # -- bindings --
    def _fill_bindings(self):
        rows = self.ee.bindings()
        presets = set(self.ee.presets())
        self.bind_table.setRowCount(len(rows))
        for i, b in enumerate(rows):
            self.bind_table.setItem(i, 0, _item(b.get("device-description") or b.get("device", ""), b["path"]))
            self.bind_table.setItem(i, 1, _item(b.get("device-profile", "")))
            name = b.get("preset-name", "")
            self.bind_table.setItem(i, 2, _item(name if name in presets else f"{name} (missing: nothing loads)"))
        self.bind_table.resizeColumnsToContents()

    def _update_bind_label(self):
        d = self.s.device
        self.bind_label.setText(
            f"Bind <b>{esc(d.description)}</b>{' · ' + esc(d.port) if d and d.port else ''} to" if d else "No microphone")

    def _bind(self):
        d, preset = self.s.device, self.bind_preset.currentText()
        if not d or not preset:
            self.s.status.emit("Pick a microphone in the header and a saved preset.")
            return
        path = self.ee.bind(d, preset)
        self._fill_bindings()
        self.s.status.emit(f"Bound {d.description} to “{preset}” ({path.name}).")

    def _unbind(self):
        row = self.bind_table.currentRow()
        if row < 0:
            return
        path = self.bind_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        preset = self.bind_table.item(row, 2).text().split(" (missing", 1)[0]
        if QMessageBox.question(self, "Remove binding",
                                f"Stop autoloading “{preset}” for this microphone? The preset itself stays."
                                ) == QMessageBox.StandardButton.Yes:
            self.ee.unbind(path)
            self._fill_bindings()
