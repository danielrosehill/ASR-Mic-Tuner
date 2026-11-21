"""Runs sync.sync off the GUI thread and reloads the session when profiles
arrive from the folder (v2.8, docs/profiles.md)."""

import threading

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import sync

DEBOUNCE_MS = 8000              # after a profile or binding changes
PERIODIC_MS = 60 * 60 * 1000    # hourly, for changes made on other machines


class SyncRunner(QObject):
    finished = pyqtSignal(dict)        # the report from sync.sync
    _done = pyqtSignal(dict)           # worker thread -> GUI thread

    def __init__(self, session):
        super().__init__()
        self.s = session
        self.last = None
        self.running = False
        self._again = False
        self._quiet = False
        self._done.connect(self._on_done)
        self.debounce = QTimer(singleShot=True, interval=DEBOUNCE_MS)
        self.debounce.timeout.connect(self.run)
        self.periodic = QTimer(interval=PERIODIC_MS)
        self.periodic.timeout.connect(self.run)
        self.periodic.start()
        session.presets_changed.connect(self._changed)
        session.settings_changed.connect(self._changed)
        QTimer.singleShot(3000, self.run)

    def enabled(self):
        st = self.s.settings
        return bool(st.get("sync_folder")) and st.get("sync_auto", True)

    def _changed(self):
        if not self._quiet and self.enabled():
            self.debounce.start()

    def run(self, force=False):
        if not (force or self.enabled()):
            return
        if self.running:
            self._again = True
            return
        self.running = True
        settings = dict(self.s.settings)
        threading.Thread(target=lambda: self._done.emit(sync.sync(settings)), daemon=True).start()

    def _on_done(self, report):
        self.running = False
        self.last = report
        if report.get("pulled") or any(d.startswith("local:") for d in report.get("deleted", [])):
            self._quiet = True           # our own reload is not a change to sync again
            try:
                self.s.reload_profiles()
            finally:
                self._quiet = False
        self.finished.emit(report)
        if self._again:
            self._again = False
            self.run()


def describe(report):
    if not report:
        return "Not synced yet."
    if report.get("error"):
        return f"Sync failed: {report['error']}"
    parts = [f"{len(report['pulled'])} in", f"{len(report['pushed'])} out"]
    if report.get("deleted"):
        parts.append(f"{len(report['deleted'])} removed")
    if report.get("conflicts"):
        parts.append(f"{len(report['conflicts'])} conflict(s), both copies kept")
    if report.get("git"):
        parts.append(report["git"])
    return f"Synced {report['at'][11:16]} UTC: " + ", ".join(parts) + "."
