"""Custom-painted Qt widgets: level meter, gain zone strip, live spectrum,
band chart, readiness badge, and the dynamic tray icon."""

import math

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap,
)
from PyQt6.QtWidgets import QSizePolicy, QSlider, QStyle, QStyleOptionSlider, QWidget

from .core import (
    METER_MIN_DB, RED_DB, TARGET_HI_DB, TARGET_LO_DB, ZONE_CAUTION_HI,
    ZONE_SAFE_HI, ZONE_SAFE_LO, zone_for_db,
)

C_TRACK = QColor(28, 28, 31)
C_LOW = QColor(59, 122, 214)
C_TARGET = QColor(46, 164, 79)
C_CAUTION = QColor(217, 163, 64)
C_DANGER = QColor(212, 51, 51)
C_HOLD = QColor(242, 242, 242)
C_QUIET = QColor(77, 107, 153)
ZONE_COLOURS = {"low": C_LOW, "safe": C_TARGET, "caution": C_CAUTION, "danger": C_DANGER}
ZONE_HEX = {"low": "#3b7ad6", "safe": "#2ea44f", "caution": "#d9a340", "danger": "#d43333"}

METER_BANDS = (
    (METER_MIN_DB, TARGET_LO_DB, C_QUIET, 0.30),
    (TARGET_LO_DB, TARGET_HI_DB, C_TARGET, 0.55),
    (TARGET_HI_DB, RED_DB, C_DANGER, 0.40),
    (RED_DB, 0.0, C_DANGER, 0.75),
)
BAND_CAPTIONS = (
    (METER_MIN_DB, TARGET_LO_DB, "too quiet"),
    (TARGET_LO_DB, TARGET_HI_DB, "good"),
    (TARGET_HI_DB, 0.0, "too loud"),
)


def alpha(colour, a):
    c = QColor(colour)
    c.setAlphaF(a)
    return c


def level_colour(db):
    if db > RED_DB:
        return C_DANGER
    if db > TARGET_HI_DB:
        return C_CAUTION
    if db >= TARGET_LO_DB:
        return C_TARGET
    return C_LOW


def db_frac(db, lo=METER_MIN_DB, hi=0.0):
    return max(0.0, min(1.0, (max(db, lo) - lo) / (hi - lo)))


class LevelMeter(QWidget):
    """Per-channel dBFS meters: RMS bar, peak-hold marker, banded track."""

    BAR_H = 22
    GAP = 5
    SCALE_H = 30
    LABEL_W = 82
    NUMBER_W = 150

    def __init__(self, compact=False, parent=None):
        super().__init__(parent)
        self.compact = compact
        if compact:
            self.BAR_H, self.LABEL_W, self.NUMBER_W, self.SCALE_H = 12, 10, 12, 16
        self.labels = ["Channel 1"]
        self.data = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._resize()

    def configure(self, labels):
        self.labels = labels or ["Channel 1"]
        self.data = []
        self._resize()
        self.update()

    def _resize(self):
        count = max(1, len(self.labels))
        self.setFixedHeight(count * (self.BAR_H + self.GAP) + self.SCALE_H)

    def set_data(self, data):
        self.data = data
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        ink = self.palette().windowText().color()
        width = self.width()
        track_x = self.LABEL_W
        track_w = max(60, width - self.LABEL_W - self.NUMBER_W)
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, self.font().pointSizeF() * 0.85))
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSizeF(small.pointSizeF())

        for row, name in enumerate(self.labels):
            y = row * (self.BAR_H + self.GAP)
            stats = self.data[row] if row < len(self.data) else None
            if not self.compact:
                p.setPen(alpha(ink, 0.85))
                p.setFont(small)
                p.drawText(QRectF(0, y, self.LABEL_W - 6, self.BAR_H),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name[:14])
            p.fillRect(QRectF(track_x, y, track_w, self.BAR_H), C_TRACK)
            for lo_db, hi_db, colour, a in METER_BANDS:
                lo, hi = db_frac(lo_db) * track_w, db_frac(hi_db) * track_w
                p.fillRect(QRectF(track_x + lo, y, hi - lo, self.BAR_H), alpha(colour, a))
            if stats is not None:
                bar = db_frac(stats["rms"]) * track_w
                p.fillRect(QRectF(track_x, y + 2, bar, self.BAR_H - 4), level_colour(stats["rms"]))
                if stats["hold"] > METER_MIN_DB:
                    hx = db_frac(stats["hold"]) * track_w
                    p.fillRect(QRectF(track_x + max(0, hx - 1), y, 2, self.BAR_H), C_HOLD)
            p.setPen(QPen(alpha(ink, 0.35), 1))
            p.drawRect(QRectF(track_x + 0.5, y + 0.5, track_w - 1, self.BAR_H - 1))
            if stats is not None and not self.compact:
                p.setPen(alpha(ink, 0.9))
                p.setFont(mono)
                p.drawText(QRectF(track_x + track_w + 8, y, self.NUMBER_W - 8, self.BAR_H),
                           Qt.AlignmentFlag.AlignVCenter,
                           f"{stats['rms']:6.1f}  pk {stats['hold']:6.1f}")

        base = len(self.labels) * (self.BAR_H + self.GAP)
        p.setFont(small)
        p.setPen(alpha(ink, 0.7))
        step = 20 if self.compact else 10
        for db in range(int(METER_MIN_DB), 1, step):
            x = track_x + db_frac(db) * track_w
            p.drawLine(QPointF(x, base), QPointF(x, base + 3))
            text = str(db)
            w = p.fontMetrics().horizontalAdvance(text)
            p.drawText(QPointF(x - w / 2, base + 3 + p.fontMetrics().ascent()), text)
        if self.compact:
            return
        p.drawText(QPointF(0, base + 3 + p.fontMetrics().ascent()), "dBFS")
        cy = base + 5 + 2 * p.fontMetrics().ascent()
        for lo_db, hi_db, caption in BAND_CAPTIONS:
            lo = track_x + db_frac(lo_db) * track_w
            hi = track_x + db_frac(hi_db) * track_w
            w = p.fontMetrics().horizontalAdvance(caption)
            if w < hi - lo - 4:
                p.drawText(QPointF((lo + hi - w) / 2, cy), caption)


