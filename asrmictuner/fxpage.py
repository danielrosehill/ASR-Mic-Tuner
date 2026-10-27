"""ASR Effects tab: input chains for dictation, run inside PipeWire and
published as "ASR Mic" (engine.py, asrmic.py).

From the top: ASR Mic on/off and what feeds it, one-click quick setup from a
template, the optional downloadable denoisers, the chain editor (folded away),
and which chain each microphone uses. Saving or binding a chain takes effect
within a few seconds: the supervisor notices and restarts the chain.
"""

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from . import asrmic, engine
from .engine import EFFECTS, INPUT_MIX, LIBRARY, NOISE_MODES, PLUGINS, TEMPLATES, VAD_CAUTION
from .pages import _item, _table
from .qtutil import esc, run_async, wrap_label

NEW_CHAIN = "(new chain)"


class EffectsPage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.s = session
        self.chain = []

        root = QVBoxLayout(self)
        root.addWidget(wrap_label(
            "<small>Input-side clean-up for dictation, run inside PipeWire. The result is a separate input, "
            "<b>ASR Mic</b> (mono, 48 kHz); your raw microphone stays the default and is never altered. "
            "Set the analog gain first (Monitor or Measure tab): no effect can repair a clipped or buried "
            "signal.</small>"))

        # -- ASR Mic --
        micbox = QGroupBox("ASR Mic")
        al = QVBoxLayout(micbox)
        arow = QHBoxLayout()
        self.asr_mic = QCheckBox("Create ASR Mic")
        self.asr_mic.toggled.connect(self._asr_mic_toggled)
        arow.addWidget(self.asr_mic)
        self.asr_mic_pause = QPushButton("Pause")
        self.asr_mic_pause.setToolTip("Stop ASR Mic for now (a call, say). It starts again at login, "
                                      "or here, or from the tray menu.")
        self.asr_mic_pause.clicked.connect(self._asr_mic_pause)
        arow.addWidget(self.asr_mic_pause)
        self.bypass = QCheckBox("Bypass effects")
        self.bypass.setToolTip("ASR Mic carries the plain mic (mono) while this is on: for A/B comparison")
        self.bypass.setChecked(bool(self.s.settings.get("asr_mic_bypass")))
        self.bypass.toggled.connect(self._bypass)
        arow.addWidget(self.bypass)
        arow.addSpacing(12)
        arow.addWidget(QLabel("Noise reduction"))
        self.noise = QComboBox()
        for key, label, _kind, _s, tip in NOISE_MODES:
            self.noise.addItem(label, key)
            self.noise.setItemData(self.noise.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        self.noise.setToolTip("A denoiser laid over the bound chain, for when the room is loud. It replaces "
                              "any denoiser in the chain, so two never run together. Also in the tray menu.")
        self.noise.activated.connect(self._noise_chosen)
        arow.addWidget(self.noise)
        arow.addStretch(1)
        al.addLayout(arow)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Fed by"))
        self.source = QComboBox()
        self.source.activated.connect(self._source_chosen)
        srow.addWidget(self.source, 1)
        al.addLayout(srow)
        self.asr_mic_state = wrap_label()
        al.addWidget(self.asr_mic_state)
        root.addWidget(micbox)

        # -- quick setup --
        quick = QGroupBox("Quick setup")
        ql = QVBoxLayout(quick)
        row = QHBoxLayout()
        row.addWidget(QLabel("Chain"))
        self.quick_template = QComboBox()
        for name, blurb, _steps in TEMPLATES:
            self.quick_template.addItem(name)
            self.quick_template.setItemData(self.quick_template.count() - 1, blurb, Qt.ItemDataRole.ToolTipRole)
        row.addWidget(self.quick_template, 1)
        self.quick_button = QPushButton("Set up")
        self.quick_button.setToolTip("Save the chain, use it for this microphone, and turn ASR Mic on")
        self.quick_button.clicked.connect(self._quick_setup)
        row.addWidget(self.quick_button)
        ql.addLayout(row)
        self.quick_blurb = wrap_label()
        self.quick_template.currentIndexChanged.connect(self._quick_blurb)
        ql.addWidget(self.quick_blurb)
        root.addWidget(quick)

        # -- downloadable plugins --
        plugbox = QGroupBox("Neural denoisers (optional downloads)")
        pl = QVBoxLayout(plugbox)
        pl.addWidget(wrap_label(
            "<small>Fetched from the maintainers' own GitHub releases and checked against a pinned SHA-256 "
            "before use. Everything else is built into PipeWire.</small>"))
        self.plugin_rows = {}
        for key in PLUGINS:
            prow = QHBoxLayout()
            label = QLabel()
            prow.addWidget(label, 1)
            button = QPushButton("Get")
            button.clicked.connect(lambda _c=False, key=key: self._get_plugin(key))
            prow.addWidget(button)
            pl.addLayout(prow)
            self.plugin_rows[key] = (label, button)
        self.system_plugins = wrap_label()
        pl.addWidget(self.system_plugins)
        root.addWidget(plugbox)

        # -- chain editor, folded away until asked for --
        self.editbox = QGroupBox("Customise the chain")
        self.editbox.setCheckable(True)
        self.editbox.setChecked(False)
        outer = QVBoxLayout(self.editbox)
        self.editor = QWidget()
        el = QVBoxLayout(self.editor)
        el.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.editor)
        self.editor.setVisible(False)
        self.editbox.toggled.connect(self.editor.setVisible)

        top = QHBoxLayout()
        top.addWidget(QLabel("Chain"))
        self.saved = QComboBox()
        self.saved.activated.connect(lambda _i: self._open_saved())
        top.addWidget(self.saved, 2)
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
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Input channels"))
        self.input_mix = QComboBox()
        self.input_mix.addItems(INPUT_MIX)
        self.input_mix.setToolTip("ASR Mic is mono. Laptop arrays often have one better side.")
        mrow.addWidget(self.input_mix)
        mrow.addStretch(1)
        el.addLayout(mrow)

        cols = QHBoxLayout()
        lib = QVBoxLayout()
        lib.addWidget(QLabel("<b>Effects</b>"))
        self.library = QListWidget()
        for i, (name, _kind, _start, blurb) in enumerate(LIBRARY):
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setToolTip(blurb)
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
        self.chain_notes = wrap_label()
        self.chain_notes.hide()
        el.addWidget(self.chain_notes)
        save_row = QHBoxLayout()
        save_row.addWidget(QLabel("Save as"))
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Dictation")
        save_row.addWidget(self.name, 2)
        save = QPushButton("Save")
        save.clicked.connect(lambda: self._save(use=False))
        save_row.addWidget(save)
        save_use = QPushButton("Save and use for this mic")
        save_use.clicked.connect(lambda: self._save(use=True))
        save_row.addWidget(save_use)
        el.addLayout(save_row)
        root.addWidget(self.editbox)

        # -- bindings --
        bindbox = QGroupBox("Which chain each microphone uses")
        bl = QVBoxLayout(bindbox)
        bl.addWidget(wrap_label(
            "<small>ASR Mic runs the chain bound to the microphone that feeds it. Bindings are per port: "
            "the internal mic and a headset jack on the same card are separate.</small>"))
        self.bind_table = _table(["Microphone", "Port", "Chain"])
        self.bind_table.setMinimumHeight(90)
        self.bind_table.setMaximumHeight(160)
        bl.addWidget(self.bind_table)
        brow = QHBoxLayout()
        self.bind_label = QLabel()
        brow.addWidget(self.bind_label)
        self.bind_chain = QComboBox()
        brow.addWidget(self.bind_chain, 1)
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
        self.s.device_changed.connect(self._quick_blurb)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._asr_mic_status)
        self._shown_once = False
        self._quick_blurb()

    # -- lifecycle --
    def showEvent(self, event):
        super().showEvent(event)
        if not self._shown_once:
            self._shown_once = True
            self.refresh()
        self.timer.start(3000)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()

    def refresh(self):
        names = engine.chain_names()
        current = self.saved.currentText()
        self.saved.clear()
        self.saved.addItem(NEW_CHAIN)
        self.saved.addItems(names)
        if current and self.saved.findText(current) >= 0:
            self.saved.setCurrentText(current)
        self.bind_chain.clear()
        self.bind_chain.addItems(names)
        self.source.clear()
        self.source.addItem("Follow the default input", "")
        for d in self.s.devices:
            if d.is_hardware:
                self.source.addItem(d.description, d.name)
        idx = self.source.findData(self.s.settings.get("asr_mic_source", ""))
        self.source.setCurrentIndex(max(0, idx))
        for key, (label, button) in self.plugin_rows.items():
            ready, _ = engine.backend_ready(key)
            spec = PLUGINS[key]
            label.setText(f"{esc(spec['title'])}: " + ("<span style='color:#2ea44f'>installed</span>" if ready
                          else f"not installed (about {spec['size_mb'] or 1} MB)"))
            button.setVisible(not ready)
        notes = []
        for backend, what in (("ebur128", "Auto gain"), ("swh", "Gate, compressor and limiter")):
            ready, missing = engine.backend_ready(backend)
            if not ready:
                notes.append(f"{what} needs {esc(missing)}.")
        self.system_plugins.setText(f"<small style='color:#b07d00'>{' '.join(notes)}</small>" if notes else "")
        self._fill_bindings()
        self._update_bind_label()
        self._quick_blurb()
        self._render_chain()
        self._asr_mic_status()

    # -- ASR Mic --
    def _asr_mic_status(self):
        on = asrmic.enabled()
        self.asr_mic.blockSignals(True)
        self.asr_mic.setChecked(on)
        self.asr_mic.blockSignals(False)
        st = asrmic.state()
        self.asr_mic_pause.setVisible(on)
        self.asr_mic_pause.setText("Resume" if st == "paused" else "Pause")
        colour = {"active": "#2ea44f", "off": "#888"}.get(st, "#b07d00")
        text = f"<span style='color:{colour}'>{esc(st.capitalize())}</span>"
        mode = self.s.settings.get("noise_reduction", "off")
        self.noise.setCurrentIndex(max(0, self.noise.findData(mode)))
        info = asrmic.read_state()
        if on and info.get("device"):
            chain = info.get("chain")
            text += (f" · fed by <b>{esc(info.get('device_description') or info['device'])}</b> · "
                     + ("effects bypassed" if info.get("bypass") else
                        f"chain <b>{esc(chain)}</b>" if chain else "no chain bound (plain mic, mono)"))
            noise = next((m[1] for m in NOISE_MODES if m[0] == info.get("noise_reduction")), None)
            if noise and info.get("noise_reduction") != "off" and not info.get("bypass"):
                text += f" · noise reduction <b>{esc(noise)}</b>"
            if info.get("skipped"):
                text += (" · <span style='color:#d43333'>skipped, plugin missing: "
                         f"{esc(', '.join(EFFECTS[k]['name'] for k in info['skipped'] if k in EFFECTS))}</span>")
        self.asr_mic_state.setText(f"<small>{text}</small>")

    def _asr_mic_toggled(self, on):
        ok, message = asrmic.set_enabled(on)
        self.s.status.emit(message)
        QTimer.singleShot(2000, self._asr_mic_status)

    def _asr_mic_pause(self):
        resume = asrmic.state() == "paused"
        asrmic.set_running(resume)
        self.s.status.emit("ASR Mic resumed." if resume else "ASR Mic paused until login or Resume.")
        QTimer.singleShot(1500, self._asr_mic_status)

    def _bypass(self, on):
        self.s.settings["asr_mic_bypass"] = on
        self.s.save_settings()
        self.s.status.emit("ASR Mic: effects bypassed." if on else "ASR Mic: effects on.")

    def _noise_chosen(self, _i):
        self.s.settings["noise_reduction"] = self.noise.currentData()
        self.s.save_settings()
        self.s.status.emit(f"ASR Mic noise reduction: {self.noise.currentText()}.")
        QTimer.singleShot(4000, self._asr_mic_status)

    def _source_chosen(self, _i):
        self.s.settings["asr_mic_source"] = self.source.currentData() or ""
        self.s.save_settings()

    # -- plugins --
    def _get_plugin(self, key):
        label, button = self.plugin_rows[key]
        button.setEnabled(False)
        label.setText(f"{esc(PLUGINS[key]['title'])}: downloading…")

        def done(path):
            button.setEnabled(True)
            self.refresh()
            self.s.status.emit(f"Installed {path}")

        def failed(msg):
            button.setEnabled(True)
            label.setText(f"{esc(PLUGINS[key]['title'])}: <span style='color:#d43333'>{esc(msg)}</span>")

        run_async(lambda: engine.download(key), done, failed)

    # -- quick setup --
    def _quick_blurb(self, *_):
        name, blurb, _steps = TEMPLATES[self.quick_template.currentIndex()]
        chain = engine.from_template(name)
        flow = " → ".join(esc(engine.describe(e)) for e in chain) or "no effects"
        missing = sorted({engine.backend_ready(EFFECTS[e["kind"]]["backend"])[1] for e in chain
                          if not engine.backend_ready(EFFECTS[e["kind"]]["backend"])[0]})
        d = self.s.device
        target = (esc(d.description) + (f" · {esc(d.port)}" if d.port else "")) if d else "no microphone"
        text = (f"<small>{esc(blurb)}<br><b>Mic</b> → {flow} → <b>ASR Mic</b>. <b>Set up</b> saves it as "
                f"“{esc(name)}”, uses it for {target}, and turns ASR Mic on.")
        if missing:
            text += f"<br><span style='color:#b07d00'>Needs: {esc('; '.join(missing))}</span>"
        self.quick_blurb.setText(text + "</small>")

    def _quick_setup(self):
        d = self.s.device
        if d is None:
            self.s.status.emit("Pick a microphone in the header first.")
            return
        name = self.quick_template.currentText()
        engine.save_chain(name, engine.from_template(name))
        self._set_binding(d, name)
        if not asrmic.enabled():
            asrmic.set_enabled(True)
        self.s.status.emit(f"“{name}” is now the chain for {d.description}. ASR Mic follows within a few seconds.")
        self.refresh()
        QTimer.singleShot(4000, self._asr_mic_status)

    # -- the chain --
    def _open_saved(self):
        self.chain_notes.hide()
        name = self.saved.currentText()
        if name == NEW_CHAIN:
            self.chain = []
            self.name.clear()
            self.input_mix.setCurrentIndex(0)
        else:
            try:
                self.chain, mix = engine.load_chain(name)
            except (OSError, ValueError) as exc:
                self.s.status.emit(f"Could not read chain {name}: {exc}")
                return
            self.input_mix.setCurrentText(mix)
            self.name.setText(name)
        self._render_chain()

    def load_chain(self, chain, name, notes=None):
        """Open a chain built elsewhere (a personal chain from a mic test) for
        review: nothing is saved until the user presses Save."""
        self.editbox.setChecked(True)
        self.saved.setCurrentText(NEW_CHAIN)
        self.chain = chain
        self.name.setText(name)
        self._render_chain(select=0)
        if notes:
            self.chain_notes.setText("<small><b>Why this chain:</b><ul>" +
                                     "".join(f"<li>{esc(n)}</li>" for n in notes) + "</ul>Review, then "
                                     "<b>Save and use for this mic</b>.</small>")
            self.chain_notes.show()

    def _use_template(self):
        self.chain_notes.hide()
        name = self.template.currentText()
        if self.chain and QMessageBox.question(
                self, "Replace chain", f"Replace the current chain with “{name}”?") != QMessageBox.StandardButton.Yes:
            return
        self.chain = engine.from_template(name)
        if not self.name.text().strip():
            self.name.setText(name)
        self._render_chain(select=0)

    def _add(self):
        item = self.library.currentItem()
        if item is None:
            return
        _name, kind, start, _blurb = LIBRARY[item.data(Qt.ItemDataRole.UserRole)]
        entry = engine.new_entry(kind, start)
        at = len(self.chain)
        if kind != "limiter":                    # a limiter belongs last
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

    def _label(self, entry):
        text = engine.describe(entry)
        if entry.get("bypass"):
            text += "  (bypassed)"
        spec = EFFECTS.get(entry["kind"])
        if spec and not engine.backend_ready(spec["backend"])[0]:
            text += "  (plugin missing: skipped)"
        return text

    def _render_chain(self, select=None):
        self.chain_list.blockSignals(True)
        self.chain_list.clear()
        for entry in self.chain:
            self.chain_list.addItem(self._label(entry))
        self.chain_list.blockSignals(False)
        if select is not None and 0 <= select < len(self.chain):
            self.chain_list.setCurrentRow(select)
        else:
            self._select(-1)
        self._update_summary()

    def _update_summary(self):
        if not self.chain:
            self.summary.setText("<small><b>Mic</b> → mono → <b>ASR Mic</b>, no effects. "
                                 "Pick a template or add effects.</small>")
            return
        steps = " → ".join(esc(engine.describe(e)) for e in self.chain if not e.get("bypass"))
        text = f"<small><b>Mic</b> → mono → {steps} → <b>ASR Mic</b></small>"
        if any(engine.has_vad(e) for e in self.chain):
            text += f"<br><small style='color:#d43333'><b>VAD in this chain.</b> {esc(VAD_CAUTION)}</small>"
        self.summary.setText(text)

    def _show_blurb(self, item, _prev=None):
        if item is not None and self.chain_list.currentRow() < 0:
            self.blurb.setText(f"<small>{esc(LIBRARY[item.data(Qt.ItemDataRole.UserRole)][3])}</small>")

    def _select(self, row):
        while self.form.rowCount():
            self.form.removeRow(0)
        if not (0 <= row < len(self.chain)):
            self.blurb.setText("<small>Select an effect in the chain to change its settings.</small>")
            return
        entry = self.chain[row]
        spec = EFFECTS.get(entry["kind"])
        if spec is None:
            self.blurb.setText(f"<small>Unknown effect <code>{esc(entry['kind'])}</code>; it is skipped.</small>")
            return
        ready, missing = engine.backend_ready(spec["backend"])
        blurb = esc(spec["blurb"])
        if not ready:
            blurb += f"<br><span style='color:#d43333'>Needs {esc(missing)}. Skipped until then.</span>"
        self.blurb.setText(f"<small>{blurb}</small>")
        settings = entry["settings"]
        if engine.has_vad(entry):
            self.form.addRow(wrap_label(f"<small style='color:#d43333'><b>VAD is on.</b> {esc(VAD_CAUTION)}</small>"))
        bypass = QCheckBox("Bypass this effect")
        bypass.setChecked(bool(entry.get("bypass")))
        bypass.toggled.connect(lambda on: self._set_bypass(row, on))
        self.form.addRow("", bypass)
        for key, label, kind, lo, hi, default, unit in spec["params"]:
            value = settings.get(key, default)
            if kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(value))
                w.toggled.connect(lambda on, key=key: self._set(row, key, on))
            elif isinstance(kind, list):
                w = QComboBox()
                w.addItems(kind)
                w.setCurrentText(str(value))
                w.currentTextChanged.connect(lambda text, key=key: self._set(row, key, text))
            else:
                w = QSpinBox() if kind == "int" else QDoubleSpinBox()
                if kind == "float":
                    w.setDecimals(2 if hi - lo <= 5 else 1)
                    w.setSingleStep((hi - lo) / 100 if hi - lo <= 5 else 1.0)
                w.setRange(lo, hi)
                w.setValue(int(value) if kind == "int" else float(value))
                if unit:
                    w.setSuffix(f" {unit}")
                w.valueChanged.connect(lambda v, key=key: self._set(row, key, v))
            self.form.addRow(label, w)

    def _set(self, row, key, value):
        vad_before = engine.has_vad(self.chain[row])
        self.chain[row]["settings"][key] = value
        if engine.has_vad(self.chain[row]) != vad_before:
            QTimer.singleShot(0, lambda: self._select(row))   # show or drop the caution
        item = self.chain_list.item(row)
        if item is not None:
            item.setText(self._label(self.chain[row]))
        self._update_summary()

    def _set_bypass(self, row, on):
        self.chain[row]["bypass"] = on
        self.chain_list.item(row).setText(self._label(self.chain[row]))
        self._update_summary()

    def _save(self, use):
        name = self.name.text().strip()
        if not name or "/" in name:
            self.s.status.emit("Give the chain a name (no slashes).")
            return
        if name != self.saved.currentText() and name in engine.chain_names() and QMessageBox.question(
                self, "Overwrite chain", f"A chain “{name}” already exists. Overwrite it?"
                ) != QMessageBox.StandardButton.Yes:
            return
        engine.save_chain(name, self.chain, self.input_mix.currentText())
        if use and self.s.device:
            self._set_binding(self.s.device, name)
            if not asrmic.enabled():
                asrmic.set_enabled(True)
            self.s.status.emit(f"Saved “{name}” and bound it to {self.s.device.description}. "
                               "ASR Mic follows within a few seconds.")
        else:
            self.s.status.emit(f"Saved “{name}”.")
        self.refresh()
        self.saved.setCurrentText(name)

    # -- bindings --
    def _bindings(self):
        return dict(self.s.settings.get("chain_bindings") or {})

    def _set_binding(self, device, name):
        b = self._bindings()
        b[engine.binding_key(device.name, device.port)] = name
        self.s.settings["chain_bindings"] = b
        self.s.save_settings()

    def _fill_bindings(self):
        names = set(engine.chain_names())
        labels = {d.name: d.description for d in self.s.devices}
        rows = sorted(self._bindings().items())
        self.bind_table.setRowCount(len(rows))
        for i, (key, chain) in enumerate(rows):
            device, _, port = key.rpartition(":")
            self.bind_table.setItem(i, 0, _item(labels.get(device, device), key))
            self.bind_table.setItem(i, 1, _item(port))
            self.bind_table.setItem(i, 2, _item(chain if chain in names else f"{chain} (missing)"))
        self.bind_table.resizeColumnsToContents()

    def _update_bind_label(self):
        d = self.s.device
        self.bind_label.setText(
            f"Bind <b>{esc(d.description)}</b>{' · ' + esc(d.port) if d.port else ''} to" if d else "No microphone")

    def _bind(self):
        d, name = self.s.device, self.bind_chain.currentText()
        if not d or not name:
            self.s.status.emit("Pick a microphone in the header and a saved chain.")
            return
        self._set_binding(d, name)
        self._fill_bindings()
        self.s.status.emit(f"{d.description} now uses “{name}”.")

    def _unbind(self):
        row = self.bind_table.currentRow()
        if row < 0:
            return
        key = self.bind_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        b = self._bindings()
        b.pop(key, None)
        self.s.settings["chain_bindings"] = b
        self.s.save_settings()
        self._fill_bindings()
