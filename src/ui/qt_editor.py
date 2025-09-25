import sys
import os
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
# New imports for preprocessing
import cv2
import numpy as np

from PyQt6 import QtCore, QtGui, QtWidgets

# Try importing project modules; fall back to simple defaults if not available.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FIELD_TYPES: Dict[str, Dict[str, Any]] = {}
try:
    from src.constants import FIELD_TYPES as _FT  # type: ignore
    FIELD_TYPES = dict(_FT)
except Exception:
    FIELD_TYPES = {
        "QTYPE_INT": {"bubbleValues": [str(i) for i in range(10)], "direction": "vertical"},
        "QTYPE_INT_FROM_1": {"bubbleValues": [str(i) for i in range(1, 10)] + ["0"], "direction": "vertical"},
        "QTYPE_MCQ4": {"bubbleValues": ["A", "B", "C", "D"], "direction": "horizontal"},
        "QTYPE_MCQ5": {"bubbleValues": ["A", "B", "C", "D", "E"], "direction": "horizontal"},
    }

def load_image_as_pixmap(img_path: Path) -> QtGui.QPixmap:
    img = QtGui.QImage(str(img_path))
    if img.isNull():
        raise SystemExit(f"Cannot load image: {img_path}")
    return QtGui.QPixmap.fromImage(img)

