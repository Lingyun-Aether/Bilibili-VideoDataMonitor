import re
import sys
import time
import os
import json
import csv
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Any

import requests

from PySide6.QtWidgets import QSlider
from PySide6.QtWidgets import QMessageBox
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Qt
from PySide6.QtGui import QColor, QBrush, QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QInputDialog,
    QToolBar,
    QTextEdit,
    QFileDialog,
)

import pyqtgraph as pg


# ===== Global session sequence (per program run) =====
_SESSION_SEQ = 0


def next_session_seq() -> int:
    global _SESSION_SEQ
    _SESSION_SEQ += 1
    return _SESSION_SEQ


# ===== Metrics =====
STAT_METRICS = [
    ("view", "播放量"),
    ("danmaku", "弹幕"),
    ("like", "点赞"),
    ("coin", "投币"),
    ("favorite", "收藏"),
    ("share", "转发"),
    ("reply", "评论"),
]
ALL_METRICS = STAT_METRICS + [("online", "在线人数")]  # 实时在线观看人数（若接口不可用将显示0/不变化）


BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"(?:av|AV)(\d+)")


@dataclass
class Sample:
    ts: float
    values: Dict[str, int]


def _int_or_zero(x) -> int:
    try:
        if isinstance(x, str):
            x = x.replace(",", "").strip()
            if x in {"--", ""}:
                return 0
        return int(x)
    except Exception:
        return 0


def fmt_group4_int(n: int) -> str:
    sign = "-" if n < 0 else ""
    s = str(abs(int(n)))
    groups = []
    while s:
        groups.append(s[-4:])
        s = s[:-4]
    return sign + " ".join(reversed(groups)) if groups else "0"


def fmt_group4_float(x: float, decimals: int = 2) -> str:
    sign = "-" if x < 0 else ""
    ax = abs(x)
    s = f"{ax:.{decimals}f}"
    ip, fp = s.split(".")
    ip_fmt = fmt_group4_int(int(ip))
    return f"{sign}{ip_fmt}.{fp}"


def safe_filename(s: str, max_len: int = 80) -> str:
    s = (s or "").strip()
    if not s:
        return "untitled"
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len] if len(s) > max_len else s


def resolve_b23(url: str, session: requests.Session) -> str:
    r = session.get(url, allow_redirects=True, timeout=10)
    return str(r.url)


def parse_video_id(text: str, session: requests.Session) -> Tuple[Optional[str], Optional[int]]:
    text = text.strip()
    if not text:
        return None, None

    m = BV_RE.search(text)
    if m:
        return m.group(1), None
    m = AV_RE.search(text)
    if m:
        return None, int(m.group(1))

    if "b23.tv" in text:
        text = resolve_b23(text, session)

    m = BV_RE.search(text)
    if m:
        return m.group(1), None
    m = AV_RE.search(text)
    if m:
        return None, int(m.group(1))

    return None, None


def parse_online_total_to_int(total: Any) -> Optional[int]:
    """
    B站在线人数接口 data.total 常见为 "1000+" / "1.2万+" / "0" / 0 等。
    转为 int；失败返回 None。
    """
    try:
        if total is None:
            return None
        if isinstance(total, (int, float)):
            return int(total)
        s = str(total).strip()
        if not s:
            return None
        s = s.replace("+", "")
        if s.endswith("万"):
            return int(float(s[:-1]) * 10000)
        return int(float(s))
    except Exception:
        return None


class BiliClient:
    def __init__(self):
        self.sess = requests.Session()
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }

    def fetch_view(self, bvid: Optional[str], aid: Optional[int], sessdata: Optional[str]) -> Tuple[Dict[str, int], Optional[int], Optional[str]]:
        if not bvid and not aid:
            raise ValueError("缺少 bvid/aid")

        url = "https://api.bilibili.com/x/web-interface/view"
        params = {"bvid": bvid} if bvid else {"aid": aid}

        cookies = {}
        if sessdata:
            cookies["SESSDATA"] = sessdata.strip()

        r = self.sess.get(url, params=params, headers=self.headers, cookies=cookies, timeout=10)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 0:
            raise RuntimeError(f"view接口 code={j.get('code')}, message={j.get('message')}")
        data = j.get("data") or {}
        stat = (data.get("stat") or {})

        values = {k: _int_or_zero(stat.get(k, 0)) for k, _cn in STAT_METRICS}

        cid = data.get("cid")
        if cid is None:
            pages = data.get("pages") or []
            if pages:
                cid = pages[0].get("cid")
        cid = int(cid) if cid is not None else None

        title = data.get("title")
        return values, cid, title

    def fetch_online(self, bvid: Optional[str], aid: Optional[int], cid: int, sessdata: Optional[str]) -> Optional[int]:
        url = "https://api.bilibili.com/x/player/online/total"
        params = {"cid": cid}
        if bvid:
            params["bvid"] = bvid
        elif aid:
            params["aid"] = aid
        else:
            return None

        cookies = {}
        if sessdata:
            cookies["SESSDATA"] = sessdata.strip()

        r = self.sess.get(url, params=params, headers=self.headers, cookies=cookies, timeout=10)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 0:
            return None
        data = j.get("data") or {}
        return parse_online_total_to_int(data.get("total"))


class FetchSignals(QObject):
    ok = Signal(object)   # payload: (values:dict, ts:float, cid:int|None, title:str|None)
    err = Signal(str)


