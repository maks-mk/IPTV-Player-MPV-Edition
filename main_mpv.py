"""
IPTV плеер на MPV + PySide6
Стабильное решение БЕЗ проблем с полноэкранным режимом
"""

# === ОПТИМИЗИРОВАННЫЕ ИМПОРТЫ ===
import sys
import os
import locale
import urllib.request
import time
import json
import ssl
import socket
from pathlib import Path
from functools import wraps
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

# === КОНФИГУРАЦИЯ И КОНСТАНТЫ ===
# Устанавливаем таймауты
ssl._create_default_https_context = ssl._create_unverified_context
socket.setdefaulttimeout(5)

# Настройка локали для MPV
locale.setlocale(locale.LC_NUMERIC, 'C')

# Путь MPV (Windows)
os.environ["PATH"] = r"C:\ProgramData\chocolatey\lib\mpvio.install\tools" + os.pathsep + os.environ["PATH"]

# Иконки
try:
    import qtawesome as qta
    HAS_QTA = True
except ImportError:
    print("WARNING: qtawesome не установлен! Используются текстовые кнопки.")
    print("Установите: pip install qtawesome")
    HAS_QTA = False

# Qt импорты
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                               QLabel, QPushButton, QListWidget, QListWidgetItem, QSplitter,
                               QComboBox, QLineEdit, QMenuBar, QFileDialog,
                               QMessageBox, QDialog, QDialogButtonBox, QTabWidget, QProgressBar, QSlider)
from PySide6.QtCore import Qt, QTimer, QThread, Signal, Slot, QMetaObject, Q_ARG, QSize
from PySide6.QtGui import QKeyEvent, QAction, QPixmap, QColor, QIcon

# === КОНСТАНТЫ ===
WINDOW_TITLE = "MaksIPTV Player - MPV Edition"
WINDOW_GEOMETRY = (100, 50, 1100, 650)
WINDOW_MIN_SIZE = (800, 600)
VIDEO_FRAME_MIN_SIZE = (640, 480)
CHANNEL_ICON_SIZE = 32

VOLUME_DEFAULT = 70
VOLUME_MIN = 0
VOLUME_MAX = 100
VOLUME_SLIDER_WIDTH = 150

TIMEOUT_SSL = 5
TIMEOUT_SOCKET = 5
TIMEOUT_DOWNLOAD = 30
TOGGLE_FULLSCREEN_DELAY = 0.5
VOLUME_DEBOUNCE_MS = 50
UI_INIT_DELAY_MS = 100
POST_INIT_DELAY_MS = 200
ICON_DOWNLOAD_DELAY_MS = 100
MAX_CONCURRENT_DOWNLOADS = 5
PLAYLIST_UPDATE_INTERVAL = 86400  # 24 часа

CATEGORY_ALL = "Все каналы"
CATEGORY_NONE = "Без категории"

COLORS = {
    'background': '#2a2a2a',
    'background_alt': '#383838',
    'panel_bg': '#2d2d2d',
    'accent': '#4080b0',
    'text': 'white',
    'text_dim': '#a0a0a0',
    'button_bg': '#3a3a3a',
    'button_border': '#555',
    'button_hover': '#4a4a4a',
    'button_pressed': '#2a2a2a',
    'button_disabled': '#2a2a2a',
    'button_border_disabled': '#444',
}

PLAYLISTS_JSON = "playlists.json"
DOWNLOADED_M3U = "downloaded.m3u"

USER_AGENT = 'Mozilla/5.0'
REQUEST_TIMEOUT = 30

MPV_SETTINGS = {
    'keep_open': 'yes', 'idle': 'yes',
    'input_default_bindings': 'no', 'input_vo_keyboard': 'no', 'osc': 'no',
    'cache': 'yes', 'demuxer_max_bytes': '150M', 'demuxer_max_back_bytes': '75M',
    'hwdec': 'auto', 'vo': 'gpu',
    'msg_level': 'all=error', 'fs': 'no',
}

# === ДЕКОРАТОРЫ ===
def toggle_protect(func):
    """Защита от множественных вызовов toggle_fullscreen"""
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not hasattr(self, 'is_toggling_fullscreen'):
            return func(self, *args, **kwargs)

        if self.is_toggling_fullscreen:
            print("Already toggling fullscreen, ignoring...")
            return

        current_time = time.time()
        if hasattr(self, 'last_fullscreen_toggle') and (current_time - self.last_fullscreen_toggle) < TOGGLE_FULLSCREEN_DELAY:
            print("Too soon, ignoring fullscreen toggle...")
            return

        self.is_toggling_fullscreen = True
        self.last_fullscreen_toggle = current_time

        try:
            result = func(self, *args, **kwargs)
            return result
        except Exception as e:
            self.is_toggling_fullscreen = False
            raise
        finally:
            QTimer.singleShot(500, self._reset_fullscreen_flag)

    return wrapper