def find_first_image_under(inputs_dir: Path) -> Optional[Path]:
    exts = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
    if not inputs_dir.exists():
        return None
    for p in sorted(inputs_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts:
            return p
    return None

def find_first_template_under(inputs_dir: Path) -> Optional[Path]:
    """Find the first template.json under the given inputs dir (recursively)."""
    if not inputs_dir.exists():
        return None
    for p in sorted(inputs_dir.rglob("template.json")):
        if p.is_file():
            return p
    return None

def parse_csv_or_range(text: str) -> List[str]:
    # e.g., "A,B,C" or "q1..4" -> ["q1","q2","q3","q4"] (kept simple for labels)
    t = text.strip()
    if ".." in t and "," not in t:
        # very simple range expansion: prefix + start..end (numbers only)
        # Example: q1..4 -> q1, q2, q3, q4
        import re
        m = re.match(r"^([^\d]+)(\d+)\.\.(\d+)$", t)
        if m:
            pref, s, e = m.group(1), int(m.group(2)), int(m.group(3))
            step = 1 if e >= s else -1
            return [f"{pref}{i}" for i in range(s, e + step, step)]
    if t == "":
        return []
    return [x.strip() for x in t.split(",") if x.strip()]

def to_csv(values: List[str]) -> str:
    return ",".join(values)

# New: import defaults and preprocessors
try:
    from dotmap import DotMap
    from src.defaults.config import CONFIG_DEFAULTS
    from src.utils.parsing import open_config_with_defaults  # NEW
    from src.processors.CropPage import CropPage
    from src.processors.CropOnMarkers import CropOnMarkers
    from src.processors.FeatureBasedAlignment import FeatureBasedAlignment
    from src.processors.builtins import GaussianBlur as _GaussianBlur, MedianBlur as _MedianBlur, Levels as _Levels
    from src.template import Template  # NEW
    from src.utils.image import ImageUtils  # NEW
except Exception:
    # Editor should still run without project context (noop fallbacks)
    DotMap = None
    CONFIG_DEFAULTS = None
    open_config_with_defaults = None  # NEW
    CropPage = CropOnMarkers = FeatureBasedAlignment = object  # type: ignore
    Template = None  # type: ignore

class TemplateModel(QtCore.QObject):
    changed = QtCore.pyqtSignal()

    def __init__(self, template_path: Path, image_path: Path):
        super().__init__()
        self.template_path = template_path
        self.image_path = image_path
        self.template: Dict[str, Any] = {}
        self.load()

    def load(self):
        try:
            with open(self.template_path, "r") as f:
                self.template = json.load(f)
        except Exception as e:
            raise SystemExit(f"Failed to load template: {e}")

        self.template.setdefault("fieldBlocks", {})
        self.template.setdefault("bubbleDimensions", [20, 20])  # [width, height]
        self.template.setdefault("pageDimensions", [0, 0])

    def field_blocks(self) -> List[Tuple[str, Dict[str, Any]]]:
        return list(self.template.get("fieldBlocks", {}).items())

    def get_block(self, name: str) -> Dict[str, Any]:
        return self.template["fieldBlocks"][name]

    def add_block(self, name: str, rect: QtCore.QRectF):
        # Store rect as origin(x,y), BubblesGap=width, LabelsGap=height (matches existing template usage)
        fb = {
            "origin": [int(rect.left()), int(rect.top())],
            "bubblesGap": int(max(30, rect.width())),
            "labelsGap": int(max(30, rect.height())),
            "direction": "horizontal",
            "fieldLabels": ["q1..1"],
            "bubbleValues": ["A", "B", "C", "D"],
        }
        self.template["fieldBlocks"][name] = fb
        self.changed.emit()

    def remove_block(self, name: str):
        if name in self.template["fieldBlocks"]:
            del self.template["fieldBlocks"][name]
            self.changed.emit()

    def next_block_name(self, prefix="MCQ_Block_Q") -> str:
        i = 1
        existing = set(self.template["fieldBlocks"].keys())
        while f"{prefix}{i}" in existing:
            i += 1
        return f"{prefix}{i}"

    def save_as_edited(self) -> Path:
        out = self.template_path.with_name(self.template_path.stem + ".edited.json")
        with open(out, "w") as f:
            json.dump(self.template, f, indent=2)
        return out

# New: minimal image-instance ops to satisfy processors
class _DummyImageInstanceOps:
    def __init__(self, tuning_config):
        self.tuning_config = tuning_config

    def append_save_img(self, *args, **kwargs):
        pass

# New: run template preprocessors without downscaling/blur, like main.py
def run_preprocessors_for_editor(template_path: Path, image_path: Path) -> Optional[np.ndarray]:
    """
    Run preprocessing EXACTLY like the main pipeline:
      - Load config.json (if present) merged over CONFIG_DEFAULTS.
      - Build Template(template.json, config).
      - Call Template.image_instance_ops.apply_preprocessors(file_path, image, template),
        which internally:
          * resizes to processing_width/height,
          * applies preProcessors in order (CropPage, CropOnMarkers, blurs, levels, etc.) without overrides,
          * returns the processed image or None on failure.
      - For editor overlay consistency, resize the processed image to template.pageDimensions.
    """
    try:
        img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        if Template is None or CONFIG_DEFAULTS is None:
            return img

        # Load tuning config exactly like main (no forced overrides)
        template_dir = template_path.parent
        cfg = CONFIG_DEFAULTS
        cfg_path = template_dir / "config.json"
        if open_config_with_defaults is not None and cfg_path.exists():
            cfg = open_config_with_defaults(cfg_path)

        # Build Template and run its preprocessors as-is
        tmpl = Template(template_path, cfg)
        processed = tmpl.image_instance_ops.apply_preprocessors(str(image_path), img, tmpl)
        if processed is None:
            return None

        # Align preview to template coordinates (same as draw_template_layout does)
        try:
            pw, ph = tmpl.page_dimensions
            if int(pw) > 0 and int(ph) > 0:
                processed = ImageUtils.resize_util(processed, int(pw), int(ph))
        except Exception:
            # keep processed as-is if pageDimensions invalid
            pass
        return processed
    except Exception:
        return None

def np_gray_to_qpixmap(img: np.ndarray) -> QtGui.QPixmap:
    h, w = img.shape[:2]
    bytes_per_line = w
    qimg = QtGui.QImage(img.data, w, h, bytes_per_line, QtGui.QImage.Format.Format_Grayscale8)
    return QtGui.QPixmap.fromImage(qimg.copy())

class BlockGraphicsItem(QtWidgets.QGraphicsItem):
    def __init__(self, name: str, model: TemplateModel):
        super().__init__()
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges, True)  # NEW
        self.name = name
        self.model = model
        # Keep rect local; use item position for origin
        self._rect = QtCore.QRectF(0, 0, 120, 60)  # CHANGED
        # New: resize handles
        self._handles: Dict[str, QtWidgets.QGraphicsRectItem] = {}
        self._handle_size = 14.0  # larger grab area
        self.sync_from_model()
        self._create_handles()
        self._update_handles_positions()
        self._set_handles_visible(False)

    def boundingRect(self) -> QtCore.QRectF:
        # Slight padding for the border and handles
        r = self._rect
        pad = max(4.0, self._handle_size / 2.0)  # CHANGED
        return QtCore.QRectF(r.left() - pad, r.top() - pad, r.width() + 2 * pad, r.height() + 2 * pad)

    def paint(self, painter: QtGui.QPainter, option, widget=None):
        fb = self.model.get_block(self.name)
        # Base rect
        pen = QtGui.QPen(QtGui.QColor(200, 60, 60) if self.isSelected() else QtGui.QColor(40, 160, 40), 2)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRect(self._rect)

        # Title
        painter.setPen(QtGui.QPen(QtGui.QColor(240, 240, 240)))
        painter.setFont(QtGui.QFont("", 9))
        painter.drawText(QtCore.QRectF(self._rect.left()+4, self._rect.top()-16, self._rect.width(), 16),
                         QtCore.Qt.AlignmentFlag.AlignLeft, f"{self.name}")  # CHANGED

        # Bubble preview (grid using bubbleDimensions, gaps, direction, and labels)
        vals: List[str] = fb.get("bubbleValues") or []
        labels: List[str] = fb.get("fieldLabels") or []
        if not isinstance(vals, list):
            vals = []
        if not isinstance(labels, list):
            labels = []
        bw, bh = fb.get("bubbleDimensions", self.model.template.get("bubbleDimensions", [20, 20]))
        try:
            bw = float(bw)
            bh = float(bh)
        except Exception:
            bw, bh = 20.0, 20.0
        direction = fb.get("direction", "horizontal")
        bubbles_gap = int(fb.get("bubblesGap", 12))
        labels_gap = int(fb.get("labelsGap", 12))
        base_x = self._rect.left()
        base_y = self._rect.top()
        painter.setPen(QtGui.QPen(QtGui.QColor(240, 200, 60), 1))
        if direction == "vertical":
            for li, _lab in enumerate(labels):
                start_x = base_x + li * labels_gap
                start_y = base_y
                for vi, _val in enumerate(vals):
                    x = start_x
                    y = start_y + vi * bubbles_gap
                    rx = x + bw * 0.10
                    ry = y + bh * 0.10
                    rw = max(2.0, bw - 2 * bw * 0.10)
                    rh = max(2.0, bh - 2 * bh * 0.10)
                    painter.drawRect(QtCore.QRectF(rx, ry, rw, rh))
        else:
            for li, _lab in enumerate(labels):
                start_x = base_x
                start_y = base_y + li * labels_gap
                for vi, _val in enumerate(vals):
                    x = start_x + vi * bubbles_gap
                    y = start_y
                    rx = x + bw * 0.10
                    ry = y + bh * 0.10
                    rw = max(2.0, bw - 2 * bw * 0.10)
                    rh = max(2.0, bh - 2 * bh * 0.10)
                    painter.drawRect(QtCore.QRectF(rx, ry, rw, rh))

    def sync_from_model(self):
        fb = self.model.get_block(self.name)
        ox, oy = fb.get("origin", [0, 0])
        direction = fb.get("direction", "horizontal")
        vals: List[str] = fb.get("bubbleValues", []) or []
        labels: List[str] = fb.get("fieldLabels", []) or []
        bubbles_gap = int(fb.get("bubblesGap", 12))
        labels_gap = int(fb.get("labelsGap", 12))
        bw, bh = fb.get("bubbleDimensions", self.model.template.get("bubbleDimensions", [20, 20]))
        try:
            bw = int(bw)
            bh = int(bh)
        except Exception:
            bw, bh = 20, 20
        n_vals = max(1, len(vals))
        n_fields = max(1, len(labels))
        if direction == "vertical":
            values_dimension = int(bubbles_gap * (n_vals - 1) + bh)
            fields_dimension = int(labels_gap * (n_fields - 1) + bw)
            width, height = fields_dimension, values_dimension
        else:
            values_dimension = int(bubbles_gap * (n_vals - 1) + bw)
            fields_dimension = int(labels_gap * (n_fields - 1) + bh)
            width, height = values_dimension, fields_dimension
        self.prepareGeometryChange()
        self._rect = QtCore.QRectF(0, 0, max(30, width), max(30, height))
        self.setPos(float(ox), float(oy))

    def update_model_from_item(self):
        """Update origin and derive gaps from current rect dimensions (inverse of calculate_block_dimensions)."""
        fb = self.model.get_block(self.name)
        r = self._rect
        fb["origin"] = [int(self.pos().x()), int(self.pos().y())]
        direction = fb.get("direction", "horizontal")
        vals: List[str] = fb.get("bubbleValues", []) or []
        labels: List[str] = fb.get("fieldLabels", []) or []
        n_vals = max(1, len(vals))
        n_fields = max(1, len(labels))
        bw, bh = fb.get("bubbleDimensions", self.model.template.get("bubbleDimensions", [20, 20]))
        try:
            bw = float(bw)
            bh = float(bh)
        except Exception:
            bw, bh = 20.0, 20.0
        width = float(r.width())
        height = float(r.height())
        if direction == "vertical":
            fields_dimension = width
            values_dimension = height
            fb["labelsGap"] = int(round((fields_dimension - bw) / (n_fields - 1))) if n_fields > 1 else int(bw)
            fb["bubblesGap"] = int(round((values_dimension - bh) / (n_vals - 1))) if n_vals > 1 else int(bh)
        else:
            values_dimension = width
            fields_dimension = height
            fb["bubblesGap"] = int(round((values_dimension - bw) / (n_vals - 1))) if n_vals > 1 else int(bw)
            fb["labelsGap"] = int(round((fields_dimension - bh) / (n_fields - 1))) if n_fields > 1 else int(bh)

    # New: handle creation and positioning
    def _create_handles(self):
        roles = ("tl", "tr", "bl", "br")
        for r in roles:
            item = _ResizeHandle(self, r, self._handle_size)
            self._handles[r] = item

    def _update_handles_positions(self):
        r = self._rect
        s = self._handle_size
        offs = s / 2.0
        positions = {
            "tl": QtCore.QPointF(r.left() - offs, r.top() - offs),
            "tr": QtCore.QPointF(r.right() - offs, r.top() - offs),
            "bl": QtCore.QPointF(r.left() - offs, r.bottom() - offs),
            "br": QtCore.QPointF(r.right() - offs, r.bottom() - offs),
        }
        for role, item in self._handles.items():
            # Suppress itemChange while programmatically repositioning to avoid recursion
            if hasattr(item, "setPosSilently"):
                item.setPosSilently(positions[role])  # type: ignore[attr-defined]
            else:
                item.setPos(positions[role])

    def _set_handles_visible(self, vis: bool):
        for item in self._handles.values():
            item.setVisible(vis)

    def setSelected(self, selected: bool):
        super().setSelected(selected)
        self._set_handles_visible(selected)

    # Single safe override; do not call super().itemChange (avoid enum type error)
    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            # Accept movement; keep rect anchored at (0,0) and update model/handles
            QtCore.QTimer.singleShot(0, self._update_handles_positions)
            QtCore.QTimer.singleShot(0, self.update_model_from_item)
            return value
        return value

    # Public API used by handle items
    def resize_from_handle(self, role: str, handle_local_pos: QtCore.QPointF):
        r = self._rect
        MIN_W, MIN_H = 30, 30
        offs = self._handle_size / 2.0
        # Handles are positioned with top-left at (corner - offs); use center as actual corner
        corner = handle_local_pos + QtCore.QPointF(offs, offs)  # CHANGED
        new = QtCore.QRectF(r)
        if role == "tl":
            new.setTopLeft(corner)
        elif role == "tr":
            new.setTopRight(corner)
        elif role == "bl":
            new.setBottomLeft(corner)
        elif role == "br":
            new.setBottomRight(corner)
        new = new.normalized()
        if new.width() < MIN_W:
            new.setWidth(MIN_W)
        if new.height() < MIN_H:
            new.setHeight(MIN_H)
        self.set_rect(new)
        self._update_handles_positions()

    def set_rect(self, rect: QtCore.QRectF):
        self.prepareGeometryChange()
        self._rect = rect.normalized()
        self.update_model_from_item()
        self.update()