class FetchTask(QRunnable):
    def __init__(self, client: BiliClient, bvid: Optional[str], aid: Optional[int], sessdata: Optional[str]):
        super().__init__()
        self.client = client
        self.bvid = bvid
        self.aid = aid
        self.sessdata = sessdata
        self.signals = FetchSignals()

    def run(self):
        try:
            ts = time.time()
            values, cid, title = self.client.fetch_view(self.bvid, self.aid, self.sessdata)

            online_val = None
            if cid is not None:
                try:
                    online_val = self.client.fetch_online(self.bvid, self.aid, cid, self.sessdata)
                except Exception:
                    online_val = None

            if online_val is not None:
                values["online"] = int(online_val)
            else:
                # 不抛错，交给上层决定是否沿用上一次/置0
                values["online"] = -1  # sentinel: unavailable this tick

            self.signals.ok.emit((values, ts, cid, title))
        except Exception as e:
            self.signals.err.emit(str(e))


class PlainNumberAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            vv = v * scale
            if abs(spacing) >= 1:
                out.append(fmt_group4_int(int(round(vv))))
            else:
                out.append(fmt_group4_float(float(vv), decimals=2))
        return out


class MonitorPanel(QWidget):
    EGG_THRESHOLDS = [32_500, 325_000, 3_250_000, 32_500_000]

    def __init__(self, title: str):
        super().__init__()
        self.title = title

        self.client = BiliClient()
        self.pool = QThreadPool.globalInstance()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self._autosave_tick)

        self.bvid: Optional[str] = None
        self.aid: Optional[int] = None
        self.cid: Optional[int] = None

        self.samples: deque[Sample] = deque()
        self._fetch_in_flight = False
        self.start_ts: Optional[float] = None  # t=0 原点（首次成功采样）

        self._theme_dark = True

        # online last-known (avoid graph jumping to 0 on transient failures)
        self._last_online: Optional[int] = None

        # milestones
        self._milestones_inited = False
        self._next_view_milestone: Optional[int] = None
        self._next_reply_milestone: Optional[int] = None
        self._egg_next_index: Dict[str, int] = {}

        # autosave session info
        self._autosave_enabled = False
        self._autosave_dir = ""
        self._autosave_fmt = "jsonl"
        self._autosave_path: Optional[str] = None
        self._session_seq: Optional[int] = None
        self._session_start_str: Optional[str] = None
        self._session_title_str: Optional[str] = None

        self._build_ui()
        self.apply_theme(True)

    def set_title(self, title: str):
        self.title = title

    def _on_trend_shift_changed(self, v: int):
        # v 单位：秒
        self.trend_shift_spin.setText(f"横移: {v}s")
        self._redraw()

    # ===== Logging =====
    def _ts_now_str(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime())

    def log(self, msg: str):
        self.log_view.append(f"[{self._ts_now_str()}] {msg}")

    def milestone(self, msg: str):
        self.ms_log.append(f"[{self._ts_now_str()}] {msg}")

    # ===== UI =====
    def _build_ui(self):
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        left = QVBoxLayout()
        right = QVBoxLayout()
        top.addLayout(left, 3)
        top.addLayout(right, 5)
        root.addLayout(top)

        # Control box
        ctrl = QGroupBox("控制")
        fl = QFormLayout(ctrl)

        self.url_in = QLineEdit()
        self.url_in.setPlaceholderText("粘贴视频链接，或直接输入 BV/av（回车自动开始）")
        self.url_in.returnPressed.connect(self.start)
        fl.addRow("视频链接/BV/av", self.url_in)

        self.interval = QSpinBox()
        self.interval.setRange(1, 3600)
        self.interval.setValue(5)  # default 5s
        self.interval.setSuffix(" s")
        fl.addRow("抓取间隔", self.interval)

        self.sessdata_in = QLineEdit()
        self.sessdata_in.setPlaceholderText("可选：填 Cookie 里的 SESSDATA（用于需要登录的视频）")
        fl.addRow("SESSDATA(可选)", self.sessdata_in)

        # Autosave row
        self.autosave_chk = QCheckBox("每10分钟自动保存")
        self.autosave_fmt = QComboBox()
        self.autosave_fmt.addItems(["csv", "jsonl"])
        row_as = QHBoxLayout()
        row_as.addWidget(self.autosave_chk)
        row_as.addSpacing(8)
        row_as.addWidget(QLabel("格式"))
        row_as.addWidget(self.autosave_fmt)
        row_as.addStretch(1)
        fl.addRow("自动保存", row_as)

        self.autosave_dir_in = QLineEdit()
        self.autosave_dir_in.setPlaceholderText("选择保存目录（例如 D:/bili_logs）")
        self.autosave_pick_btn = QPushButton("选择目录")
        row_dir = QHBoxLayout()
        row_dir.addWidget(self.autosave_dir_in)
        row_dir.addWidget(self.autosave_pick_btn)
        fl.addRow("保存目录", row_dir)

        self.autosave_pick_btn.clicked.connect(self.pick_autosave_dir)
        self.autosave_chk.stateChanged.connect(self.toggle_autosave)

        # Buttons
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("开始")
        self.stop_btn = QPushButton("停止")
        self.import_btn = QPushButton("导入数据")
        self.export_csv_btn = QPushButton("导出CSV")
        self.export_jsonl_btn = QPushButton("导出JSONL")
        self.stop_btn.setEnabled(False)

        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.import_btn)
        btn_row.addWidget(self.export_csv_btn)
        btn_row.addWidget(self.export_jsonl_btn)
        fl.addRow(btn_row)

        self.status = QLabel("状态：未开始")
        self.status.setWordWrap(True)
        fl.addRow(self.status)

        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self.import_btn.clicked.connect(self.import_data_file)
        self.export_csv_btn.clicked.connect(self.export_csv)
        self.export_jsonl_btn.clicked.connect(self.export_jsonl)

        left.addWidget(ctrl)

        # Table
        table_box = QGroupBox("当前数据流状态（↑红-正增长/↓绿-负增长）")
        tb_l = QVBoxLayout(table_box)
        self.table = QTableWidget(len(ALL_METRICS), 6)
        self.table.setHorizontalHeaderLabels(["当前", "10s内", "1min内", "10min内", "30min内", "1h内"])
        self.table.setVerticalHeaderLabels([cn for _k, cn in ALL_METRICS])
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        tb_l.addWidget(self.table)
        left.addWidget(table_box)

        # Milestones + Logs (side-by-side)
        ms_box = QGroupBox("节点里程碑")
        ms_l = QVBoxLayout(ms_box)
        self.ms_log = QTextEdit()
        self.ms_log.setReadOnly(True)
        self.btn_clear_ms = QPushButton("清空")
        self.btn_clear_ms.clicked.connect(self.ms_log.clear)
        ms_l.addWidget(self.ms_log)
        ms_l.addWidget(self.btn_clear_ms)

        log_box = QGroupBox("日志")
        log_l = QVBoxLayout(log_box)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.btn_clear_log = QPushButton("清空")
        self.btn_clear_log.clicked.connect(self.log_view.clear)
        log_l.addWidget(self.log_view)
        log_l.addWidget(self.btn_clear_log)

        row_ml = QHBoxLayout()
        row_ml.addWidget(ms_box, 1)
        row_ml.addWidget(log_box, 1)
        left.addLayout(row_ml)

        # Chart
        chart_box = QGroupBox("数据可视化：折线图")
        chart_l = QVBoxLayout(chart_box)

        row1 = QHBoxLayout()
        self.metric_sel = QComboBox()
        for k, cn in ALL_METRICS:
            self.metric_sel.addItem(cn, userData=k)

        self.mode_sel = QComboBox()
        self.mode_sel.addItems(["变化量(每次采样Δ)", "变化率(/min)", "绝对值"])

        row1.addWidget(QLabel("数据标签"))
        row1.addWidget(self.metric_sel)
        row1.addSpacing(10)
        row1.addWidget(QLabel("数据类型"))
        row1.addWidget(self.mode_sel)
        row1.addStretch(1)
        chart_l.addLayout(row1)

        row_trend = QHBoxLayout()

        # 左侧：趋势线选项
        row_trend.addWidget(QLabel("趋势"))
        self.chk_trend = QCheckBox("趋势线")
        self.trend_mode = QComboBox()
        self.trend_mode.addItems(["EMA", "滑动平均", "线性回归"])

        self.trend_param = QSpinBox()
        self.trend_param.setRange(2, 10000)
        self.trend_param.setValue(50)
        self.trend_param.setSuffix(" 点")

        row_trend.addWidget(self.chk_trend)
        row_trend.addWidget(self.trend_mode)
        row_trend.addWidget(self.trend_param)

        row_trend.addSpacing(12)

        # 右侧：横向修正（作为最后一项，slider 占满剩余宽度）
        row_trend.addWidget(QLabel("横向修正"))

        self.trend_shift = QSlider(Qt.Horizontal)
        self.trend_shift.setRange(-6000, 6000)  # 秒
        self.trend_shift.setValue(0)
        self.trend_shift.setSingleStep(5)
        self.trend_shift.setPageStep(30)

        self.trend_shift_spin = QSpinBox()
        self.trend_shift_spin.setRange(-6000, 6000)     # 与slider一致，单位秒
        self.trend_shift_spin.setValue(0)
        self.trend_shift_spin.setSuffix(" s")
        self.trend_shift_spin.setFixedWidth(110)        # 让它像“数字区域”，可按需调整宽度
        row_trend.addWidget(self.trend_shift_spin)

        row_trend.addWidget(self.trend_shift, 1)
        row_trend.addWidget(self.trend_shift_spin)

        chart_l.addLayout(row_trend)

        self.chk_trend.stateChanged.connect(self._redraw)
        self.trend_mode.currentIndexChanged.connect(self._redraw)
        self.trend_param.valueChanged.connect(self._redraw)
        self.trend_shift.valueChanged.connect(self._on_trend_shift_slider_changed)
        self.trend_shift_spin.valueChanged.connect(self._on_trend_shift_spin_changed)

        row3 = QHBoxLayout()
        self.zoom_mode = QComboBox()
        self.zoom_mode.addItems(["双轴缩放", "仅X缩放", "仅Y缩放"])

        self.chk_points = QCheckBox("打点")
        self.chk_auto_y = QCheckBox("自适应缩放(Y)")
        self.chk_follow_x = QCheckBox("跟随最新(全量)")
        self.chk_zero_base = QCheckBox("以起始值为0(仅绝对值)")

        self.chk_auto_y.setChecked(True)
        self.chk_follow_x.setChecked(True)

        self.btn_reset_view = QPushButton("重置视图")
        self.btn_reset_view.clicked.connect(self.reset_view)

        row3.addWidget(QLabel("缩放"))
        row3.addWidget(self.zoom_mode)
        row3.addSpacing(10)
        row3.addWidget(self.chk_points)
        row3.addWidget(self.chk_auto_y)
        row3.addWidget(self.chk_follow_x)
        row3.addWidget(self.chk_zero_base)
        row3.addStretch(1)
        row3.addWidget(self.btn_reset_view)
        chart_l.addLayout(row3)

        self.plot = pg.PlotWidget(axisItems={"left": PlainNumberAxis(orientation="left")})
        self.plot.setLabel("bottom", "时间", units="min (t=0→)")
        self.plot.setLabel("left", "数值")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.curve = self.plot.plot([], [])
        self.trend_curve = self.plot.plot([], [], pen=pg.mkPen("red", width=2))
        self.trend_curve.setZValue(10)  # 让趋势线在上层
        chart_l.addWidget(self.plot)

        vb = self.plot.getViewBox()
        vb.setLimits(xMin=-1.0)  # 负X只露一点点
        vb.sigRangeChangedManually.connect(self._on_user_manual_range)

        right.addWidget(chart_box)

        # signals
        self.metric_sel.currentIndexChanged.connect(self._redraw)
        self.mode_sel.currentIndexChanged.connect(self._redraw)
        self.chk_points.stateChanged.connect(self._redraw)
        self.chk_zero_base.stateChanged.connect(self._redraw)
        self.chk_auto_y.stateChanged.connect(self._redraw)
        self.chk_follow_x.stateChanged.connect(self._redraw)
        self.zoom_mode.currentIndexChanged.connect(self._apply_zoom_mode)

        self._apply_zoom_mode()

    def _on_trend_shift_slider_changed(self, v: int):
        # slider -> spin
        if hasattr(self, "trend_shift_spin") and self.trend_shift_spin.value() != v:
            self.trend_shift_spin.blockSignals(True)
            self.trend_shift_spin.setValue(v)
            self.trend_shift_spin.blockSignals(False)
        self._redraw()

    def _on_trend_shift_spin_changed(self, v: int):
        # spin -> slider
        if hasattr(self, "trend_shift") and self.trend_shift.value() != v:
            self.trend_shift.blockSignals(True)
            self.trend_shift.setValue(v)
            self.trend_shift.blockSignals(False)
        self._redraw()

    def _on_user_manual_range(self, *args):
        if self.chk_follow_x.isChecked():
            self.chk_follow_x.setChecked(False)
        if self.chk_auto_y.isChecked():
            self.chk_auto_y.setChecked(False)

    def _apply_zoom_mode(self):
        mode = self.zoom_mode.currentText()
        vb = self.plot.getViewBox()
        if mode == "仅X缩放":
            vb.setMouseEnabled(x=True, y=False)
        elif mode == "仅Y缩放":
            vb.setMouseEnabled(x=False, y=True)
        else:
            vb.setMouseEnabled(x=True, y=True)

    def apply_theme(self, dark: bool):
        self._theme_dark = dark
        if dark:
            bg = "#0f0f0f"
            fg = "#e6e6e6"
            grid_alpha = 0.25
        else:
            bg = "#ffffff"
            fg = "#111111"
            grid_alpha = 0.15

        self.plot.setBackground(bg)
        for ax in ("left", "bottom"):
            axis = self.plot.getAxis(ax)
            axis.setPen(pg.mkPen(fg))
            axis.setTextPen(pg.mkPen(fg))
        self.plot.showGrid(x=True, y=True, alpha=grid_alpha)

    def reset_view(self):
        self.chk_follow_x.setChecked(True)
        self.chk_auto_y.setChecked(True)
        self._redraw(force_follow=True, force_autoy=True)

    # ===== Autosave =====
    def pick_autosave_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择自动保存目录", self.autosave_dir_in.text().strip() or "")
        if d:
            self.autosave_dir_in.setText(d)

    def toggle_autosave(self):
        self._autosave_enabled = self.autosave_chk.isChecked()
        self._autosave_dir = self.autosave_dir_in.text().strip()
        self._autosave_fmt = self.autosave_fmt.currentText().strip().lower() or "jsonl"

        if not self._autosave_enabled:
            self.autosave_timer.stop()
            self.log("自动保存：已关闭")
            return

        if not self._autosave_dir:
            self.autosave_chk.setChecked(False)
            self._autosave_enabled = False
            self.status.setText("状态：请先选择自动保存目录")
            self.log("自动保存：未启用（未设置目录）")
            return

        # 若当前已在采集，立刻准备路径并启动计时器
        if self.start_ts is not None:
            self._ensure_autosave_path()
            self.autosave_timer.start(10 * 60 * 1000)
            self.log(f"自动保存：已启用（每10分钟，覆盖写入 {os.path.basename(self._autosave_path or '')}）")
        else:
            self.log("自动保存：已启用（将在开始采集后生效）")

    def _ensure_autosave_path(self):
        if self._autosave_path and self._session_start_str and self._session_title_str and self._session_seq:
            return

        if self.start_ts is None:
            return

        if self._session_seq is None:
            self._session_seq = next_session_seq()

        if self._session_start_str is None:
            self._session_start_str = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.start_ts))

        if self._session_title_str is None:
            self._session_title_str = safe_filename(self.title)

        ext = self._autosave_fmt if self._autosave_fmt in ("csv", "jsonl") else "jsonl"
        fname = f"{self._session_start_str}_{self._session_title_str}_{self._session_seq:03d}.{ext}"
        self._autosave_path = os.path.join(self._autosave_dir, fname)

    def _autosave_tick(self):
        if not self._autosave_enabled:
            return
        if not self.samples:
            self.log("自动保存：跳过（无数据）")
            return
        if not self._autosave_dir:
            self.log("自动保存：跳过（目录为空）")
            return
        if self.start_ts is None:
            self.log("自动保存：跳过（未开始采集）")
            return

        self._autosave_fmt = self.autosave_fmt.currentText().strip().lower() or self._autosave_fmt
        self._autosave_dir = self.autosave_dir_in.text().strip() or self._autosave_dir
        self._ensure_autosave_path()

        try:
            if self._autosave_fmt == "csv":
                self._write_csv(self._autosave_path, overwrite=True)
            else:
                self._write_jsonl(self._autosave_path, overwrite=True)
            self.log(f"自动保存：已写入（覆盖）{os.path.basename(self._autosave_path or '')}")
        except Exception as e:
            self.log(f"自动保存：失败：{e}")

    # ===== Session start/stop =====
    def _reset_milestones(self, start_values: Dict[str, int]):
        step = 100_000
        v0 = int(start_values.get("view", 0))
        r0 = int(start_values.get("reply", 0))
        self._next_view_milestone = (v0 // step + 1) * step
        self._next_reply_milestone = (r0 // step + 1) * step

        self._egg_next_index.clear()
        for k, _cn in ALL_METRICS:
            s0 = int(start_values.get(k, 0))
            idx = 0
            while idx < len(self.EGG_THRESHOLDS) and s0 >= self.EGG_THRESHOLDS[idx]:
                idx += 1
            self._egg_next_index[k] = idx

        self._milestones_inited = True

    def start(self):
        bvid, aid = parse_video_id(self.url_in.text(), self.client.sess)
        if not bvid and not aid:
            self.status.setText("状态：无法从输入中解析 BV/av")
            self.log("开始：解析失败（输入无法识别BV/av）")
            return

        self.bvid, self.aid = bvid, aid
        self.cid = None
        self.samples.clear()
        self._fetch_in_flight = False
        self.start_ts = None
        self._last_online = None

        # reset milestones/logs for new session
        self.ms_log.clear()
        self._milestones_inited = False
        self._next_view_milestone = None
        self._next_reply_milestone = None
        self._egg_next_index.clear()

        # autosave session identity reset (new session => new filename base)
        self._autosave_path = None
        self._session_seq = None
        self._session_start_str = None
        self._session_title_str = None

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("状态：运行中")

        self.log(f"开始：{bvid or ('av' + str(aid))}（interval={self.interval.value()}s）")

        self._tick()
        self.timer.start(int(self.interval.value() * 1000))

    def stop(self):
        self.timer.stop()
        self.autosave_timer.stop()

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText("状态：已停止")
        self.log("停止：已停止抓取/自动保存")

    # ===== Fetch loop =====
    def _tick(self):
        if self._fetch_in_flight:
            return
        if not self.bvid and not self.aid:
            return

        self._fetch_in_flight = True
        task = FetchTask(
            self.client,
            self.bvid,
            self.aid,
            self.sessdata_in.text().strip() or None,
        )
        task.signals.ok.connect(self._on_data_payload)
        task.signals.err.connect(self._on_err)
        self.pool.start(task)

    def _on_err(self, msg: str):
        self._fetch_in_flight = False
        self.status.setText(f"状态：错误：{msg}")
        self.log(f"网络/接口错误：{msg}")

    def _on_data_payload(self, payload: object):
        values, ts, cid, title = payload  # type: ignore
        self._fetch_in_flight = False

        if self.start_ts is None:
            self.start_ts = ts  # t=0
            if cid is not None:
                self.cid = cid

            # initialize milestones from the very first sample (do NOT record already-reached)
            v_init = dict(values)
            if v_init.get("online", -1) == -1:
                v_init["online"] = 0
            self._reset_milestones(v_init)

            # autosave start (use start time/title/seq fixed for this session)
            if self._autosave_enabled and (self.autosave_dir_in.text().strip() or self._autosave_dir):
                self._autosave_dir = self.autosave_dir_in.text().strip() or self._autosave_dir
                self._autosave_fmt = self.autosave_fmt.currentText().strip().lower() or self._autosave_fmt
                self._ensure_autosave_path()
                self.autosave_timer.start(10 * 60 * 1000)
                self.log(f"自动保存：已启动（覆盖写入 {os.path.basename(self._autosave_path or '')}）")

        # update cid if later discovered
        if self.cid is None and cid is not None:
            self.cid = cid

        # online sentinel handling
        if int(values.get("online", -1)) == -1:
            if self._last_online is not None:
                values["online"] = self._last_online
            else:
                values["online"] = 0
        else:
            self._last_online = int(values.get("online", 0))

        # store
        self.samples.append(Sample(ts=ts, values={k: int(values.get(k, 0)) for k, _cn in ALL_METRICS}))

        # UI updates
        self._update_table()
        self._check_milestones(values, ts)
        self._redraw()

        t_str = time.strftime("%H:%M:%S", time.localtime(ts))
        self.status.setText(f"状态：运行中（最后更新于： {t_str}）")

    # ===== Milestones =====
    def _check_milestones(self, values: dict, ts: float):
        if not self._milestones_inited:
            return

        # 10万节点（播放量/评论）
        step = 100_000
        v = int(values.get("view", 0))
        r = int(values.get("reply", 0))

        while self._next_view_milestone is not None and v >= self._next_view_milestone:
            self.milestone(f"播放量 达到 {fmt_group4_int(self._next_view_milestone)} (10万节点)")
            self._next_view_milestone += step

        while self._next_reply_milestone is not None and r >= self._next_reply_milestone:
            self.milestone(f"评论数 达到 {fmt_group4_int(self._next_reply_milestone)} (10万节点)")
            self._next_reply_milestone += step

        # 彩蛋阈值（任一数据）
        for k, cn in ALL_METRICS:
            cur = int(values.get(k, 0))
            idx = int(self._egg_next_index.get(k, 0))
            while idx < len(self.EGG_THRESHOLDS) and cur >= self.EGG_THRESHOLDS[idx]:
                self.milestone(f"{cn} 达到 {fmt_group4_int(self.EGG_THRESHOLDS[idx])} ")
                idx += 1
            self._egg_next_index[k] = idx

    # ===== Table =====
    def _delta(self, metric: str, seconds: int) -> Optional[int]:
        if not self.samples:
            return None
        now_ts = self.samples[-1].ts
        target = now_ts - seconds

        base = None
        for s in reversed(self.samples):
            if s.ts <= target:
                base = s.values.get(metric)
                break
        if base is None:
            return None

        cur = self.samples[-1].values.get(metric, 0)
        return int(cur) - int(base)

    def _delta_item(self, v: Optional[int]) -> QTableWidgetItem:
        if v is None:
            it = QTableWidgetItem("-")
            it.setTextAlignment(Qt.AlignVCenter | Qt.AlignRight)
            return it

        if v > 0:
            it = QTableWidgetItem(f"{fmt_group4_int(v)} ↑")
            it.setForeground(QBrush(QColor(220, 0, 0)))
        elif v < 0:
            it = QTableWidgetItem(f"{fmt_group4_int(abs(v))} ↓")
            it.setForeground(QBrush(QColor(0, 160, 0)))
        else:
            it = QTableWidgetItem("0")

        it.setTextAlignment(Qt.AlignVCenter | Qt.AlignRight)
        return it

    def _update_table(self):
        if not self.samples:
            return
        cur = self.samples[-1].values

        for r, (k, _cn) in enumerate(ALL_METRICS):
            cur_item = QTableWidgetItem(fmt_group4_int(int(cur.get(k, 0))))
            cur_item.setTextAlignment(Qt.AlignVCenter | Qt.AlignRight)
            self.table.setItem(r, 0, cur_item)

            self.table.setItem(r, 1, self._delta_item(self._delta(k, 10)))
            self.table.setItem(r, 2, self._delta_item(self._delta(k, 60)))
            self.table.setItem(r, 3, self._delta_item(self._delta(k, 600)))
            self.table.setItem(r, 4, self._delta_item(self._delta(k, 1800)))
            self.table.setItem(r, 5, self._delta_item(self._delta(k, 3600)))

    # ===== Plot =====
    def _series(self) -> Tuple[list, list]:
        if len(self.samples) < 1 or self.start_ts is None:
            return [], []

        metric = self.metric_sel.currentData()
        mode = self.mode_sel.currentText()

        xs = [(s.ts - self.start_ts) / 60.0 for s in self.samples]  # t=0→

        if mode == "绝对值":
            ys = [s.values.get(metric, 0) for s in self.samples]
            if self.chk_zero_base.isChecked() and ys:
                base = ys[0]
                ys = [y - base for y in ys]
            return xs, ys

        if len(self.samples) < 2:
            return xs, [0]

        ys = [0]
        for i in range(1, len(self.samples)):
            prev, cur = self.samples[i - 1], self.samples[i]
            dv = cur.values.get(metric, 0) - prev.values.get(metric, 0)
            dt = max(cur.ts - prev.ts, 1e-6)
            if mode == "变化率(/min)":
                ys.append(dv / dt * 60.0)
            else:
                ys.append(dv)
        return xs, ys
    
    #EMA趋势线算法
    def _ema(self, ys: list, period: int) -> list:
            if not ys:
                return []
            period = max(1, int(period))
            alpha = 2.0 / (period + 1.0)
            out = [float(ys[0])]
            for i in range(1, len(ys)):
                out.append(alpha * float(ys[i]) + (1.0 - alpha) * out[-1])
            return out

    #SMA趋势线算法
    def _sma(self, ys: list, window: int) -> list:
        if not ys:
            return []
        window = max(1, int(window))
        out = []
        s = 0.0
        q = deque()
        for y in ys:
            fy = float(y)
            q.append(fy)
            s += fy
            if len(q) > window:
                s -= q.popleft()
            out.append(s / len(q))
        return out

    #线性回归算法
    def _linreg(self, xs: list, ys: list) -> list:
            # 简单最小二乘：y = a*x + b
            n = min(len(xs), len(ys))
            if n < 2:
                return ys[:]
            sx = sy = sxx = sxy = 0.0
            for i in range(n):
                x = float(xs[i])
                y = float(ys[i])
                sx += x
                sy += y
                sxx += x * x
                sxy += x * y
            den = n * sxx - sx * sx
            if abs(den) < 1e-12:
                return [float(ys[0])] * n
            a = (n * sxy - sx * sy) / den
            b = (sy - a * sx) / n
            return [a * float(xs[i]) + b for i in range(n)]

    def _apply_y_limiter(self, ys: list):
        if not ys:
            return
        y_min = float(min(ys))
        y_max = float(max(ys))
        rng = max(y_max - y_min, 1.0)

        if y_min >= 0:
            margin = max(1.0, max(abs(y_max), 1.0) * 0.02)
            y_limit = -margin
        else:
            y_limit = y_min - rng * 0.10

        vb = self.plot.getViewBox()
        vb.setLimits(yMin=float(y_limit))

    def _redraw(self, force_follow: bool = False, force_autoy: bool = False):
        xs, ys = self._series()
        if not xs:
            self.curve.setData([], [])
            return

        if self.chk_points.isChecked():
            self.curve.setData(xs, ys, symbol="o", symbolSize=6)
        else:
            self.curve.setData(xs, ys, symbol=None)
            try:
                self.curve.setSymbol(None)
            except Exception:
                pass

        self._apply_y_limiter(ys)

        # --- 趋势线 ---
        if hasattr(self, "trend_curve"):
            if self.chk_trend.isChecked():
                mode = self.trend_mode.currentText()
                p = int(self.trend_param.value())
                if mode == "EMA":
                    ty = self._ema(ys, p)
                elif mode == "滑动平均":
                    ty = self._sma(ys, p)
                else:
                    ty = self._linreg(xs, ys)

                # 趋势线不打点
                shift_sec = int(self.trend_shift_spin.value()) if hasattr(self, "trend_shift") else 0
                shift_min = shift_sec / 60.0
                xs2 = [x + shift_min for x in xs]
                self.trend_curve.setData(xs2, ty)
            else:
                # 关闭时清空
                self.trend_curve.setData([], [])

        follow = self.chk_follow_x.isChecked() or force_follow
        if follow:
            x_max = xs[-1]
            self.plot.setXRange(0.0, x_max, padding=0)

        autoy = self.chk_auto_y.isChecked() or force_autoy
        if autoy and ys:
            y_min = min(ys)
            y_max = max(ys)
            if y_min == y_max:
                pad = 1 if y_max == 0 else abs(y_max) * 0.05
            else:
                pad = (y_max - y_min) * 0.10
            self.plot.setYRange(y_min - pad, y_max + pad, padding=0)

    # ===== Import / Export (no scientific notation) =====
    def import_data_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择历史数据文件",
            "",
            "Data Files (*.jsonl *.csv);;All Files (*)",
        )
        if not path:
            return

        try:
            samples = []
            if path.lower().endswith(".jsonl"):
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        ts_raw = obj.get("ts")
                        ts = float(ts_raw)  # ts may be str/float/int
                        values = {k: int(obj.get(k, 0)) for k, _cn in ALL_METRICS}
                        samples.append(Sample(ts=ts, values=values))

            elif path.lower().endswith(".csv"):
                with open(path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        ts = float(row["ts"])
                        values = {k: _int_or_zero(row.get(k, 0)) for k, _cn in ALL_METRICS}
                        samples.append(Sample(ts=ts, values=values))
            else:
                raise ValueError("仅支持 .jsonl 或 .csv")

            if not samples:
                raise ValueError("文件中没有有效数据")

            samples.sort(key=lambda s: s.ts)
            self.samples.clear()
            for s in samples:
                self.samples.append(s)

            self.start_ts = self.samples[0].ts
            self._last_online = self.samples[-1].values.get("online", 0)

            # 导入后停止实时抓取与自动保存（避免覆盖）
            self.timer.stop()
            self.autosave_timer.stop()
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)

            # 里程碑以导入首条为起点（不补记已达到）
            self.ms_log.clear()
            self._reset_milestones(self.samples[0].values)

            self._update_table()
            self._redraw(force_follow=True, force_autoy=True)

            t0 = time.strftime("%H:%M:%S", time.localtime(self.start_ts))
            t1 = time.strftime("%H:%M:%S", time.localtime(self.samples[-1].ts))
            self.status.setText(f"状态：已导入历史数据（{len(self.samples)} 条，{t0}~{t1}）")
            self.log(f"导入成功：{os.path.basename(path)}（{len(self.samples)}条）")
        except Exception as e:
            self.status.setText(f"状态：导入失败：{e}")
            self.log(f"导入失败：{e}")

    def _default_export_name(self, ext: str) -> str:
        safe_title = safe_filename(self.title).replace(" ", "_")
        t = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        return f"{safe_title}_{t}.{ext}"

    def _write_csv(self, path: str, overwrite: bool = True):
        mode = "w" if overwrite else "x"
        with open(path, mode, encoding="utf-8", newline="") as f:
            fieldnames = ["ts"] + [k for k, _cn in ALL_METRICS]
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for s in self.samples:
                row = {"ts": f"{s.ts:.3f}"}  # fixed-point string
                for k, _cn in ALL_METRICS:
                    row[k] = int(s.values.get(k, 0))
                w.writerow(row)

    def _write_jsonl(self, path: str, overwrite: bool = True):
        mode = "w" if overwrite else "x"
        with open(path, mode, encoding="utf-8") as f:
            for s in self.samples:
                obj = {"ts": f"{s.ts:.3f}"}  # store as string to avoid any sci-notation
                for k, _cn in ALL_METRICS:
                    obj[k] = int(s.values.get(k, 0))
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def export_csv(self):
        if not self.samples:
            self.status.setText("状态：没有可导出的数据")
            self.log("导出CSV：失败（无数据）")
            return

        default = self._default_export_name("csv")
        path, _ = QFileDialog.getSaveFileName(self, "导出为 CSV", default, "CSV (*.csv);;All Files (*)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"

        try:
            self._write_csv(path, overwrite=True)
            self.status.setText(f"状态：已导出 CSV：{os.path.basename(path)}")
            self.log(f"导出CSV：{os.path.basename(path)}")
        except Exception as e:
            self.status.setText(f"状态：导出失败：{e}")
            self.log(f"导出CSV：失败：{e}")

    def export_jsonl(self):
        if not self.samples:
            self.status.setText("状态：没有可导出的数据")
            self.log("导出JSONL：失败（无数据）")
            return

        default = self._default_export_name("jsonl")
        path, _ = QFileDialog.getSaveFileName(self, "导出为 JSONL", default, "JSONL (*.jsonl);;All Files (*)")
        if not path:
            return
        if not path.lower().endswith(".jsonl"):
            path += ".jsonl"

        try:
            self._write_jsonl(path, overwrite=True)
            self.status.setText(f"状态：已导出 JSONL：{os.path.basename(path)}")
            self.log(f"导出JSONL：{os.path.basename(path)}")
        except Exception as e:
            self.status.setText(f"状态：导出失败：{e}")
            self.log(f"导出JSONL：失败：{e}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("B站视频数据监测工具")
        self.resize(1400, 860)

        self.tabs = QTabWidget()
        self.panels = [MonitorPanel(f"监控{i}") for i in range(1, 11)]
        for i, p in enumerate(self.panels, start=1):
            self.tabs.addTab(p, f"监控{i}")

        self.tabs.tabBarDoubleClicked.connect(self.rename_tab)
        self.setCentralWidget(self.tabs)

        self._theme_dark = True
        self._build_toolbar()
        self.apply_theme(True)

    def _build_toolbar(self):
        tb = QToolBar("工具")
        self.addToolBar(tb)

        act_dark = QAction("深色主题", self)
        act_light = QAction("浅色主题", self)
        act_dark.triggered.connect(lambda: self.apply_theme(True))
        act_light.triggered.connect(lambda: self.apply_theme(False))
        tb.addAction(act_dark)
        tb.addAction(act_light)

        tb.addSeparator()
        act_rename = QAction("重命名标签页", self)
        act_rename.triggered.connect(lambda: self.rename_tab(self.tabs.currentIndex()))
        tb.addAction(act_rename)

    def rename_tab(self, index: int):
        if index is None or index < 0:
            return
        old = self.tabs.tabText(index)
        text, ok = QInputDialog.getText(self, "重命名标签页", "新名称：", text=old)
        if ok and text.strip():
            name = text.strip()
            self.tabs.setTabText(index, name)
            try:
                self.panels[index].set_title(name)
                self.panels[index].log(f"标签页重命名：{old} -> {name}")
            except Exception:
                pass

    def apply_theme(self, dark: bool):
        self._theme_dark = dark
        if dark:
            qss = """
            QWidget { background: #0f0f0f; color: #e6e6e6; }
            QGroupBox { border: 1px solid #2a2a2a; margin-top: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
            QLineEdit, QSpinBox, QComboBox, QTableWidget, QTextEdit {
                background: #171717; color: #e6e6e6; border: 1px solid #2a2a2a;
            }
            QPushButton { background: #1f1f1f; border: 1px solid #2a2a2a; padding: 6px 10px; }
            QPushButton:disabled { color: #777; }
            QHeaderView::section { background: #171717; border: 1px solid #2a2a2a; }
            """
        else:
            qss = """
            QWidget { background: #ffffff; color: #111111; }
            QGroupBox { border: 1px solid #d0d0d0; margin-top: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
            QLineEdit, QSpinBox, QComboBox, QTableWidget, QTextEdit {
                background: #ffffff; color: #111111; border: 1px solid #d0d0d0;
            }
            QPushButton { background: #f5f5f5; border: 1px solid #d0d0d0; padding: 6px 10px; }
            QHeaderView::section { background: #f5f5f5; border: 1px solid #d0d0d0; }
            """
        self.setStyleSheet(qss)
        for p in self.panels:
            p.apply_theme(dark)

    def closeEvent(self, event):
        r1 = QMessageBox.question(
            self,
            "确认退出",
            "确定要退出吗？退出后所有监控/自动保存将停止。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if r1 != QMessageBox.Yes:
            event.ignore()
            return

        r2 = QMessageBox.question(
            self,
            "二次确认",
            "真的要退出吗？需要再次确认才会退出。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if r2 != QMessageBox.Yes:
            event.ignore()
            return

        # 最终确认后，安全停止所有tab的抓取/自动保存
        try:
            for i in range(self.tabs.count()):
                panel = self.tabs.widget(i)
                if panel:
                    try:
                        panel.stop()
                    except Exception:
                        pass
        finally:
            event.accept()


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()