"""Near-black + cyan color system and the app's global QSS stylesheet.

Single source of truth for every color used by the Qt UI -- widgets pull
hex values from COLORS instead of hardcoding them, so the palette can be
retuned in one place.
"""
from __future__ import annotations

COLORS: dict[str, str] = {
    "bg": "#0e0f11",
    "surface": "#16181b",
    "surface_alt": "#1e2124",
    "surface_raised": "#24272b",
    "border": "#2c2f33",
    "border_strong": "#3a3e43",
    "text": "#eceeef",
    "text_muted": "#8b8f94",
    "text_dim": "#5a5e64",
    "accent": "#29c5e6",
    "accent_hover": "#52d2ee",
    "accent_pressed": "#1fa8c6",
    "accent_dark": "#177e94",
    "accent_muted": "#1c3238",
    "success": "#3ec97b",
    "warning": "#e8a83c",
    "danger": "#e5544c",
    "danger_hover": "#f06f68",
    "violet": "#8b7bf0",
    "violet_hover": "#a294f5",
}

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"

RADIUS = 6


def _c(name: str) -> str:
    return COLORS[name]


def build_stylesheet(font_pt: int = 10, mono_pt: int = 10) -> str:
    c = COLORS
    return f"""
* {{
    font-family: "{FONT_FAMILY}";
    font-size: {font_pt}pt;
    color: {c['text']};
    outline: none;
}}

QWidget {{
    background-color: {c['bg']};
}}

QMainWindow, QDialog {{
    background-color: {c['bg']};
}}

QMainWindow::separator {{
    background-color: {c['accent_dark']};
    width: 4px;
    height: 4px;
}}
QMainWindow::separator:hover {{
    background-color: {c['accent']};
}}

QLabel {{
    background: transparent;
}}

QLabel[muted="true"] {{
    color: {c['text_muted']};
}}

QLabel[heading="true"] {{
    font-size: {font_pt + 2}pt;
    font-weight: 600;
    color: {c['text']};
}}

QLabel[mono="true"] {{
    font-family: "{MONO_FAMILY}";
    font-size: {mono_pt}pt;
    color: {c['text_muted']};
}}

/* ---------------------------------------------------------------- Frames */

QFrame#Panel, QWidget#Panel {{
    background-color: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: {RADIUS}px;
}}

QFrame#Divider {{
    background-color: {c['border']};
    max-width: 1px;
    min-width: 1px;
}}

QFrame#HDivider {{
    background-color: {c['border']};
    max-height: 1px;
    min-height: 1px;
}}

/* ------------------------------------------------------------------ Tabs */

QTabWidget::pane {{
    border: 1px solid {c['border']};
    border-radius: {RADIUS}px;
    background-color: {c['bg']};
    top: -1px;
}}

QTabBar::tab {{
    background-color: {c['surface']};
    color: {c['text_muted']};
    padding: 8px 18px;
    margin-right: 2px;
    border-top-left-radius: {RADIUS}px;
    border-top-right-radius: {RADIUS}px;
    border: 1px solid {c['border']};
    border-bottom: none;
}}

QTabBar::tab:selected {{
    background-color: {c['accent']};
    color: #06181c;
    font-weight: 600;
}}

QTabBar::tab:hover:!selected {{
    background-color: {c['surface_raised']};
    color: {c['text']};
}}

/* --------------------------------------------------------------- Buttons */

QPushButton {{
    background-color: {c['surface_alt']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: {RADIUS}px;
    padding: 6px 14px;
}}

QPushButton:hover {{
    background-color: {c['surface_raised']};
    border-color: {c['border_strong']};
}}

QPushButton:pressed {{
    background-color: {c['bg']};
}}

QPushButton:disabled {{
    color: {c['text_dim']};
    background-color: {c['surface']};
    border-color: {c['border']};
}}

QPushButton[variant="primary"] {{
    background-color: {c['accent']};
    color: #06181c;
    border: 1px solid {c['accent']};
    font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{ background-color: {c['accent_hover']}; }}
QPushButton[variant="primary"]:pressed {{ background-color: {c['accent_pressed']}; }}
QPushButton[variant="primary"]:disabled {{ background-color: {c['accent_muted']}; color: {c['text_dim']}; border-color: {c['border']}; }}

QPushButton[variant="violet"] {{
    background-color: {c['violet']};
    color: #100c2b;
    border: 1px solid {c['violet']};
    font-weight: 600;
}}
QPushButton[variant="violet"]:hover {{ background-color: {c['violet_hover']}; }}

QPushButton[variant="danger"] {{
    background-color: {c['danger']};
    color: #2a0a08;
    border: 1px solid {c['danger']};
}}
QPushButton[variant="danger"]:hover {{ background-color: {c['danger_hover']}; }}

QPushButton[variant="flat"] {{
    background-color: transparent;
    border: 1px solid transparent;
}}
QPushButton[variant="flat"]:hover {{
    background-color: {c['surface_alt']};
}}

QToolButton {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS}px;
    padding: 4px;
}}
QToolButton:hover {{
    background-color: {c['surface_alt']};
    border-color: {c['border']};
}}
QToolButton:pressed {{
    background-color: {c['bg']};
}}

/* -------------------------------------------------------- Inputs / Combo */

QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {c['surface_alt']};
    border: 1px solid {c['border']};
    border-radius: {RADIUS}px;
    padding: 4px 6px;
    selection-background-color: {c['accent']};
    selection-color: #06181c;
}}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {c['accent']};
}}

QLineEdit:read-only {{
    color: {c['text_muted']};
    background-color: {c['surface']};
}}

QLineEdit:disabled, QTextEdit:disabled {{
    color: {c['text_dim']};
}}

QComboBox {{
    background-color: {c['surface_alt']};
    border: 1px solid {c['border']};
    border-radius: {RADIUS}px;
    padding: 4px 8px;
}}
QComboBox:hover {{ border-color: {c['border_strong']}; }}
QComboBox:focus {{ border-color: {c['accent']}; }}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background-color: {c['surface_alt']};
    border: 1px solid {c['border']};
    selection-background-color: {c['accent']};
    selection-color: #06181c;
    outline: none;
}}

QCheckBox {{
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid {c['border_strong']};
    background-color: {c['surface_alt']};
}}
QCheckBox::indicator:checked {{
    background-color: {c['accent']};
    border-color: {c['accent']};
}}
QCheckBox::indicator:hover {{
    border-color: {c['accent']};
}}

QRadioButton {{
    spacing: 8px;
}}
QRadioButton::indicator {{
    width: 15px;
    height: 15px;
    border-radius: 8px;
    border: 1px solid {c['border_strong']};
    background-color: {c['surface_alt']};
}}
QRadioButton::indicator:checked {{
    background-color: {c['accent']};
    border: 4px solid {c['surface_alt']};
    outline: 1px solid {c['accent']};
}}

/* ---------------------------------------------------------------- Lists */

QListWidget, QTreeWidget {{
    background-color: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: {RADIUS}px;
    padding: 2px;
}}
QListWidget::item, QTreeWidget::item {{
    padding: 4px 6px;
    border-radius: 4px;
}}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background-color: {c['accent_muted']};
    color: {c['text']};
}}
QListWidget::item:hover, QTreeWidget::item:hover {{
    background-color: {c['surface_alt']};
}}

/* ---------------------------------------------------------------- Other */

QProgressBar {{
    background-color: {c['surface_alt']};
    border: 1px solid {c['border']};
    border-radius: {RADIUS}px;
    text-align: center;
    color: {c['text_muted']};
    height: 14px;
}}
QProgressBar::chunk {{
    background-color: {c['accent']};
    border-radius: {RADIUS - 1}px;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {c['border_strong']};
    border-radius: 5px;
    min-height: 24px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background: {c['text_dim']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QScrollBar:horizontal {{
    background: transparent;
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {c['border_strong']};
    border-radius: 5px;
    min-width: 24px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {c['text_dim']}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QScrollArea {{
    border: none;
    background: transparent;
}}

QSplitter::handle {{
    background-color: {c['accent_dark']};
}}
QSplitter::handle:hover {{
    background-color: {c['accent']};
}}

QToolTip {{
    background-color: {c['surface_raised']};
    color: {c['text']};
    border: 1px solid {c['border_strong']};
    padding: 4px 6px;
    border-radius: 4px;
}}

QMenu {{
    background-color: {c['surface_alt']};
    border: 1px solid {c['border']};
    border-radius: {RADIUS}px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 20px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {c['accent_muted']};
}}
"""