# New: subclass handle item to forward drag to parent
class _ResizeHandle(QtWidgets.QGraphicsRectItem):
    def __init__(self, parent: BlockGraphicsItem, role: str, size: float):
        super().__init__(0, 0, size, size, parent)
        self._parent = parent
        self.role = role  # type: ignore[attr-defined]
        self.setBrush(QtGui.QBrush(QtGui.QColor(255, 200, 0, 220)))
        self.setPen(QtGui.QPen(QtGui.QColor(30, 30, 30), 1))
        self.setZValue(1000)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges, True)
        # Suppress recursion when parent repositions this handle
        self._suppress_item_change = False

    def setPosSilently(self, pos: QtCore.QPointF):
        self._suppress_item_change = True
        try:
            super().setPos(pos)
        finally:
            self._suppress_item_change = False

    def itemChange(self, change, value):
        # Avoid calling super().itemChange to prevent enum type issues
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            if self._suppress_item_change:
                return value
            # value is the new pos in parent's coordinates; pass directly
            self._parent.resize_from_handle(self.role, value)  # type: ignore[arg-type]
            return value
        return value

class GraphicsView(QtWidgets.QGraphicsView):
    newRectDrawn = QtCore.pyqtSignal(QtCore.QRectF)

    def __init__(self, scene: QtWidgets.QGraphicsScene):
        super().__init__(scene)
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._adding = False
        self._rubber_item: Optional[QtWidgets.QGraphicsRectItem] = None
        self._start_scene_pt: Optional[QtCore.QPointF] = None

    def enter_add_mode(self):
        self._adding = True
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)

    def wheelEvent(self, event: QtGui.QWheelEvent):
        # Zoom under mouse
        zoom_in = event.angleDelta().y() > 0
        factor = 1.15 if zoom_in else 1 / 1.15
        self.scale(factor, factor)

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if self._adding and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._start_scene_pt = self.mapToScene(event.position().toPoint())
            self._rubber_item = QtWidgets.QGraphicsRectItem()
            pen = QtGui.QPen(QtGui.QColor(255, 200, 0), 1, QtCore.Qt.PenStyle.DashLine)
            self._rubber_item.setPen(pen)
            self.scene().addItem(self._rubber_item)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        if self._adding and self._start_scene_pt is not None and self._rubber_item:
            cur = self.mapToScene(event.position().toPoint())
            rect = QtCore.QRectF(self._start_scene_pt, cur).normalized()
            self._rubber_item.setRect(rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent):
        if self._adding and self._rubber_item and event.button() == QtCore.Qt.MouseButton.LeftButton:
            rect = self._rubber_item.rect()
            self.scene().removeItem(self._rubber_item)
            self._rubber_item = None
            self._start_scene_pt = None
            self._adding = False
            self.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            if rect.width() >= 10 and rect.height() >= 10:
                self.newRectDrawn.emit(rect)
            event.accept()
            return
        super().mouseReleaseEvent(event)