class GainSlider(QSlider):
    """A horizontal slider over ALSA control indices whose groove is painted
    with this device's own safe / caution / danger zones, derived from its
    curve in dB — never from percentages."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.curve = None
        self.setMinimumHeight(44)
        self.setTickPosition(QSlider.TickPosition.NoTicks)

    def set_curve(self, curve):
        self.curve = curve
        self.update()

    def _groove(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderGroove, self)

    def _handle_len(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle, self).width()

    def x_for_index(self, index):
        groove = self._groove()
        handle = self._handle_len()
        span = max(1, self.maximum() - self.minimum())
        usable = groove.width() - handle
        return groove.x() + handle / 2 + usable * (index - self.minimum()) / span

    def paintEvent(self, event):
        if self.curve is not None:
            p = QPainter(self)
            groove = self._groove()
            steps = self.curve.imax - self.curve.imin + 1
            y = groove.center().y() - 11
            for n in range(steps):
                i = self.curve.imin + n
                x0 = self.x_for_index(i - 0.5) if n else groove.x()
                x1 = self.x_for_index(i + 0.5) if n < steps - 1 else groove.right()
                zone = zone_for_db(self.curve.db_drawable(i))
                p.fillRect(QRectF(x0, y, x1 - x0 + 0.6, 6), alpha(ZONE_COLOURS[zone], 0.85))
            ink = self.palette().windowText().color()
            small = QFont(self.font())
            small.setPointSizeF(max(7.0, self.font().pointSizeF() * 0.8))
            p.setFont(small)
            p.setPen(alpha(ink, 0.75))
            for db, text in ((ZONE_SAFE_LO, "0 dB"), (ZONE_SAFE_HI, f"+{ZONE_SAFE_HI:.0f}"),
                             (ZONE_CAUTION_HI, f"+{ZONE_CAUTION_HI:.0f}")):
                if self.curve.min_db <= db <= self.curve.max_db:
                    x = self.x_for_index(self.curve.index_for_db(db))
                    w = p.fontMetrics().horizontalAdvance(text)
                    p.drawText(QPointF(x - w / 2, groove.center().y() + 22), text)
            p.end()
        super().paintEvent(event)


class SpectrumView(QWidget):
    """Live log-frequency spectrum, with an optional overlay of the active
    room profile's noise bands and markers at mains hum frequencies."""

    LO_HZ, HI_HZ = 40.0, 16000.0
    LO_DB, HI_DB = -110.0, -10.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.freqs = None
        self.levels = None
        self.peak = None
        self.room_bands = None
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_room(self, bands):
        self.room_bands = bands
        self.update()

    def set_spectrum(self, freqs, levels):
        self.freqs, self.levels = freqs, levels
        if levels is not None:
            if self.peak is None or len(self.peak) != len(levels):
                self.peak = levels.copy()
            else:
                self.peak = [max(l, pk - 1.5) for l, pk in zip(levels, self.peak)]
        self.update()

    def _x(self, hz, rect):
        f = (math.log10(hz) - math.log10(self.LO_HZ)) / (math.log10(self.HI_HZ) - math.log10(self.LO_HZ))
        return rect.left() + max(0.0, min(1.0, f)) * rect.width()

    def _y(self, db, rect):
        f = (db - self.LO_DB) / (self.HI_DB - self.LO_DB)
        return rect.bottom() - max(0.0, min(1.0, f)) * rect.height()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        ink = self.palette().windowText().color()
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, self.font().pointSizeF() * 0.8))
        p.setFont(small)
        fm = p.fontMetrics()
        rect = QRectF(34, 4, self.width() - 40, self.height() - fm.height() - 10)
        p.fillRect(rect, C_TRACK)

        # Speech band shading: where ASR actually listens.
        p.fillRect(QRectF(self._x(300, rect), rect.top(),
                          self._x(3400, rect) - self._x(300, rect), rect.height()),
                   alpha(C_TARGET, 0.10))
        p.setPen(QPen(alpha(ink, 0.18), 1))
        for db in range(int(self.LO_DB), int(self.HI_DB) + 1, 20):
            y = self._y(db, rect)
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        p.setPen(alpha(ink, 0.7))
        for db in range(int(self.LO_DB) + 10, int(self.HI_DB) + 1, 20):
            y = self._y(db, rect)
            p.drawText(QPointF(0, y + fm.ascent() / 2), f"{db}")
        for hz, text in ((50, "50"), (100, "100"), (300, "300"), (1000, "1k"),
                         (3400, "3.4k"), (8000, "8k"), (16000, "16k")):
            x = self._x(hz, rect)
            p.setPen(QPen(alpha(ink, 0.18), 1))
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            p.setPen(alpha(ink, 0.7))
            w = fm.horizontalAdvance(text)
            p.drawText(QPointF(min(x - w / 2, rect.right() - w), rect.bottom() + fm.ascent() + 3), text)

        if self.room_bands:
            path = QPainterPath()
            first = True
            for band in self.room_bands:
                if band.get("db") is None or band["hz"] < self.LO_HZ:
                    continue
                # Band power spread over a 1/3 octave → per-line level, roughly.
                pt = QPointF(self._x(band["hz"], rect), self._y(band["db"] - 10, rect))
                if first:
                    path.moveTo(pt)
                    first = False
                else:
                    path.lineTo(pt)
            p.setPen(QPen(alpha(C_CAUTION, 0.9), 1.5, Qt.PenStyle.DashLine))
            p.drawPath(path)

        if self.freqs is not None and self.levels is not None:
            path = QPainterPath()
            path.moveTo(QPointF(rect.left(), rect.bottom()))
            for hz, db in zip(self.freqs, self.levels):
                path.lineTo(QPointF(self._x(hz, rect), self._y(db, rect)))
            path.lineTo(QPointF(rect.right(), rect.bottom()))
            path.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(alpha(C_LOW, 0.55)))
            p.drawPath(path)
            if self.peak is not None:
                peak = QPainterPath()
                for n, (hz, db) in enumerate(zip(self.freqs, self.peak)):
                    pt = QPointF(self._x(hz, rect), self._y(db, rect))
                    peak.moveTo(pt) if n == 0 else peak.lineTo(pt)
                p.setPen(QPen(alpha(C_HOLD, 0.6), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(peak)
        p.setPen(QPen(alpha(ink, 0.35), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(rect)


class BandChart(QWidget):
    """1/3-octave bars for a measurement, optionally against a reference
    (voice against room: the gap between them is the per-band SNR)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bands = None
        self.reference = None
        self.setMinimumHeight(140)

    def set_bands(self, bands, reference=None):
        self.bands, self.reference = bands, reference
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        ink = self.palette().windowText().color()
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, self.font().pointSizeF() * 0.75))
        p.setFont(small)
        fm = p.fontMetrics()
        rect = QRectF(34, 4, self.width() - 38, self.height() - fm.height() - 10)
        p.fillRect(rect, C_TRACK)
        if not self.bands:
            p.setPen(alpha(ink, 0.6))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "no measurement")
            return
        lo_db, hi_db = -110.0, -10.0

        def y(db):
            return rect.bottom() - max(0.0, min(1.0, (db - lo_db) / (hi_db - lo_db))) * rect.height()

        p.setPen(alpha(ink, 0.7))
        for db in range(-100, -9, 20):
            p.drawText(QPointF(0, y(db) + fm.ascent() / 2), str(db))
        n = len(self.bands)
        bw = rect.width() / n
        ref = {b["hz"]: b["db"] for b in (self.reference or []) if b.get("db") is not None}
        for i, band in enumerate(self.bands):
            x = rect.left() + i * bw
            if band.get("db") is not None:
                colour = C_TARGET if 300 <= band["hz"] <= 3400 else C_LOW
                p.fillRect(QRectF(x + 1, y(band["db"]), bw - 2, rect.bottom() - y(band["db"])), colour)
            if band["hz"] in ref:
                p.fillRect(QRectF(x + 1, y(ref[band["hz"]]) - 1, bw - 2, 3), C_CAUTION)
            if i % 3 == 0:
                hz = band["hz"]
                text = f"{hz / 1000:g}k" if hz >= 1000 else f"{hz:g}"
                p.setPen(alpha(ink, 0.7))
                p.drawText(QPointF(x, rect.bottom() + fm.ascent() + 3), text)
        p.setPen(QPen(alpha(ink, 0.35), 1))
        p.drawRect(rect)


class ScoreBadge(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.score = None
        self.grade = ""
        self.setFixedSize(QSize(92, 92))

    def set_score(self, score, grade=""):
        self.score, self.grade = score, grade
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        ink = self.palette().windowText().color()
        rect = QRectF(6, 6, 80, 80)
        p.setPen(QPen(alpha(ink, 0.2), 8))
        p.drawArc(rect, 0, 360 * 16)
        if self.score is None:
            p.setPen(alpha(ink, 0.6))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "—")
            return
        colour = C_TARGET if self.score >= 70 else C_CAUTION if self.score >= 50 else C_DANGER
        p.setPen(QPen(colour, 8, cap=Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 90 * 16, -int(360 * 16 * self.score / 100))
        big = QFont(self.font())
        big.setPointSizeF(self.font().pointSizeF() * 1.9)
        big.setBold(True)
        p.setFont(big)
        p.setPen(ink)
        p.drawText(rect.adjusted(0, -8, 0, -8), Qt.AlignmentFlag.AlignCenter, str(self.score))
        small = QFont(self.font())
        small.setPointSizeF(self.font().pointSizeF() * 0.8)
        p.setFont(small)
        p.drawText(rect.adjusted(0, 26, 0, 0), Qt.AlignmentFlag.AlignCenter, self.grade)


def tray_icon(state, level=None):
    """Microphone glyph with a status dot. `state`: idle | low | safe |
    caution | danger | clip | drift. `level`: optional 0..1 live bar."""
    size = 64
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    base = QIcon.fromTheme("audio-input-microphone-symbolic",
                           QIcon.fromTheme("audio-input-microphone"))
    if not base.isNull():
        base.paint(p, 2, 2, 50, 50)
    else:
        p.setPen(QPen(QColor(220, 220, 220), 5))
        p.drawRoundedRect(QRectF(18, 4, 18, 30), 9, 9)
        p.drawArc(QRectF(10, 14, 34, 30), 180 * 16, 180 * 16)
        p.drawLine(27, 44, 27, 54)
    colour = {
        "low": C_LOW, "safe": C_TARGET, "caution": C_CAUTION, "danger": C_DANGER,
        "clip": C_DANGER, "drift": C_CAUTION,
    }.get(state)
    if level is not None:
        h = max(2, int(52 * max(0.0, min(1.0, level))))
        p.fillRect(QRectF(54, 58 - h, 8, h), level_colour(METER_MIN_DB * (1 - level)))
    if colour is not None:
        p.setPen(QPen(QColor(20, 20, 20), 2))
        p.setBrush(colour)
        p.drawEllipse(QRectF(36, 36, 26, 26))
        if state in ("drift", "clip"):
            p.setPen(QPen(QColor(255, 255, 255), 4))
            p.drawLine(49, 41, 49, 52)
            p.drawPoint(49, 57)
    p.end()
    return QIcon(pix)