def safe_call(default=None, silent=False):
    """Безопасный вызов с обработкой исключений"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Молчаливый режим для частых ошибок (например, загрузка иконок)
                if not silent:
                    print(f"Error in {func.__name__}: {e}")
                return default
        return wrapper
    return decorator

# === КЛАССЫ ДАННЫХ ===
@dataclass
class Channel:
    """Модель канала"""
    name: str
    url: str
    group: str
    logo: Optional[str] = None

    @classmethod
    def from_dict(cls, data):
        """Создать канал из словаря"""
        return cls(
            name=data.get('name', ''),
            url=data.get('url', ''),
            group=data.get('group', CATEGORY_NONE),
            logo=data.get('logo')
        )

    def to_dict(self):
        """Конвертировать в словарь"""
        return {
            'name': self.name,
            'url': self.url,
            'group': self.group,
            'logo': self.logo
        }


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def validate_m3u(content):
    """Валидация M3U контента"""
    return content and ('#EXTM3U' in content or '#EXTINF' in content)

def parse_m3u_line(line):
    """Парсинг строки EXTINF"""
    current_name = None
    current_group = None
    current_logo = None

    if ',' in line:
        info_part, current_name = line.split(',', 1)
        current_name = current_name.strip()

        if 'group-title="' in info_part:
            start = info_part.find('group-title="') + 13
            end = info_part.find('"', start)
            current_group = info_part[start:end]

        if 'tvg-logo="' in info_part:
            start = info_part.find('tvg-logo="') + 10
            end = info_part.find('"', start)
            current_logo = info_part[start:end]

    return current_name, current_group, current_logo

# === UI FACTORY HELPERS ===
def make_label(text, align=Qt.AlignCenter, style_type='default', parent=None):
    """Создать QLabel с базовыми стилями"""
    label = QLabel(text, parent)
    label.setAlignment(align)

    styles = {
        'default': f"color: {COLORS['text_dim']}; padding: 4px;",
        'header': "font-size: 14px; font-weight: bold; padding: 8px;",
        'channel_name': f"""
            font-size: 14px;
            font-weight: bold;
            padding: 6px;
            background-color: {COLORS['panel_bg']};
            border-radius: 2px;
            color: {COLORS['text']};
        """,
    }
    label.setStyleSheet(styles.get(style_type, styles['default']))
    return label

def make_button(text, callback=None, tooltip=None, style_type='default', parent=None):
    """Создать QPushButton с базовыми стилями"""
    btn = QPushButton(text, parent)

    style = f"""
        QPushButton {{
            background-color: {COLORS['button_bg']};
            border: 1px solid {COLORS['button_border']};
            border-radius: 4px;
            padding: 6px 12px;
            color: {COLORS['text']};
        }}
        QPushButton:hover {{
            background-color: {COLORS['button_hover']};
            border: 1px solid {COLORS['button_border']};
        }}
        QPushButton:pressed {{
            background-color: {COLORS['button_pressed']};
        }}
        QPushButton:disabled {{
            background-color: {COLORS['button_disabled']};
            border: 1px solid {COLORS['button_border_disabled']};
            color: {COLORS['text_dim']};
        }}
    """
    btn.setStyleSheet(style)

    if callback:
        btn.clicked.connect(callback)
    if tooltip:
        btn.setToolTip(tooltip)

    return btn

def make_layout(layout_type='vbox', parent=None, spacing=6, margins=(0,0,0,0)):
    """Создать QLayout с базовыми настройками"""
    if layout_type == 'vbox':
        layout = QVBoxLayout(parent)
    elif layout_type == 'hbox':
        layout = QHBoxLayout(parent)
    else:
        raise ValueError(f"Unknown layout type: {layout_type}")

    layout.setSpacing(spacing)
    layout.setContentsMargins(*margins)
    return layout

def make_combo_box(items=None, callback=None, min_width=150, parent=None):
    """Создать QComboBox с базовыми стилями"""
    combo = QComboBox(parent)
    combo.setMinimumWidth(min_width)

    if items:
        combo.addItems(items)

    if callback:
        combo.currentTextChanged.connect(callback)

    # Базовый стиль
    combo.setStyleSheet(f"""
        QComboBox {{
            background-color: {COLORS['button_bg']};
            border: 1px solid {COLORS['button_border']};
            border-radius: 3px;
            padding: 4px;
            color: {COLORS['text']};
        }}
        QComboBox::drop-down {{
            border: none;
        }}
        QComboBox::down-arrow {{
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 6px solid {COLORS['text_dim']};
            margin-right: 6px;
        }}
    """)

    return combo


try:
    import mpv
except ImportError:
    print("ERROR: python-mpv не установлен!")
    print("Установите: pip install python-mpv")
    print("А также скачайте MPV: https://mpv.io/installation/")
    sys.exit(1)


class PlaylistDownloadThread(QThread):
    """Поток для загрузки плейлиста"""
    finished = Signal(bool, str)

    def __init__(self, url, file_path):
        super().__init__()
        self.url = url
        self.file_path = file_path
        self._running = True

    def run(self):
        try:
            # Настройка request
            opener = urllib.request.build_opener()
            opener.addheaders = [('User-Agent', USER_AGENT)]
            urllib.request.install_opener(opener)

            request = urllib.request.Request(self.url)
            response = urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT)

            content = response.read()
            content_str = content.decode('utf-8', errors='ignore')

            # Валидация контента
            if not validate_m3u(content_str):
                self.finished.emit(False, "Файл не является плейлистом")
                return

            if self._running:
                with open(self.file_path, 'wb') as f:
                    f.write(content)

                self.finished.emit(True, "")
        except Exception as e:
            if self._running:
                self.finished.emit(False, str(e))

    @safe_call()
    def stop(self):
        self._running = False


class ImageDownloadThread(QThread):
    """Поток для асинхронной загрузки изображений (TV логотипов)"""
    finished = Signal(str, object)  # url, QPixmap

    def __init__(self, url, channel_name):
        super().__init__()
        self.url = url
        self.channel_name = channel_name
        self._running = True

    @safe_call(silent=True)
    def run(self):
        # Загружаем изображение с правильными заголовками
        opener = urllib.request.build_opener()
        opener.addheaders = [
            ('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'),
            ('Accept', 'image/webp,image/apng,image/*,*/*;q=0.8'),
            ('Accept-Language', 'en-US,en;q=0.9'),
            ('Referer', 'http://www.google.com/')
        ]
        urllib.request.install_opener(opener)

        request = urllib.request.Request(self.url)
        response = urllib.request.urlopen(request, timeout=5)
        data = response.read()

        if not self._running:
            return

        # Создаем QPixmap из данных
        pixmap = QPixmap()
        pixmap.loadFromData(data)

        # Масштабируем до 32x32, сохраняя соотношение сторон
        if not pixmap.isNull():
            pixmap = pixmap.scaled(
                CHANNEL_ICON_SIZE, CHANNEL_ICON_SIZE,
                Qt.KeepAspectRatio, Qt.SmoothTransformation
            )

        if self._running:
            self.finished.emit(self.url, pixmap)

    def stop(self):
        """Graceful shutdown"""
        self._running = False


class MPVPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self._setup_window()
        self._init_data_structures()
        self._cleanup_threads = []

        # Загружаем данные плейлистов
        self.load_playlists_data()

        # Создаем UI и MPV
        self.init_ui()
        self.init_mpv()

        # Инициализация
        self.show()
        self._schedule_initial_load()

    def _setup_window(self):
        """Настройка окна"""
        self.setWindowIcon(QIcon("maksiptv.ico"))
        self.setWindowTitle(WINDOW_TITLE)
        self.setGeometry(*WINDOW_GEOMETRY)
        self.setMinimumSize(*WINDOW_MIN_SIZE)

    def _init_data_structures(self):
        """Инициализация всех структур данных"""
        self.channels = []
        self.categories = {CATEGORY_ALL: []}
        self.current_category = CATEGORY_ALL
        self.current_channel = ""
        self.current_channel_url = ""

        # Состояние
        self.is_toggling_fullscreen = False
        self.last_fullscreen_toggle = 0
        self.is_fullscreen = False
        self.initializing_ui = True
        self._is_closing = False

        # Управление плейлистами
        self.playlist_files = []
        self.playlists_data = {}
        self.last_playlist = None

        # Кэш иконок
        self.channel_icons = {}
        self.pending_icon_downloads = {}
        self.icon_download_queue = []
        self.icon_stats = {'loaded': 0, 'failed': 0, 'cache': 0}
        self.max_concurrent_downloads = MAX_CONCURRENT_DOWNLOADS
        self._active_threads = []  # Список активных потоков

    def _schedule_initial_load(self):
        """Запланировать загрузку плейлиста после инициализации UI"""
        QTimer.singleShot(UI_INIT_DELAY_MS, self._load_initial_playlist)
        QTimer.singleShot(POST_INIT_DELAY_MS, self._complete_ui_init)

    @safe_call()
    def _complete_ui_init(self):
        """Завершить инициализацию UI"""
        self.initializing_ui = False

    @safe_call()
    def init_mpv(self):
        """Инициализация MPV плеера"""
        # Создаем MPV с оптимальными настройками для IPTV
        wid = str(int(self.video_frame.winId()))
        self.player = mpv.MPV(wid=wid, **MPV_SETTINGS)

        # Обработчики событий
        @self.player.event_callback('file-loaded')
        def on_loaded(event):
            self._on_mpv_file_loaded()

        @self.player.event_callback('end-file')
        def on_end(event):
            self._on_mpv_end_file(event)

        print("="*60)
        print("MPV Player initialized successfully")
        print("="*60)

    def _on_mpv_file_loaded(self):
        """Обработка загрузки файла MPV"""
        print(f"Loaded: {self.current_channel}")
        self.status_label.setText(f"Воспроизводится: {self.current_channel}")
        self.progress_bar.setVisible(False)

    def _on_mpv_end_file(self, event):
        """Обработка завершения файла MPV"""
        print("Playback ended")
        try:
            event_data = event.as_dict()
            if event_data.get('event', {}).get('reason') == 'error':
                self.status_label.setText("Ошибка воспроизведения")
        except Exception:
            pass

    def init_ui(self):
        """Создание UI компонентов"""
        self._setup_central_widget()
        self._create_menu()

        # Панели
        self.left_panel = self.create_left_panel()
        self.right_panel = self.create_right_panel()

        # Splitter
        self._setup_splitter()

        # Создаем контролы отложенно (улучшение перформанса)
        QTimer.singleShot(UI_INIT_DELAY_MS, self._create_control_panel_later)

    def _setup_central_widget(self):
        """Настройка центрального виджета"""
        central = QWidget()
        self.setCentralWidget(central)
        self.main_layout = QHBoxLayout(central)
        self.main_layout.setContentsMargins(6, 6, 6, 6)

    def _create_menu(self):
        """Создание меню"""
        self.main_menubar = self.menuBar()
        self._create_file_menu()
        self._create_view_menu()

    def _create_file_menu(self):
        """Файловое меню"""
        file_menu = self.main_menubar.addMenu("Файл")

        add_action = QAction("Добавить плейлист...", self)
        add_action.triggered.connect(self.add_playlist_dialog)
        file_menu.addAction(add_action)

        file_menu.addSeparator()

        exit_action = QAction("Выход", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    def _create_view_menu(self):
        """Меню Вид"""
        view_menu = self.main_menubar.addMenu("Вид")

        fullscreen_action = QAction("Полноэкранный режим (F11)", self)
        fullscreen_action.triggered.connect(self.toggle_fullscreen)
        view_menu.addAction(fullscreen_action)

    def _setup_splitter(self):
        """Настройка splitter"""
        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.addWidget(self.left_panel)
        self.main_splitter.addWidget(self.right_panel)
        self.main_splitter.setSizes([300, 800])
        self.main_layout.addWidget(self.main_splitter)


    @safe_call()
    def create_left_panel(self):
        """Левая панель с каналами"""
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(6, 6, 6, 6)

        # Заголовок
        self._create_channels_header(left_layout)

        # Категории
        self._create_category_controls(left_layout)

        # Поиск
        self._create_search_controls(left_layout)

        # Список каналов
        self._create_channel_list(left_layout)

        # Информация
        self._create_info_label(left_layout)

        return left_panel

    def _create_channels_header(self, layout):
        """Создать заголовок каналов"""
        header = make_label("КАНАЛЫ", align=Qt.AlignCenter, style_type='header')
        layout.addWidget(header)

    def _create_category_controls(self, layout):
        """Создать контролы категорий"""
        cat_layout = make_layout('hbox', spacing=6)
        cat_layout.addWidget(make_label("Категория:"))
        self.category_combo = make_combo_box(callback=self.filter_channels, min_width=150)
        cat_layout.addWidget(self.category_combo)
        layout.addLayout(cat_layout)

    def _create_search_controls(self, layout):
        """Создать контролы поиска"""
        search_layout = make_layout('hbox', spacing=6)
        search_layout.addWidget(make_label("Поиск:"))
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Поиск каналов...")
        self.search_box.textChanged.connect(self.filter_channels)
        search_layout.addWidget(self.search_box)
        layout.addLayout(search_layout)

    def _create_channel_list(self, layout):
        """Создать список каналов"""
        self.channel_list = QListWidget()
        self.channel_list.itemDoubleClicked.connect(self.on_channel_double_clicked)
        self.channel_list.setIconSize(QSize(CHANNEL_ICON_SIZE, CHANNEL_ICON_SIZE))
        self.channel_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {COLORS['background']};
                alternate-background-color: {COLORS['background_alt']};
                color: {COLORS['text']};
            }}
            QListWidget::item {{
                padding: 4px;
                padding-left: 8px;
                height: 40px;
            }}
            QListWidget::item:selected {{
                background-color: {COLORS['accent']};
            }}
        """)
        layout.addWidget(self.channel_list)

    def _create_info_label(self, layout):
        """Создать информационную метку"""
        self.info_label = make_label("Всего каналов: 0", align=Qt.AlignCenter)
        layout.addWidget(self.info_label)

    @safe_call()
    def create_right_panel(self):
        """Правая панель с видео и управлением"""
        right_panel = QWidget()
        self.right_layout = QVBoxLayout(right_panel)
        self.right_layout.setContentsMargins(8, 8, 8, 8)

        # Управление плейлистом
        self._create_playlist_controls(self.right_layout)

        # Название канала
        self._create_channel_name_label(self.right_layout)

        # Видео фрейм
        self._create_video_frame(self.right_layout)

        # Панель управления создается отложенно в init_ui()

        # Статус и прогресс
        self._create_status_and_progress(self.right_layout)

        return right_panel

    def _create_playlist_controls(self, layout):
        """Создать контролы управления плейлистом"""
        playlist_layout = make_layout('hbox')

        self.playlist_label = make_label("Плейлист:")
        playlist_layout.addWidget(self.playlist_label)

        self.playlist_combo = make_combo_box(callback=self.on_playlist_changed, min_width=200)
        self.update_playlist_list()
        playlist_layout.addWidget(self.playlist_combo)

        # Кнопки управления плейлистом
        self.btn_update_playlist = self.create_icon_button(
            'fa5s.sync', '🔄', 'Обновить плейлист', self.on_update_playlist_clicked
        )
        self.btn_update_playlist.setEnabled(False)
        playlist_layout.addWidget(self.btn_update_playlist)

        self.btn_delete_playlist = self.create_icon_button(
            'fa5s.trash', '🗑', 'Удалить плейлист', self.on_delete_playlist_clicked
        )
        self.btn_delete_playlist.setEnabled(True)
        playlist_layout.addWidget(self.btn_delete_playlist)

        playlist_layout.addStretch()
        layout.addLayout(playlist_layout)

    def _create_channel_name_label(self, layout):
        """Создать метку названия канала"""
        self.channel_name_label = make_label("Выберите канал", align=Qt.AlignCenter, style_type='channel_name')
        layout.addWidget(self.channel_name_label)

    def _create_video_frame(self, layout):
        """Создать видео фрейм"""
        self.video_frame = QWidget()
        self.video_frame.setMinimumSize(*VIDEO_FRAME_MIN_SIZE)
        self.video_frame.setStyleSheet("background-color: black;")

        # Двойной клик для полноэкранного режима
        def safe_double_click(e):
            print("Double click detected on video frame")
            self.toggle_fullscreen()

        self.video_frame.mouseDoubleClickEvent = safe_double_click
        layout.addWidget(self.video_frame, 1)

    def _create_status_and_progress(self, layout):
        """Создать метку статуса и прогресс-бар"""
        self.status_label = make_label("Готов", align=Qt.AlignCenter)
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumHeight(4)
        layout.addWidget(self.progress_bar)

    def _create_control_panel_later(self):
        """Отложенное создание панели управления для предотвращения зависания UI"""
        try:
            print("Creating control panel asynchronously...")
            self.control_panel = self.create_control_panel()
            self.right_layout.addWidget(self.control_panel)
            print("Control panel created successfully")
        except Exception as e:
            print(f"Error creating control panel: {e}")
            # Fallback на простые кнопки если иконки вызывают проблемы
            try:
                self.control_panel = self._create_simple_control_panel()
                self.right_layout.addWidget(self.control_panel)
                print("Created simple control panel as fallback")
            except Exception as e2:
                print(f"Even fallback failed: {e2}")

    def _create_simple_control_panel(self):
        """Простая панель управления без qtawesome (fallback)"""
        panel = QWidget()
        panel.setStyleSheet("background-color: #2d2d2d; border-radius: 4px;")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)

        # Простые текстовые кнопки
        self.btn_play = QPushButton("▶")
        self.btn_play.setToolTip("Воспроизвести (Пробел)")
        self.btn_play.clicked.connect(self.play_selected)

        self.btn_stop = QPushButton("⏹")
        self.btn_stop.setToolTip("Стоп")
        self.btn_stop.clicked.connect(self.stop_playback)

        self.btn_fullscreen = QPushButton("⛶")
        self.btn_fullscreen.setToolTip("Полноэкранный режим (F11 или двойной клик)")
        self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)

        for btn in [self.btn_play, self.btn_stop, self.btn_fullscreen]:
            btn.setFixedSize(36, 36)
            layout.addWidget(btn)

        layout.addStretch()

        # Ползунок громкости (простой, без qtawesome)
        volume_layout = QHBoxLayout()
        volume_layout.addWidget(QLabel("🔊"))
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setMinimum(0)
        self.volume_slider.setMaximum(100)
        self.volume_slider.setValue(70)
        self.volume_slider.setFixedWidth(150)
        self.volume_slider.setToolTip("Громкость")
        self.volume_slider.valueChanged.connect(self.on_volume_changed)
        volume_layout.addWidget(self.volume_slider)
        volume_label = QLabel("70%")
        self.volume_label = volume_label
        volume_layout.addWidget(volume_label)
        layout.addLayout(volume_layout)

        return panel

    def create_control_panel(self):
        """Панель управления"""
        panel = QWidget()
        panel.setStyleSheet("background-color: #2d2d2d; border-radius: 4px;")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)

        # Кнопки воспроизведения/остановки
        self.btn_play = self.create_icon_button('fa5s.play', '▶', 'Воспроизвести (Пробел)', self.play_selected)
        self.btn_stop = self.create_icon_button('fa5s.stop', '⏹', 'Стоп', self.stop_playback)

        layout.addWidget(self.btn_play)
        layout.addWidget(self.btn_stop)
        layout.addStretch()

        # Ползунок громкости
        volume_layout = QHBoxLayout()
        volume_layout.addWidget(QLabel("🔊"))
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setMinimum(0)
        self.volume_slider.setMaximum(100)
        self.volume_slider.setValue(70)  # Значение по умолчанию
        self.volume_slider.setFixedWidth(150)
        self.volume_slider.setToolTip("Громкость")
        self.volume_slider.valueChanged.connect(self.on_volume_changed)
        volume_layout.addWidget(self.volume_slider)
        volume_label = QLabel("70%")
        self.volume_label = volume_label
        volume_layout.addWidget(volume_label)
        layout.addLayout(volume_layout)

        self.btn_fullscreen = self.create_icon_button('fa5s.expand-arrows-alt', '⛶', 'Полноэкранный режим (F11 или двойной клик)', self.toggle_fullscreen)
        layout.addWidget(self.btn_fullscreen)

        return panel

    @safe_call()
    def load_playlist(self, filepath):
        """Загрузка плейлиста"""
        if not os.path.exists(filepath):
            print(f"Playlist not found: {filepath}")
            return

        # Очищаем старые данные
        self._cleanup_channels_and_threads()

        # Парсим плейлист
        self.channels = []
        self.categories = {CATEGORY_ALL: []}

        current_name, current_group, current_logo = None, None, None

        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                if line.startswith('#EXTINF'):
                    current_name, current_group, current_logo = parse_m3u_line(line)
                elif not line.startswith('#') and current_name:
                    channel = Channel(
                        name=current_name,
                        url=line,
                        group=current_group or CATEGORY_NONE,
                        logo=current_logo
                    )

                    self.channels.append(channel)
                    self.categories[CATEGORY_ALL].append(channel)

                    if current_group:
                        if current_group not in self.categories:
                            self.categories[current_group] = []
                        self.categories[current_group].append(channel)

                    current_name, current_group, current_logo = None, None, None

        # Обновляем UI
        self.update_categories()
        self.filter_channels()

        print(f"Loaded {len(self.channels)} channels in {len(self.categories)} categories")

    @safe_call()
    def _cleanup_channels_and_threads(self):
        """Очистка каналов и остановка потоков загрузки иконок"""
        # Останавливаем активные загрузки иконок
        for thread in self.pending_icon_downloads.values():
            thread.stop()

        self.pending_icon_downloads.clear()
        self.channel_icons.clear()
        self.icon_download_queue.clear()
        self.icon_stats = {'loaded': 0, 'failed': 0, 'cache': 0}

    def update_categories(self):
        """Обновление списка категорий"""
        self.category_combo.clear()
        self.category_combo.addItems(sorted(self.categories.keys()))

        if "Все каналы" in self.categories:
            self.category_combo.setCurrentText("Все каналы")

    def filter_channels(self):
        """Фильтрация каналов по категории и поиску с отображением иконок"""
        self.channel_list.clear()

        try:
            # Получаем категорию
            category = self.category_combo.currentText()
            if not category or category not in self.categories:
                return

            channels = self.categories[category]

            # Применяем поиск
            search_text = self.search_box.text().lower()
            if search_text:
                channels = [ch for ch in channels if search_text in ch.name.lower()]

            # Добавляем в список с иконками
            for channel in channels:
                item = QListWidgetItem(channel.name)
                self.channel_list.addItem(item)

                # Устанавливаем иконку (загружаем асинхронно или используем fallback)
                self.get_channel_icon(channel.logo, channel.name, item)

            self.info_label.setText(f"Показано каналов: {len(channels)} из {len(self.channels)}")
        except Exception as e:
            print(f"Error in filter_channels: {e}")

    def on_channel_double_clicked(self, item):
        """Двойной клик по каналу"""
        try:
            channel_name = item.text()
            self.play_channel(channel_name)
        except Exception as e:
            print(f"Error in on_channel_double_clicked: {e}")

    @safe_call()
    def play_selected(self):
        """Воспроизвести выбранный канал"""
        current_item = self.channel_list.currentItem()
        if current_item:
            self.play_channel(current_item.text())

    @safe_call()
    def play_channel(self, channel_name):
        """Воспроизведение канала с MPV"""
        # Находим канал
        channel = next((c for c in self.channels if c.name == channel_name), None)
        if not channel:
            return

        self.current_channel = channel_name
        self.current_channel_url = channel.url
        self.channel_name_label.setText(f"▶ {channel_name}")
        self.status_label.setText(f"Загрузка: {channel_name}")
        self.progress_bar.setVisible(True)

        print(f"Playing: {channel_name}")
        print(f"URL: {channel.url}")

        # MPV воспроизведение
        self.player.play(channel.url)

    def stop_playback(self):
        """Остановка"""
        try:
            self.player.stop()
            self.status_label.setText("Остановлено")
            self.channel_name_label.setText("Выберите канал")
            self.current_channel = ""
            self.current_channel_url = ""
            print("Stopped")
        except Exception as e:
            print(f"Error stopping: {e}")

    def get_channel_icon(self, logo_url, channel_name, list_item):
        """Получить иконку канала (из кэша или добавить в очередь)"""
        # Устанавливаем fallback сразу (быстро)
        fallback_icon = self._create_fallback_icon()
        list_item.setIcon(fallback_icon)

        if not logo_url:
            # Нет URL логотипа - оставляем fallback
            return

        # Проверяем кэш
        if logo_url in self.channel_icons:
            list_item.setIcon(self.channel_icons[logo_url])
            self.icon_stats['cache'] += 1
            return

        # Проверяем, не идет ли уже загрузка этого URL
        if logo_url in self.pending_icon_downloads:
            return

        # Добавляем в очередь с небольшой задержкой для избежания падения
        self.icon_download_queue.append({
            'url': logo_url,
            'name': channel_name,
            'item': list_item
        })

        # Откладываем запуск очереди на 100мс, чтобы UI полностью инициализировался
        # иначе может произойти падение при быстром выходе
        QTimer.singleShot(100, self._process_download_queue)

    def _process_download_queue(self):
        """Обработать очередь загрузок (запустить новые потоки, если можно)"""
        # Проверяем, что self все еще существует (приложение может быть в процессе закрытия)
        if not hasattr(self, 'pending_icon_downloads') or self._is_closing:
            return

        if not hasattr(self, 'icon_download_queue'):
            return

        # Запускаем потоки, пока не достигнем лимита и пока есть очередь
        while (len(self.pending_icon_downloads) < self.max_concurrent_downloads and
               self.icon_download_queue):
            try:
                item_data = self.icon_download_queue.pop(0)

                # Повторная проверка
                url = item_data['url']
                if url in self.pending_icon_downloads or url in self.channel_icons:
                    continue

                # Проверяем, что list_item все еще существует
                if not item_data['item']:
                    continue

                download_thread = ImageDownloadThread(url, item_data['name'])
                # Надежное соединение с сохранением данных
                download_thread.finished.connect(
                    lambda url, pixmap, item=item_data['item']: self._on_icon_loaded(url, pixmap, item)
                )

                # Сохраняем сильные ссылки
                self.pending_icon_downloads[url] = download_thread
                self._active_threads.append(download_thread)

                # Запускаем
                download_thread.start()
            except IndexError:
                # Очередь пуста
                break

    def _on_icon_loaded(self, url, pixmap, list_item):
        """Обработчик загрузки иконки"""
        # Проверяем, что self все еще существует и приложение не закрывается
        if (not hasattr(self, 'pending_icon_downloads') or
            self._is_closing or
            not hasattr(self, '_process_download_queue')):
            return

        # Удаляем из списка активных загрузок
        if url in self.pending_icon_downloads:
            del self.pending_icon_downloads[url]

        try:
            # Проверяем, что объект list_item еще существует
            # Дополнительная проверка через Sip API
            if list_item is None:
                return

            # Проверяем, что это действительно QListWidgetItem
            from PySide6.QtWidgets import QListWidgetItem
            if not isinstance(list_item, QListWidgetItem):
                return

            if pixmap and not pixmap.isNull():
                # Создаем иконку из pixmap
                icon = QIcon(pixmap)
                self.channel_icons[url] = icon
                list_item.setIcon(icon)
                self.icon_stats['loaded'] += 1
            else:
                # Ошибка загрузки - используем fallback
                icon = self._create_fallback_icon()
                self.channel_icons[url] = icon
                list_item.setIcon(icon)
                self.icon_stats['failed'] += 1
        except (RuntimeError, AttributeError):
            # Объект list_item был удален (пользователь сменил категорию/поиск)
            # Игнорируем ошибку
            pass
        except Exception as e:
            # Любая другая ошибка - тоже игнорируем
            print(f"Warning: error setting icon (ignored): {e}")
        finally:
            # Запускаем следующую загрузку из очереди
            if hasattr(self, '_process_download_queue') and not self._is_closing:
                self._process_download_queue()

            # Выводим статистику, когда все загрузки завершены
            if (hasattr(self, 'icon_stats') and
                not self.pending_icon_downloads and
                not self.icon_download_queue and
                (self.icon_stats['loaded'] + self.icon_stats['failed']) > 0):
                print(f"Icon loading complete: {self.icon_stats['loaded']} loaded, "
                      f"{self.icon_stats['failed']} failed, {self.icon_stats['cache']} from cache")
                self.icon_stats = {'loaded': 0, 'failed': 0, 'cache': 0}

    def _create_fallback_icon(self):
        """Создать fallback иконку для каналов без логотипа"""
        # Создаем простую иконку с буквой "TV"
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor("transparent"))

        from PySide6.QtGui import QPainter, QFont

        painter = QPainter(pixmap)
        painter.setPen(QColor("#4080b0"))
        painter.setFont(QFont("Arial", 8))
        painter.drawText(pixmap.rect(), Qt.AlignCenter, "TV")
        painter.end()

        return QIcon(pixmap)

    def _create_playlist_control_buttons(self):
        """Создать кнопки управления плейлистом (обновить, удалить)"""
        # Кнопка обновления плейлиста
        self.btn_update_playlist = self.create_icon_button('fa5s.sync', '🔄', 'Обновить плейлист', self.on_update_playlist_clicked)
        self.btn_update_playlist.setEnabled(False)  # По умолчанию выключена

        # Кнопка удаления плейлиста
        self.btn_delete_playlist = self.create_icon_button('fa5s.trash', '🗑', 'Удалить плейлист', self.on_delete_playlist_clicked)
        self.btn_delete_playlist.setEnabled(True)  # Всегда включена (можно удалить любой плейлист)

    def create_icon_button(self, icon_name, text, tooltip=None, callback=None):
        """Создание кнопки с иконкой или текстом"""
        from PySide6.QtCore import QSize

        btn = QPushButton()

        # Устанавливаем фиксированный размер для квадратных кнопок
        btn.setFixedSize(36, 36)

        # Стилизация кнопок
        btn.setStyleSheet("""
            QPushButton {
                background-color: #3a3a3a;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 0px;
            }
            QPushButton:hover {
                background-color: #4a4a4a;
                border: 1px solid #666;
            }
            QPushButton:pressed {
                background-color: #2a2a2a;
            }
            QPushButton:disabled {
                background-color: #2a2a2a;
                border: 1px solid #444;
            }
        """)

        if HAS_QTA:
            try:
                # Создаем иконку с qtawesome
                icon = qta.icon(icon_name, color='white')
                btn.setIcon(icon)
                btn.setIconSize(QSize(20, 20))
                btn.setToolTip(f"{tooltip or text}")
            except Exception as e:
                print(f"Warning: Could not load icon {icon_name}: {e}")
                btn.setText(text[:2])
                btn.setToolTip(tooltip or text)
        else:
            btn.setText(text[:2])
            btn.setToolTip(tooltip or text)

        if callback:
            btn.clicked.connect(callback)

        return btn

    def _find_playlist_file_by_display_name(self, display_name):
        """Поиск файла плейлиста по отображаемому имени"""
        if not display_name:
            return None
        for filename, data in self.playlists_data.items():
            if data.get('name', filename) == display_name:
                return filename
        return None

    def _find_playlist_display_name(self, playlist_file):
        """Поиск отображаемого имени по файлу плейлиста"""
        if playlist_file in self.playlists_data:
            return self.playlists_data[playlist_file].get('name', playlist_file)
        return os.path.basename(playlist_file)

    def _update_playlist_controls(self, playlist_file):
        """Обновление состояния управляющих элементов плейлиста"""
        if not hasattr(self, 'btn_update_playlist'):
            return

        # Кнопка обновления - включается только для URL-плейлистов
        if playlist_file and playlist_file in self.playlists_data and 'url' in self.playlists_data[playlist_file]:
            self.btn_update_playlist.setEnabled(True)
            url = self.playlists_data[playlist_file]['url']
            self.btn_update_playlist.setToolTip(f"Обновить из URL\n{url}")
        else:
            self.btn_update_playlist.setEnabled(False)
            self.btn_update_playlist.setToolTip("Обновить плейлист (доступно только для URL-плейлистов)")

    def on_playlist_changed(self, playlist_name):
        """Обработка изменения выбранного плейлиста"""
        # Пропускаем обработку во время инициализации UI
        if getattr(self, 'initializing_ui', False):
            return

        if not playlist_name:
            return

        # Проверяем, что UI полностью инициализирован
        if not hasattr(self, 'status_label'):
            return

        # Находим соответствующий файл
        playlist_file = self._find_playlist_file_by_display_name(playlist_name)

        # Сохраняем последний использованный плейлист
        if playlist_file:
            self.last_playlist = playlist_file
            self.save_playlists_data()

        # Обновляем управляющие элементы
        self._update_playlist_controls(playlist_file)

        # Загружаем плейлист
        if playlist_file and os.path.exists(playlist_file):
            print(f"Loading playlist: {playlist_file}")
            self.load_playlist(playlist_file)
            display_name = self._find_playlist_display_name(playlist_file)
            self.status_label.setText(f"Загружен плейлист: {display_name}")
        else:
            self.status_label.setText(f"Файл плейлиста не найден: {playlist_name}")

    def on_update_playlist_clicked(self):
        """Обработка нажатия кнопки обновления плейлиста"""
        current_display_name = self.playlist_combo.currentText()
        if not current_display_name:
            QMessageBox.warning(self, "Предупреждение", "Сначала выберите плейлист")
            return

        # Ищем имя файла по отображаемому имени
        playlist_file = self._find_playlist_file_by_display_name(current_display_name)

        if not playlist_file or playlist_file not in self.playlists_data or 'url' not in self.playlists_data[playlist_file]:
            QMessageBox.warning(self, "Предупреждение", "Этот плейлист нельзя обновить (отсутствует URL)")
            return

        # Запрашиваем подтверждение
        url = self.playlists_data[playlist_file]['url']
        display_name = self._find_playlist_display_name(playlist_file)
        reply = QMessageBox.question(
            self,
            "Подтверждение",
            f"Обновить плейлист '{display_name}' из:\n{url}",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            self.update_playlist_from_url(playlist_file)

    def on_delete_playlist_clicked(self):
        """Обработка нажатия кнопки удаления плейлиста"""
        current_display_name = self.playlist_combo.currentText()
        if not current_display_name:
            QMessageBox.warning(self, "Предупреждение", "Сначала выберите плейлист")
            return

        # Ищем имя файла по отображаемому имени
        playlist_file = self._find_playlist_file_by_display_name(current_display_name)

        if not playlist_file or playlist_file not in self.playlists_data:
            QMessageBox.warning(self, "Предупреждение", "Не удалось найти плейлист")
            return

        # Запрашиваем подтверждение
        display_name = self.playlists_data[playlist_file].get('name', playlist_file)
        reply = QMessageBox.question(
            self,
            "Подтверждение удаления",
            f"Удалить плейлист '{display_name}'?\n\nЭто действие удалит запись о плейлисте, но сам файл не будет удален с диска.",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            try:
                # Удаляем из JSON данных
                del self.playlists_data[playlist_file]
                self.save_playlists_data()

                # Удаляем из списка файлов, если есть
                if playlist_file in self.playlist_files:
                    self.playlist_files.remove(playlist_file)

                # Обновляем UI
                self.update_playlist_list()

                self.status_label.setText(f"Плейлист '{display_name}' удален")
                QMessageBox.information(self, "Успех", f"Плейлист '{display_name}' успешно удален из списка!")

                # Если это был последний плейлист, очищаем каналы
                if self.playlist_combo.count() == 0:
                    self.channels = []
                    self.categories = {"Все каналы": []}
                    self.update_categories()
                    self.filter_channels()
                    self.status_label.setText("Плейлист удален. Добавьте новый плейлист.")

            except Exception as e:
                print(f"Error deleting playlist: {e}")
                QMessageBox.critical(self, "Ошибка", f"Не удалось удалить плейлист:\n{e}")

    def on_volume_changed(self, value):
        """Обработка изменения громкости"""
        # Проверяем, что UI полностью инициализирован
        if not hasattr(self, 'volume_label'):
            return

        try:
            # ПРОСТОЕ ПРЯМОЕ ОБРАЩЕНИЕ К MPV (как в старой версии)
            self.player.volume = value
            self.volume_label.setText(f"{value}%")
        except Exception as e:
            print(f"Error setting volume: {e}")

    def load_playlists_data(self):
        """Загрузка данных плейлистов из JSON"""
        if os.path.exists("playlists.json"):
            try:
                with open("playlists.json", 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.playlists_data = data.get('playlists', {})
                    self.last_playlist = data.get('last_playlist', None)
                print(f"Loaded {len(self.playlists_data)} playlists metadata from JSON")
                print(f"Last playlist: {self.last_playlist}")
            except Exception as e:
                print(f"Error loading playlists.json: {e}")
                self.playlists_data = {}
                self.last_playlist = None
        else:
            self.playlists_data = {}
            self.last_playlist = None

    def check_and_update_playlists_on_startup(self):
        """Проверка и обновление плейлистов при запуске"""
        if not self.playlists_data:
            return

        # Для каждого плейлиста с URL, который не обновлялся более 24 часов
        current_time = time.time()
        needs_update = []

        for playlist_name, data in self.playlists_data.items():
            if 'url' in data:
                last_updated = data.get('last_updated', 0)
                if current_time - last_updated > 86400:  # 24 часа в секундах
                    needs_update.append(playlist_name)

        if needs_update:
            print(f"Found {len(needs_update)} playlists that need updating")
            reply = QMessageBox.question(
                self,
                "Обновление плейлистов",
                f"Найдено {len(needs_update)} плейлистов, которые не обновлялись более 24 часов.\n\nОбновить их автоматически?",
                QMessageBox.Yes | QMessageBox.No
            )

            if reply == QMessageBox.Yes:
                for playlist_name in needs_update:
                    print(f"Auto-updating playlist: {playlist_name}")
                    self.update_playlist_from_url(playlist_name)

    def save_playlists_data(self):
        """Сохранение данных плейлистов в JSON"""
        try:
            data = {
                'playlists': self.playlists_data,
                'last_playlist': self.last_playlist
            }
            with open("playlists.json", 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print("Saved playlists metadata to playlists.json")
        except Exception as e:
            print(f"Error saving playlists.json: {e}")

    def update_playlist_from_url(self, playlist_name):
        """Обновление плейлиста из URL в JSON файле"""
        if not playlist_name or playlist_name not in self.playlists_data:
            return False

        playlist_data = self.playlists_data[playlist_name]
        if 'url' not in playlist_data:
            return False

        self.status_label.setText(f"Обновление плейлиста: {playlist_name}...")
        self.progress_bar.setVisible(True)

        # Скачиваем
        self.download_thread = PlaylistDownloadThread(playlist_data['url'], playlist_name)
        self.download_thread.finished.connect(
            lambda success, error: self.on_playlist_updated(success, error, playlist_name)
        )
        self.download_thread.start()
        return True

    def on_playlist_updated(self, success, error, playlist_name):
        """Обработка обновленного плейлиста"""
        self.progress_bar.setVisible(False)

        if success:
            # Обновляем timestamp в JSON
            if playlist_name in self.playlists_data:
                self.playlists_data[playlist_name]['last_updated'] = time.time()
                self.save_playlists_data()

            # Если это текущий плейлист, перезагружаем его
            current_display_name = self.playlist_combo.currentText()
            display_name = self.playlists_data[playlist_name].get('name', playlist_name)
            if current_display_name == display_name:
                self.load_playlist(playlist_name)
                self.status_label.setText(f"Плейлист обновлён: {display_name}")
            else:
                self.status_label.setText(f"Плейлист обновлён: {playlist_name}")

            QMessageBox.information(self, "Успех", f"Плейлист '{display_name}' успешно обновлён!")
        else:
            self.status_label.setText(f"Ошибка обновления: {error}")
            QMessageBox.critical(self, "Ошибка", f"Не удалось обновить плейлист:\n{error}")

    def update_playlist_list(self):
        """Обновление списка доступных плейлистов"""
        self.playlist_combo.clear()
        self.playlist_files = []

        # Добавляем плейлисты из JSON данных с отображаемыми именами
        for filename, data in self.playlists_data.items():
            if os.path.exists(filename):
                display_name = data.get('name', filename)
                self.playlist_combo.addItem(display_name)
                self.playlist_files.append(filename)

        # Устанавливаем текущий плейлист (последний использованный)
        if self.playlist_combo.count() > 0:
            if self.last_playlist and self.last_playlist in self.playlists_data:
                display_name = self.playlists_data[self.last_playlist].get('name', self.last_playlist)
                self.playlist_combo.setCurrentText(display_name)
            else:
                self.playlist_combo.setCurrentIndex(0)

    def toggle_fullscreen(self):
        """
        Переключение полноэкранного режима через Qt
        Используем Qt для управления окном вместо MPV
        """
        if not self.current_channel:
            QMessageBox.information(self, "Информация", "Сначала выберите канал для воспроизведения")
            return

        # Защита от множественных вызовов
        current_time = time.time()
        if self.is_toggling_fullscreen or (current_time - self.last_fullscreen_toggle) < 0.5:
            print("Already toggling fullscreen or too soon, ignoring...")
            return

        try:
            self.is_toggling_fullscreen = True
            self.last_fullscreen_toggle = current_time

            if not self.is_fullscreen:
                # Входим в полноэкранный режим
                print("Entering fullscreen mode...")
                self.is_fullscreen = True

                # Скрываем элементы UI
                self.main_menubar.hide()
                self.left_panel.hide()
                self.channel_name_label.hide()
                self.control_panel.hide()
                self.progress_bar.hide()

                # Скрываем управление плейлистом
                if hasattr(self, 'playlist_combo'):
                    self.playlist_combo.hide()
                if hasattr(self, 'btn_update_playlist'):
                    self.btn_update_playlist.hide()
                if hasattr(self, 'btn_delete_playlist'):
                    self.btn_delete_playlist.hide()
                if hasattr(self, 'playlist_label'):
                    self.playlist_label.hide()

                # Растягиваем видео фрейм
                self.right_layout.setContentsMargins(0, 0, 0, 0)

                # Переходим в полноэкранный режим Qt
                self.showFullScreen()

                self.status_label.setText("Полноэкранный режим (ESC или F11 для выхода)")

            else:
                # Выходим из полноэкранного режима
                print("Exiting fullscreen mode...")
                self.is_fullscreen = False

                # Возвращаем нормальное окно
                self.showNormal()

                # Показываем элементы UI
                self.main_menubar.show()
                self.left_panel.show()
                self.channel_name_label.show()
                self.control_panel.show()

                # Показываем управление плейлистом
                if hasattr(self, 'playlist_combo'):
                    self.playlist_combo.show()
                if hasattr(self, 'btn_update_playlist'):
                    self.btn_update_playlist.show()
                if hasattr(self, 'btn_delete_playlist'):
                    self.btn_delete_playlist.show()
                if hasattr(self, 'playlist_label'):
                    self.playlist_label.show()

                # Восстанавливаем отступы
                self.right_layout.setContentsMargins(8, 8, 8, 8)

                self.status_label.setText(f"Воспроизводится: {self.current_channel}")

            # Сбрасываем флаг через 500мс
            QTimer.singleShot(500, self._reset_fullscreen_flag)

        except Exception as e:
            self.is_toggling_fullscreen = False
            print(f"Error toggling fullscreen: {e}")
            QMessageBox.warning(self, "Ошибка", f"Не удалось переключить режим:\n{e}")

    def _reset_fullscreen_flag(self):
        """Сброс флага переключения полноэкранного режима"""
        self.is_toggling_fullscreen = False
        print("Fullscreen toggle flag reset")

    def _load_initial_playlist(self):
        """Загрузка последнего использованного плейлиста или первого доступного"""
        print(f"=== _load_initial_playlist called ===")
        print(f"Last playlist: {self.last_playlist}")
        print(f"Last playlist exists: {os.path.exists(self.last_playlist) if self.last_playlist else False}")

        # Обновляем список плейлистов
        self.update_playlist_list()
        print(f"Playlist combo count: {self.playlist_combo.count()}")

        # Проверяем, есть ли вообще плейлисты
        if self.playlist_combo.count() == 0:
            print("No playlists found, skipping initial load")
            self.check_and_update_playlists_on_startup()
            return

        # Определяем какой плейлист загружать
        target_file = self.last_playlist if (self.last_playlist and os.path.exists(self.last_playlist)) else self.playlist_files[0]
        display_name = self._find_playlist_display_name(target_file)

        print(f"✓ Loading playlist: {target_file} ({display_name})")
        self.load_playlist(target_file)

        # Устанавливаем правильный элемент без триггеринга on_playlist_changed
        self.initializing_ui = True
        index = self.playlist_combo.findText(display_name)
        if index >= 0:
            print(f"✓ Setting combo box to: {display_name} (index: {index})")
            self.playlist_combo.blockSignals(True)
            self.playlist_combo.setCurrentIndex(index)
            self.playlist_combo.blockSignals(False)
        self.initializing_ui = False

        print(f"=== Initial playlist load complete ===")

        # Проверяем и предлагаем обновить устаревшие плейлисты
        self.check_and_update_playlists_on_startup()

    def add_playlist_dialog(self):
        """Диалог добавления плейлиста"""
        dialog = QDialog(self)
        dialog.setWindowTitle("Добавить плейлист")
        dialog.setFixedSize(500, 250)

        layout = QVBoxLayout(dialog)

        # Вкладки
        tabs = QTabWidget()

        # Вкладка URL
        url_tab = QWidget()
        url_layout = QVBoxLayout(url_tab)

        url_layout.addWidget(QLabel("URL плейлиста:"))
        url_edit = QLineEdit()
        url_edit.setPlaceholderText("https://example.com/playlist.m3u")
        url_layout.addWidget(url_edit)

        url_layout.addWidget(QLabel("Название плейлиста:"))
        name_edit = QLineEdit()
        name_edit.setPlaceholderText("Мой плейлист")
        url_layout.addWidget(name_edit)

        tabs.addTab(url_tab, "Из URL")

        # Вкладка файла
        file_tab = QWidget()
        file_layout = QVBoxLayout(file_tab)

        file_button = QPushButton("Выбрать файл...")
        file_button.clicked.connect(self.select_local_file)
        file_layout.addWidget(file_button)

        # Метка для отображения выбранного файла
        self.selected_file_label = QLabel("Файл не выбран")
        self.selected_file_label.setStyleSheet("color: #a0a0a0; font-style: italic;")
        file_layout.addWidget(self.selected_file_label)

        file_layout.addWidget(QLabel("Название плейлиста:"))
        file_name_edit = QLineEdit()
        file_name_edit.setPlaceholderText("Мой плейлист")
        file_layout.addWidget(file_name_edit)

        # Сохраняем ссылки на виджеты для доступа из других методов
        dialog.file_path = None
        dialog.file_name_edit = file_name_edit
        dialog.file_button = file_button

        file_layout.addStretch()

        tabs.addTab(file_tab, "Из файла")

        layout.addWidget(tabs)

        # Кнопки
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)

        # Определяем какую вкладку выбрали и вызываем соответствующий метод
        def on_dialog_accept():
            current_tab = tabs.currentIndex()
            if current_tab == 0:  # Вкладка URL
                self.load_playlist_from_url(url_edit.text(), dialog, name_edit.text())
            else:  # Вкладка Файл
                self.load_playlist_from_file(dialog)

        button_box.accepted.connect(on_dialog_accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        dialog.exec()

    def select_local_file(self):
        """Выбор локального файла плейлиста"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Выбрать плейлист",
            "",
            "M3U Files (*.m3u *.m3u8);;All Files (*.*)"
        )

        if file_path:
            # Получаем диалог через sender
            dialog = self.sender().parent()
            while not isinstance(dialog, QDialog):
                dialog = dialog.parent()

            # Сохраняем путь файла в диалоге
            dialog.file_path = file_path

            # Обновляем метку с именем файла
            filename = os.path.basename(file_path)
            self.selected_file_label.setText(filename)
            self.selected_file_label.setStyleSheet("color: white;")

    def load_playlist_from_file(self, dialog):
        """Загрузка плейлиста из локального файла"""
        if not dialog.file_path:
            QMessageBox.warning(self, "Предупреждение", "Сначала выберите файл")
            return

        file_path = dialog.file_path
        playlist_name = dialog.file_name_edit.text().strip()

        # Если имя не задано, используем имя файла по умолчанию
        if not playlist_name:
            playlist_name = f"Плейлист {len(self.playlists_data) + 1}"

        # Копируем файл в текущую директорию с уникальным именем
        filename = os.path.basename(file_path)
        base_name, ext = os.path.splitext(filename)

        # Генерируем уникальное имя файла
        i = 1
        new_filename = f"{base_name}_{i}{ext}"
        while os.path.exists(new_filename):
            i += 1
            new_filename = f"{base_name}_{i}{ext}"

        # Копируем файл
        import shutil
        try:
            shutil.copy2(file_path, new_filename)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось скопировать файл:\n{e}")
            return

        # Сохраняем в JSON
        self.playlists_data[new_filename] = {
            'name': playlist_name,
            'last_updated': time.time()
        }
        self.save_playlists_data()
        print(f"Added local playlist: {new_filename} -> {playlist_name}")

        # Загружаем плейлист
        self.load_playlist(new_filename)

        # Обновляем UI
        self.update_playlist_list()
        self.playlist_combo.setCurrentText(playlist_name)

        dialog.accept()
        QMessageBox.information(self, "Успех", f"Плейлист '{playlist_name}' загружен!")

    def load_playlist_from_url(self, url, dialog, playlist_name=None):
        """Загрузка плейлиста из URL"""
        if not url:
            return

        self.status_label.setText("Загрузка плейлиста...")
        self.progress_bar.setVisible(True)

        # Сохраняем имя плейлиста для использования в колбэке
        self.pending_playlist_name = playlist_name

        # Скачиваем
        self.download_thread = PlaylistDownloadThread(url, "downloaded.m3u")
        self.download_thread.finished.connect(
            lambda success, error: self.on_playlist_downloaded(success, error, dialog, url)
        )
        self.download_thread.start()

    def on_playlist_downloaded(self, success, error, dialog, url=None):
        """Обработка загруженного плейлиста"""
        self.progress_bar.setVisible(False)

        if success:
            # Сохраняем с уникальным именем, не перезаписывая local.m3u
            # Сначала пытаемся сохранить как playlist_N.m3u
            i = 1
            new_playlist_name = f"playlist_{i}.m3u"
            while os.path.exists(new_playlist_name):
                i += 1
                new_playlist_name = f"playlist_{i}.m3u"

            os.rename("downloaded.m3u", new_playlist_name)
            self.playlist_files.append(new_playlist_name)

            # Сохраняем в JSON, если это новый плейлист из URL
            if url:
                # Если имя плейлиста не задано, генерируем из URL или используем имя файла
                playlist_display_name = getattr(self, 'pending_playlist_name', None)
                if not playlist_display_name:
                    playlist_display_name = f"Плейлист {i}"

                self.playlists_data[new_playlist_name] = {
                    'name': playlist_display_name,
                    'url': url,
                    'last_updated': time.time()
                }
                self.save_playlists_data()
                print(f"Saved playlist metadata: {new_playlist_name} -> {playlist_display_name}")

            # Очищаем временное имя плейлиста
            if hasattr(self, 'pending_playlist_name'):
                delattr(self, 'pending_playlist_name')

            # Обновляем список плейлистов в UI
            self.update_playlist_list()

            # Автоматически загружаем новый плейлист
            self.load_playlist(new_playlist_name)

            self.status_label.setText("Плейлист загружен")
            dialog.accept()
            if url:
                display_name = self.playlists_data[new_playlist_name].get('name', new_playlist_name)
                QMessageBox.information(self, "Успех", f"Загружено {len(self.channels)} каналов в плейлист '{display_name}'!")
            else:
                QMessageBox.information(self, "Успех", f"Загружено {len(self.channels)} каналов!")
        else:
            self.status_label.setText(f"Ошибка: {error}")
            QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить плейлист:\n{error}")

    def keyPressEvent(self, event: QKeyEvent):
        """Обработка горячих клавиш"""
        if event.key() == Qt.Key_F11:
            self.toggle_fullscreen()
            event.accept()
        elif event.key() == Qt.Key_Escape:
            # ESC выходит из полноэкранного режима
            if self.is_fullscreen:
                self.toggle_fullscreen()
                event.accept()
            else:
                super().keyPressEvent(event)
        elif event.key() == Qt.Key_Space:
            if self.current_channel:
                self.stop_playback()
            else:
                self.play_selected()
            event.accept()
        else:
            super().keyPressEvent(event)

    @safe_call()
    def closeEvent(self, event):
        """При закрытии приложения"""
        print("Cleaning up before exit...")
        self._is_closing = True

        # Останавливаем загрузку иконок
        self._cleanup_channels_and_threads()

        # Закрываем MPV
        try:
            if hasattr(self, 'player'):
                self.player.terminate()
        except Exception as e:
            print(f"Error terminating MPV: {e}")

        event.accept()


if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)

        # Темная тема
        app.setStyle('Fusion')

        player = MPVPlayer()
        sys.exit(app.exec())
    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