class BlockPanel(QtWidgets.QGroupBox):
    changed = QtCore.pyqtSignal(str)  # emits block name

    def __init__(self, name: str, model: TemplateModel):
        super().__init__(name)
        # Collapsible panel using checkable header to toggle body visibility
        self.setCheckable(True)  # CHANGED
        self.setChecked(False)   # CHANGED
        self.name = name
        self.model = model

        # Container for body to collapse
        self._body = QtWidgets.QWidget()  # NEW
        form = QtWidgets.QFormLayout(self._body)  # NEW
        outer = QtWidgets.QVBoxLayout(self)  # NEW
        outer.setContentsMargins(6, 6, 6, 6)  # NEW
        outer.addWidget(self._body)  # NEW

        fb = self.model.get_block(name)

        # FieldName
        self.field_name = QtWidgets.QLineEdit(name)
        form.addRow("FieldName", self.field_name)

        # BubbleValues (CSV)
        vals = fb.get("bubbleValues", [])
        self.bubble_values = QtWidgets.QLineEdit(to_csv(vals) if isinstance(vals, list) else "")
        form.addRow("BubbleValues", self.bubble_values)

        # Direction
        self.direction = QtWidgets.QComboBox()
        self.direction.addItems(["horizontal", "vertical"])
        self.direction.setCurrentText(fb.get("direction", "horizontal"))
        form.addRow("Direction", self.direction)

        # FieldLabels (CSV or range)
        self.field_labels = QtWidgets.QLineEdit(to_csv(fb.get("fieldLabels", [])))
        form.addRow("FieldLabels", self.field_labels)

        # LabelsGap (height)
        self.labels_gap = QtWidgets.QSpinBox()
        self.labels_gap.setRange(0, 10000)
        self.labels_gap.setValue(int(fb.get("labelsGap", 60)))
        form.addRow("LabelsGap", self.labels_gap)

        # BubblesGap (width)
        self.bubbles_gap = QtWidgets.QSpinBox()
        self.bubbles_gap.setRange(0, 10000)
        self.bubbles_gap.setValue(int(fb.get("bubblesGap", 120)))
        form.addRow("BubblesGap", self.bubbles_gap)

        # origin
        ox, oy = fb.get("origin", [0, 0])
        self.origin_x = QtWidgets.QSpinBox()
        self.origin_x.setRange(0, 10000)
        self.origin_x.setValue(int(ox))
        self.origin_y = QtWidgets.QSpinBox()
        self.origin_y.setRange(0, 10000)
        self.origin_y.setValue(int(oy))
        origin_layout = QtWidgets.QHBoxLayout()
        origin_layout.addWidget(QtWidgets.QLabel("x"))
        origin_layout.addWidget(self.origin_x)
        origin_layout.addWidget(QtWidgets.QLabel("y"))
        origin_layout.addWidget(self.origin_y)
        form.addRow("Origin", origin_layout)

        # Fieldtype (optional)
        self.field_type = QtWidgets.QComboBox()
        self.field_type.addItem("")  # none
        self.field_type.addItems(list(FIELD_TYPES.keys()))
        self.field_type.setCurrentText(fb.get("fieldType", ""))
        form.addRow("Fieldtype", self.field_type)

        # Connections
        self.field_name.editingFinished.connect(self._apply)
        self.bubble_values.editingFinished.connect(self._apply)
        self.direction.currentTextChanged.connect(self._apply)
        self.field_labels.editingFinished.connect(self._apply)
        self.labels_gap.valueChanged.connect(self._apply)
        self.bubbles_gap.valueChanged.connect(self._apply)
        self.origin_x.valueChanged.connect(self._apply)
        self.origin_y.valueChanged.connect(self._apply)
        self.field_type.currentTextChanged.connect(self._apply_fieldtype)
        self.toggled.connect(self._toggle_body)  # NEW

    def _toggle_body(self, on: bool):  # NEW
        self._body.setVisible(on)

    def _apply_fieldtype(self, text: str):
        fb = self.model.get_block(self.name)
        if text:
            fb["fieldType"] = text
            # Apply defaults from FIELD_TYPES if bubbleValues/direction not explicitly set
            ft = FIELD_TYPES.get(text, {})
            if "bubbleValues" in ft:
                fb["bubbleValues"] = list(ft["bubbleValues"])
                self.bubble_values.setText(to_csv(fb["bubbleValues"]))
            if "direction" in ft:
                fb["direction"] = ft["direction"]
                self.direction.setCurrentText(fb["direction"])
        else:
            fb.pop("fieldType", None)
        self.changed.emit(self.name)

    def _apply(self):
        new_name = self.field_name.text().strip()
        fb = self.model.get_block(self.name)
        # If renamed
        if new_name and new_name != self.name:
            # Move the dict key
            self.model.template["fieldBlocks"][new_name] = fb
            del self.model.template["fieldBlocks"][self.name]
            self.name = new_name
            self.setTitle(new_name)

        fb["bubbleValues"] = parse_csv_or_range(self.bubble_values.text())
        fb["direction"] = self.direction.currentText()
        fb["fieldLabels"] = parse_csv_or_range(self.field_labels.text())
        fb["labelsGap"] = int(self.labels_gap.value())
        fb["bubblesGap"] = int(self.bubbles_gap.value())
        fb["origin"] = [int(self.origin_x.value()), int(self.origin_y.value())]
        self.changed.emit(self.name)

    def sync_from_model(self):
        fb = self.model.get_block(self.name)
        self.bubble_values.setText(to_csv(fb.get("bubbleValues", [])))
        self.direction.setCurrentText(fb.get("direction", "horizontal"))
        self.field_labels.setText(to_csv(fb.get("fieldLabels", [])))
        self.labels_gap.setValue(int(fb.get("labelsGap", 60)))
        self.bubbles_gap.setValue(int(fb.get("bubblesGap", 120)))
        ox, oy = fb.get("origin", [0, 0])
        self.origin_x.setValue(int(ox))
        self.origin_y.setValue(int(oy))
        self.field_type.setCurrentText(fb.get("fieldType", ""))

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, template_path: Path, image_path: Optional[Path]):
        super().__init__()
        self.setWindowTitle("OMR Template Editor (Qt6)")
        self.resize(1280, 800)
        # Wider sidebar and smoother docks
        self.setDockOptions(self.DockOption.AllowTabbedDocks | self.DockOption.AnimatedDocks)  # NEW

        # Model
        if image_path is None:
            image_path = find_first_image_under(PROJECT_ROOT / "inputs")
        if image_path is None:
            raise SystemExit("No image provided and none found under ./inputs")
        self.model = TemplateModel(template_path, image_path)

        # Scene/View
        self.scene = QtWidgets.QGraphicsScene(self)
        self.view = GraphicsView(self.scene)
        # Reduce paint trails when moving items
        self.view.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)  # NEW
        self.setCentralWidget(self.view)

        # Load and preprocess image (cropped, no downscale/blur)
        processed = run_preprocessors_for_editor(template_path, image_path)
        if processed is not None:
            pixmap = np_gray_to_qpixmap(processed)
        else:
            pixmap = load_image_as_pixmap(image_path)

        self.image_item = self.scene.addPixmap(pixmap)
        self.image_item.setZValue(-1000)
        self.scene.setSceneRect(self.image_item.boundingRect())  # NEW

        # Sidebar
        self.sidebar = QtWidgets.QDockWidget("FieldBlocks", self)
        self.sidebar.setAllowedAreas(QtCore.Qt.DockWidgetArea.RightDockWidgetArea)
        self.sidebar.setMinimumWidth(420)  # NEW
        self.sidebar_widget = QtWidgets.QWidget()
        self.sidebar_layout = QtWidgets.QVBoxLayout(self.sidebar_widget)
        self.sidebar_layout.setContentsMargins(6, 6, 6, 6)
        self.sidebar_layout.setSpacing(6)
        self.sidebar_scroll = QtWidgets.QScrollArea()
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_inner = QtWidgets.QWidget()
        self.sidebar_inner_layout = QtWidgets.QVBoxLayout(self.sidebar_inner)
        self.sidebar_inner_layout.setContentsMargins(0, 0, 0, 0)
        self.sidebar_inner_layout.setSpacing(6)
        self.sidebar_scroll.setWidget(self.sidebar_inner)
        self.sidebar_layout.addWidget(self.sidebar_scroll)
        self.sidebar_layout.addStretch(0)
        self.sidebar.setWidget(self.sidebar_widget)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.sidebar)

        # Toolbar
        tb = self.addToolBar("Tools")
        add_btn = QtGui.QAction("Add FieldBlock", self)
        add_btn.triggered.connect(self.on_add_block)
        save_btn = QtGui.QAction("Save", self)
        save_btn.triggered.connect(self.on_save)
        tb.addAction(add_btn)
        tb.addAction(save_btn)

        # Build existing blocks
        self.block_items: Dict[str, BlockGraphicsItem] = {}
        self.block_panels: Dict[str, BlockPanel] = {}
        for name, _ in self.model.field_blocks():
            self._create_block_item_and_panel(name)

        # Connect rubberband result
        self.view.newRectDrawn.connect(self._add_block_from_rect)

        # React to model changes (e.g., external updates)
        self.model.changed.connect(self.refresh_items)
        # Jump to/expand panel for selected item
        self.scene.selectionChanged.connect(self._on_scene_selection_changed)  # NEW

    def on_add_block(self):
        self.view.enter_add_mode()

    def _add_block_from_rect(self, rect: QtCore.QRectF):
        name = self.model.next_block_name()
        self.model.add_block(name, rect)
        self._create_block_item_and_panel(name)

    def _create_block_item_and_panel(self, name: str):
        # Graphics item
        item = BlockGraphicsItem(name, self.model)
        self.scene.addItem(item)
        self.block_items[name] = item

        # Sidebar panel
        panel = BlockPanel(name, self.model)
        panel.changed.connect(self._on_panel_changed)
        self.block_panels[name] = panel
        self.sidebar_inner_layout.addWidget(panel)
        panel._toggle_body(False)  # start collapsed (NEW)

    def _on_panel_changed(self, name: str):
        # Simply refresh; handles rename/delete consistently
        self.refresh_items()

    def refresh_items(self):
        # Sync items and panels from model state
        names_now = set(n for n, _ in self.model.field_blocks())
        # Preserve collapse/expanded states
        expanded_states: Dict[str, bool] = {n: w.isChecked() for n, w in self.block_panels.items()}  # NEW

        # Remove deleted
        for old in list(self.block_items.keys()):
            if old not in names_now:
                self.scene.removeItem(self.block_items[old])
                del self.block_items[old]
        for old in list(self.block_panels.keys()):
            if old not in names_now:
                w = self.block_panels[old]
                self.sidebar_inner_layout.removeWidget(w)
                w.deleteLater()
                del self.block_panels[old]

        # Update or create
        for name in names_now:
            if name not in self.block_items:
                it = BlockGraphicsItem(name, self.model)
                self.scene.addItem(it)
                self.block_items[name] = it
            else:
                self.block_items[name].sync_from_model()
                self.block_items[name].update()
            if name not in self.block_panels:
                pnl = BlockPanel(name, self.model)
                pnl.changed.connect(self._on_panel_changed)
                self.block_panels[name] = pnl
                self.sidebar_inner_layout.addWidget(pnl)
            else:
                self.block_panels[name].sync_from_model()

        # Restore expanded state
        for name in names_now:
            if name in expanded_states:
                self.block_panels[name].setChecked(expanded_states[name])  # NEW
        # Keep layout tidy
        self.sidebar_inner_layout.addStretch(0)

    def _on_scene_selection_changed(self):  # NEW
        # Jump to and expand the panel for the first selected block
        selected_names = [n for n, it in self.block_items.items() if it.isSelected()]
        if not selected_names:
            return
        name = selected_names[0]
        panel = self.block_panels.get(name)
        if panel is None:
            return
        panel.setChecked(True)
        self.sidebar_scroll.ensureWidgetVisible(panel)

    def on_save(self):
        out = self.model.save_as_edited()
        QtWidgets.QMessageBox.information(self, "Saved", f"Saved:\n{out}")

def parse_args(argv: List[str]) -> Tuple[Optional[Path], Optional[Path]]:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=False, help="Path to template.json. If omitted, auto-detect under ./inputs")
    ap.add_argument("--image", required=False, help="Path to an image. If omitted, auto-detect (preferring same folder as template)")
    args = ap.parse_args(argv)
    t = Path(args.template).resolve() if args.template else None
    img = Path(args.image).resolve() if args.image else None
    return t, img

def main():
    template_path, image_path = parse_args(sys.argv[1:])
    # Auto-detect template and image like main.py workflow if not provided
    if template_path is None:
        template_path = find_first_template_under(PROJECT_ROOT / "inputs")
        if template_path is None:
            raise SystemExit("No template.json provided and none found under ./inputs")
    # Prefer image under the template's directory; fallback to first under ./inputs
    if image_path is None:
        local_img = find_first_image_under(template_path.parent)
        image_path = local_img if local_img is not None else find_first_image_under(PROJECT_ROOT / "inputs")
        if image_path is None:
            raise SystemExit("No image provided and none found under ./inputs")

    app = QtWidgets.QApplication(sys.argv)
    # Dark-ish palette for contrast
    palette = app.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(30, 30, 30))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(37, 37, 37))
    palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(230, 230, 230))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(230, 230, 230))
    app.setPalette(palette)
    w = MainWindow(template_path, image_path)
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()