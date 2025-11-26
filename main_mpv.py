"""
IPTV плеер на MPV + PySide6 (Optimized)
- Асинхронная загрузка через QNetworkAccessManager
- Оптимизация памяти (__slots__)
- Левая панель занимает 1/4 экрана по умолчанию
"""

import sys
import os
import json
import time
from typing import Dict, List, Optional
from dataclasses import dataclass
import ctypes

# === НАСТРОЙКА ОКРУЖЕНИЯ ===
# Настройка путей для MPV (порядок: папка скрипта -> папка mpv рядом -> PATH -> Chocolatey)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MPV_DIRS = [
    SCRIPT_DIR,
    os.path.join(SCRIPT_DIR, 'mpv'),
    r"C:\ProgramData\chocolatey\lib\mpvio.install\tools",
]

# Добавляем пути в PATH
for path in MPV_DIRS:
    if os.path.exists(path):
        os.environ["PATH"] = path + os.pathsep + os.environ["PATH"]

# Импорт MPV
try:
    import mpv
except ImportError:
    # Пытаемся загрузить dll вручную, если python-mpv не находит в PATH
    try:
        os.environ["PATH"] = os.getcwd() + os.pathsep + os.environ["PATH"]
        import mpv
    except ImportError:
        pass

# Qt Импорты
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                               QLabel, QPushButton, QListWidget, QListWidgetItem, QSplitter,
                               QComboBox, QLineEdit, QFileDialog, QMessageBox, QDialog,
                               QDialogButtonBox, QTabWidget, QProgressBar, QCheckBox, QSlider)
from PySide6.QtCore import (Qt, QTimer, QSize, QUrl, QByteArray, QBuffer)
from PySide6.QtGui import QKeyEvent, QAction, QPixmap, QColor, QIcon
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

# Иконки (опционально)
try:
    import qtawesome as qta
    HAS_QTA = True
except ImportError:
    HAS_QTA = False

# === КОНСТАНТЫ ===
WINDOW_TITLE = "MaksIPTV Player - MPV Optimized"
WINDOW_GEOMETRY = (100, 50, 1200, 650)
PLAYLISTS_JSON = "playlists.json"
USER_AGENT = b'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'

COLORS = {
    'bg': '#2a2a2a', 'bg_alt': '#383838', 'panel': '#2d2d2d',
    'accent': '#4080b0', 'text': 'white', 'text_dim': '#a0a0a0',
    'btn': '#3a3a3a', 'btn_border': '#555', 'btn_hover': '#4a4a4a'
}

MPV_SETTINGS = {
    'keep_open': 'yes', 'idle': 'yes', 'osc': 'no',
    'cache': 'yes', 'demuxer_max_bytes': '150M',
    'hwdec': 'auto', 'vo': 'gpu', 'msg_level': 'all=error'
}

# === МОДЕЛИ ===
@dataclass
class Channel:
    __slots__ = ['name', 'url', 'group', 'logo'] # Экономия памяти
    name: str
    url: str
    group: str
    logo: Optional[str]

# === УТИЛИТЫ ===
def make_btn(text, func=None, tip=None, icon=None):
    btn = QPushButton()
    if HAS_QTA and icon:
        btn.setIcon(qta.icon(icon, color='white'))
        btn.setToolTip(tip or text)
    else:
        btn.setText(text)
        btn.setToolTip(tip)
    
    if func:
        btn.clicked.connect(func)
    
    btn.setStyleSheet(f"""
        QPushButton {{
            background-color: {COLORS['btn']}; border: 1px solid {COLORS['btn_border']};
            border-radius: 4px; padding: 6px; color: {COLORS['text']}; min-width: 30px;
        }}
        QPushButton:hover {{ background-color: {COLORS['btn_hover']}; }}
        QPushButton:pressed {{ background-color: {COLORS['bg']}; }}
    """)
    return btn

# === ГЛАВНЫЙ КЛАСС ===
class MPVPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        
        # Проверка наличия MPV
        if 'mpv' not in sys.modules:
            QMessageBox.critical(self, "Ошибка", "Библиотека mpv не найдена!\nУстановите: pip install python-mpv\nИ скачайте mpv.dll/exe")
            sys.exit(1)

        self._setup_window()
        self._init_variables()
        self._init_network()
        self.init_ui()
        
        # Отложенная инициализация тяжелых компонентов
        QTimer.singleShot(100, self.init_mpv)
        QTimer.singleShot(200, self.load_playlists_data)

    def _setup_window(self):
        self.setWindowTitle(WINDOW_TITLE)
        self.setGeometry(*WINDOW_GEOMETRY)
        self.setMinimumSize(800, 600)
        self.setStyleSheet(f"background-color: {COLORS['bg']}; color: {COLORS['text']};")
        icon_path = os.path.join(SCRIPT_DIR, "iptv.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

    def _init_variables(self):
        self.channels: List[Channel] = []
        self.categories: Dict[str, List[Channel]] = {"Все каналы": []}
        self.playlists_data = {}
        
        # Состояние
        self.is_fullscreen = False
        self.current_channel = None
        self.mpv_player = None
        self.was_maximized = False
        self.previous_geometry = None
        
        # Кэш иконок
        self.icon_cache = {}
        self.default_icon = self._create_default_icon()
        
        # Таймер для поиска (debounce)
        self.search_timer = QTimer()
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300) # 300мс задержка
        self.search_timer.timeout.connect(self._perform_filter)

    def _init_network(self):
        """Инициализация сетевого менеджера Qt"""
        self.nam = QNetworkAccessManager(self)

    def init_mpv(self):
        """Инициализация движка MPV"""
        try:
            wid = str(int(self.video_frame.winId()))
            self.mpv_player = mpv.MPV(wid=wid, **MPV_SETTINGS)
            start_vol = self.vol_slider.value()
            self.mpv_player.volume = start_vol
            
            @self.mpv_player.event_callback('file-loaded')
            def on_load(event):
                QTimer.singleShot(0, lambda: self.status_label.setText(f"Играет: {self.current_channel}"))
                QTimer.singleShot(0, lambda: self.progress_bar.setVisible(False))

        except Exception as e:
            self.status_label.setText(f"Ошибка MPV: {e}")
            print(f"MPV Init Error: {e}")

    # === UI CONSTRUCTION ===
    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(5, 5, 5, 5)

        # Splitter
        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter)

        # === LEFT PANEL (Channels) ===
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        # Header & Controls
        left_layout.addWidget(QLabel("КАНАЛЫ", alignment=Qt.AlignCenter))
        
        # Категории
        cat_box = QHBoxLayout()
        self.combo_cat = QComboBox()
        self.combo_cat.currentTextChanged.connect(self.filter_channels)
        self.combo_cat.setStyleSheet(f"background: {COLORS['btn']}; color: white; border: 1px solid #555;")
        cat_box.addWidget(QLabel("Кат:"))
        cat_box.addWidget(self.combo_cat, 1)
        left_layout.addLayout(cat_box)

        # Поиск
        search_box = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск...")
        self.search_input.setStyleSheet(f"background: {COLORS['btn']}; color: white; border: 1px solid #555;")
        self.search_input.textChanged.connect(self.search_timer.start)
        search_box.addWidget(QLabel("🔍"))
        search_box.addWidget(self.search_input, 1)
        left_layout.addLayout(search_box)

        # Список каналов
        self.list_widget = QListWidget()
        self.list_widget.setIconSize(QSize(32, 32))
        self.list_widget.setStyleSheet(f"""
            QListWidget {{ background: {COLORS['bg_alt']}; border: none; }}
            QListWidget::item:selected {{ background: {COLORS['accent']}; }}
        """)
        self.list_widget.itemDoubleClicked.connect(self.on_channel_click)
        left_layout.addWidget(self.list_widget)
        
        self.lbl_count = QLabel("0 каналов")
        left_layout.addWidget(self.lbl_count)
        
        splitter.addWidget(left_panel)

        # === RIGHT PANEL (Player) ===
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(5, 0, 0, 0)

        # 1. Playlist Controls (Обернули в контейнер)
        self.pl_container = QWidget()  # <--- Контейнер для скрытия
        pl_layout = QHBoxLayout(self.pl_container)
        pl_layout.setContentsMargins(0, 0, 0, 0)
        
        self.combo_pl = QComboBox()
        self.combo_pl.activated.connect(self.on_playlist_change)
        self.combo_pl.setStyleSheet(f"background: {COLORS['btn']}; color: white;")
        
        btn_update = make_btn("🔄", self.on_update_click, "Обновить", "fa5s.sync")
        btn_add = make_btn("➕", self.add_playlist_dialog, "Добавить", "fa5s.plus")
        btn_del = make_btn("🗑", self.on_delete_click, "Удалить", "fa5s.trash")
        
        pl_layout.addWidget(QLabel("Плейлист:"))
        pl_layout.addWidget(self.combo_pl, 1)
        self.cb_autoupdate = QCheckBox("Авто")
        self.cb_autoupdate.setToolTip("Автообновление текущего плейлиста при запуске")
        self.cb_autoupdate.clicked.connect(self._save_state) # Сохраняем состояние при клике
        pl_layout.addWidget(self.cb_autoupdate)
        pl_layout.addWidget(btn_update)
        pl_layout.addWidget(btn_add)
        pl_layout.addWidget(btn_del)
        
        right_layout.addWidget(self.pl_container) # Добавляем контейнер, а не layout

        # 2. Channel Title
        self.lbl_title = QLabel("Выберите канал")
        self.lbl_title.setStyleSheet("font-size: 14pt; font-weight: bold; padding: 5px;")
        self.lbl_title.setAlignment(Qt.AlignCenter)
        right_layout.addWidget(self.lbl_title)

        # 3. Video Area
        self.video_frame = QWidget()
        self.video_frame.setStyleSheet("background: black;")
        self.video_frame.mouseDoubleClickEvent = lambda e: self.toggle_fullscreen()
        right_layout.addWidget(self.video_frame, 1)

        # 4. Controls (Обернули в контейнер)
        self.controls_container = QWidget()
        self.controls_container.setFixedHeight(25)
        controls = QHBoxLayout(self.controls_container)
        controls.setContentsMargins(0, 0, 0, 0)
        
        controls.addWidget(make_btn("▶", self.play_current, "Play", "fa5s.play"))
        controls.addWidget(make_btn("⏹", self.stop_current, "Stop", "fa5s.stop"))
        
        # Volume
        controls.addWidget(QLabel("🔊"))
        
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 130)  # Увеличиваем максимум до 130%
        self.vol_slider.setValue(100)     # Ставим 100% по умолчанию
        self.vol_slider.setFixedWidth(150)
        self.vol_slider.valueChanged.connect(self.set_volume)
        controls.addWidget(self.vol_slider)
        
        # Метка уровня громкости
        self.vol_label = QLabel("100%")
        self.vol_label.setFixedWidth(35)
        controls.addWidget(self.vol_label)
        
        controls.addStretch()
        controls.addWidget(make_btn("⛶", self.toggle_fullscreen, "Full Screen", "fa5s.expand"))
        
        right_layout.addWidget(self.controls_container)
        # 5. Status
        self.status_label = QLabel("Готов")
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(2)
        self.progress_bar.setVisible(False)
        right_layout.addWidget(self.status_label)
        right_layout.addWidget(self.progress_bar)
        
        splitter.addWidget(right_panel)

        # === НАСТРОЙКА РАЗМЕРОВ (1/4 и 3/4) ===
        width = self.width()
        splitter.setSizes([int(width * 0.25), int(width * 0.75)])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        
        # === СПИСОК ЭЛЕМЕНТОВ ДЛЯ СКРЫТИЯ ===
        # Теперь здесь только виджеты, которые умеют делать .hide()
        self.ui_elements = [
            left_panel, 
            self.pl_container,       # Верхняя панель плейлиста
            self.lbl_title,          # Заголовок
            #self.controls_container, # Нижняя панель кнопок
            self.status_label        # Статус бар
        ]
    # === LOGIC: PLAYLISTS ===
    def load_playlists_data(self):
        """Загрузка метаданных плейлистов"""
        auto_update_enabled = False
        if os.path.exists(PLAYLISTS_JSON):
            try:
                with open(PLAYLISTS_JSON, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.playlists_data = data.get('playlists', {})
                    last = data.get('last_playlist')
                    # Загружаем состояние автообновления
                    auto_update_enabled = data.get('auto_update', False)
            except Exception:
                self.playlists_data = {}
                last = None
        else:
            last = None
        # Устанавливаем галочку в UI
        self.cb_autoupdate.setChecked(auto_update_enabled)    
        self._refresh_playlist_combo()
        
        # Восстановление последнего плейлиста
        target_pl = None
        if last and last in self.playlists_data:
            target_pl = last
        elif self.playlists_data:
            target_pl = list(self.playlists_data.keys())[0]
            
        if target_pl:
            idx = self.combo_pl.findText(self.playlists_data[target_pl]['name'])
            if idx >= 0:
                self.combo_pl.setCurrentIndex(idx)
                
                # === ЛОГИКА АВТООБНОВЛЕНИЯ ===
                # Если галочка стоит И у плейлиста есть URL -> обновляем
                if auto_update_enabled and 'url' in self.playlists_data[target_pl]:
                    print("Автообновление запущено...")
                    self.status_label.setText("Автообновление плейлиста...")
                    # Вызываем загрузку из сети
                    self._download_playlist(
                        self.playlists_data[target_pl]['url'], 
                        self.playlists_data[target_pl]['name']
                    )
                else:
                    # Иначе просто грузим файл с диска
                    self.load_playlist_file(target_pl)
                    
    def load_playlist_file(self, filename):
        """Парсинг M3U файла"""
        if not os.path.exists(filename):
            self.status_label.setText("Файл плейлиста не найден")
            return

        self.channels.clear()
        self.categories = {"Все каналы": []}
        
        try:
            with open(filename, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                
            name, group, logo = None, "Разное", None
            
            for line in lines:
                line = line.strip()
                if not line: continue
                
                if line.startswith("#EXTINF"):
                    # group-title="..."
                    g_start = line.find('group-title="')
                    if g_start != -1:
                        g_end = line.find('"', g_start + 13)
                        group = line[g_start+13:g_end]
                    else:
                        group = "Разное"
                    
                    # tvg-logo="..."
                    l_start = line.find('tvg-logo="')
                    if l_start != -1:
                        l_end = line.find('"', l_start + 10)
                        logo = line[l_start+10:l_end]
                    else:
                        logo = None
                    
                    # Имя канала (после запятой)
                    comma = line.rfind(',')
                    name = line[comma+1:].strip() if comma != -1 else "Неизвестный канал"
                    
                elif not line.startswith("#"):
                    if name:
                        ch = Channel(name, line, group, logo)
                        self.channels.append(ch)
                        self.categories["Все каналы"].append(ch)
                        
                        if group not in self.categories:
                            self.categories[group] = []
                        self.categories[group].append(ch)
                        
                        name = None # Сброс

            self.update_categories_ui()
            self.filter_channels()
            self.status_label.setText(f"Загружено {len(self.channels)} каналов")
            
            # Сохраняем как последний
            self._save_state(last_pl=filename)

        except Exception as e:
            self.status_label.setText(f"Ошибка парсинга: {e}")

    # === LOGIC: UI UPDATES ===
    def update_categories_ui(self):
        self.combo_cat.blockSignals(True)
        self.combo_cat.clear()
        cats = sorted(self.categories.keys())
        if "Все каналы" in cats:
            cats.remove("Все каналы")
            cats.insert(0, "Все каналы")
        self.combo_cat.addItems(cats)
        self.combo_cat.blockSignals(False)

    def filter_channels(self, *args):
        self.search_timer.stop()
        self._perform_filter()

    def _perform_filter(self):
        cat = self.combo_cat.currentText()
        search = self.search_input.text().lower()
        
        if cat not in self.categories: return
        
        source_list = self.categories[cat]
        self.list_widget.clear()
        
        count = 0
        for ch in source_list:
            if search and search not in ch.name.lower():
                continue
            
            item = QListWidgetItem(ch.name)
            item.setData(Qt.UserRole, ch)
            
            item.setIcon(self.default_icon)
            if ch.logo:
                self._load_icon_async(ch.logo, item)
            
            self.list_widget.addItem(item)
            count += 1
            if count > 500 and search == "": 
                break 

        self.lbl_count.setText(f"{count} / {len(source_list)}")

    # === LOGIC: ASYNC ICONS (QNAM) ===
    def _create_default_icon(self):
        pix = QPixmap(32, 32)
        pix.fill(Qt.transparent)
        return QIcon(pix)

    def _load_icon_async(self, url, item):
        if url in self.icon_cache:
            item.setIcon(self.icon_cache[url])
            return

        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"User-Agent", USER_AGENT)
        # Принудительно отключаем HTTP/2 (используем HTTP/1.1)
        req.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
        
        reply = self.nam.get(req)
        # Дополнительная защита от SSL ошибок (часто бывает у IPTV провайдеров)
        reply.sslErrors.connect(reply.ignoreSslErrors)
        reply.finished.connect(lambda: self._on_icon_loaded(reply, url, item))

    def _on_icon_loaded(self, reply: QNetworkReply, url: str, item: QListWidgetItem):
        reply.deleteLater()
        if reply.error() == QNetworkReply.NoError:
            data = reply.readAll()
            pix = QPixmap()
            if pix.loadFromData(data):
                icon = QIcon(pix)
                self.icon_cache[url] = icon
                try:
                    if item.listWidget(): 
                        item.setIcon(icon)
                except:
                    pass

    # === LOGIC: PLAYBACK ===
    def on_channel_click(self, item):
        ch: Channel = item.data(Qt.UserRole)
        self.play_channel(ch)

    def play_channel(self, ch: Channel):
        if not self.mpv_player: return
        
        self.current_channel = ch.name
        self.lbl_title.setText(ch.name)
        self.status_label.setText(f"Буферизация: {ch.name}...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        
        self.mpv_player.play(ch.url)

    def play_current(self):
        if self.mpv_player: 
            self.mpv_player.cycle('pause')

    def stop_current(self):
        if self.mpv_player:
            self.mpv_player.stop()
            self.lbl_title.setText("Остановлено")
            self.status_label.setText("Готов")
            self.progress_bar.setVisible(False)

    def set_volume(self, val):
        # Обновляем текст метки
        if hasattr(self, 'vol_label'):
            self.vol_label.setText(f"{val}%")
            
        if self.mpv_player:
            self.mpv_player.volume = val

    def toggle_fullscreen(self):
        # Переключаем флаг
        self.is_fullscreen = not self.is_fullscreen
        
        if self.is_fullscreen:
            # === ВХОД В ПОЛНЫЙ ЭКРАН ===
            
            # 1. Запоминаем текущее состояние перед входом
            self.was_maximized = self.isMaximized()
            
            # Если окно не было развернуто на весь экран, запоминаем его точные размеры
            if not self.was_maximized:
                self.previous_geometry = self.saveGeometry()

            # 2. Скрываем элементы интерфейса
            for w in self.ui_elements:
                if isinstance(w, QWidget): w.hide()
            
            # 3. Включаем полноэкранный режим
            self.showFullScreen()
            
        else:
            # === ВЫХОД ИЗ ПОЛНОГО ЭКРАНА ===
            
            # 1. Возвращаем элементы интерфейса
            for w in self.ui_elements:
                if isinstance(w, QWidget): w.show()
            
            # 2. Восстанавливаем состояние окна
            if self.was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
                # Восстанавливаем точные размеры и положение, если они были сохранены
                if self.previous_geometry:
                    self.restoreGeometry(self.previous_geometry)
                    
    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_F11:
            self.toggle_fullscreen()
        elif event.key() == Qt.Key_Escape and self.is_fullscreen:
            self.toggle_fullscreen()
        elif event.key() == Qt.Key_Space:
            self.play_current()
        else:
            super().keyPressEvent(event)

    # === LOGIC: PLAYLIST MANAGEMENT ===
    def add_playlist_dialog(self):
        d = QDialog(self)
        d.setWindowTitle("Добавить плейлист")
        l = QVBoxLayout(d)
        d.resize(400, 250)
        
        tabs = QTabWidget()
        
        # Tab 1: URL
        t1 = QWidget()
        l1 = QVBoxLayout(t1)
        url_edit = QLineEdit()
        url_edit.setPlaceholderText("http://...")
        name_edit = QLineEdit()
        name_edit.setPlaceholderText("Название")
        l1.addWidget(QLabel("URL:"))
        l1.addWidget(url_edit)
        l1.addWidget(QLabel("Имя:"))
        l1.addWidget(name_edit)
        l1.addStretch()
        tabs.addTab(t1, "Из интернета")
        
        # Tab 2: File
        t2 = QWidget()
        l2 = QVBoxLayout(t2)
        fname_lbl = QLabel("Файл не выбран")
        btn_f = QPushButton("Выбрать файл")
        self._temp_path = None
        
        def pick_f():
            path, _ = QFileDialog.getOpenFileName(d, "Выбрать M3U", "", "Playlist (*.m3u *.m3u8)")
            if path:
                self._temp_path = path
                fname_lbl.setText(os.path.basename(path))
        
        btn_f.clicked.connect(pick_f)
        name_edit_f = QLineEdit()
        name_edit_f.setPlaceholderText("Название")
        l2.addWidget(btn_f)
        l2.addWidget(fname_lbl)
        l2.addWidget(name_edit_f)
        l2.addStretch()
        tabs.addTab(t2, "Локальный файл")
        
        l.addWidget(tabs)
        bbox = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bbox.accepted.connect(d.accept)
        bbox.rejected.connect(d.reject)
        l.addWidget(bbox)
        
        if d.exec():
            if tabs.currentIndex() == 0:
                self._download_playlist(url_edit.text(), name_edit.text())
            else:
                if self._temp_path:
                    self._import_local_playlist(self._temp_path, name_edit_f.text())

    def _download_playlist(self, url, name):
        if not url: return
        self.status_label.setText("Скачивание плейлиста...")
        
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"User-Agent", USER_AGENT)
        # === (ОТКЛЮЧЕНИЕ HTTP/2) ===
        req.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
        reply = self.nam.get(req)
        
        def on_dl():
            reply.deleteLater()
            if reply.error() == QNetworkReply.NoError:
                data = reply.readAll()
                filename = f"playlist_{int(time.time())}.m3u"
                with open(filename, 'wb') as f:
                    f.write(data.data())
                
                real_name = name or "Web Playlist"
                self.playlists_data[filename] = {'name': real_name, 'url': url}
                self._save_state()
                self._refresh_playlist_combo()
                
                idx = self.combo_pl.findText(real_name)
                self.combo_pl.setCurrentIndex(idx)
                self.load_playlist_file(filename)
            else:
                QMessageBox.warning(self, "Ошибка", f"Не удалось скачать: {reply.errorString()}")

        reply.finished.connect(on_dl)

    def _import_local_playlist(self, path, name):
        import shutil
        filename = f"local_{int(time.time())}.m3u"
        shutil.copy(path, filename)
        
        real_name = name or os.path.basename(path)
        self.playlists_data[filename] = {'name': real_name}
        self._save_state()
        self._refresh_playlist_combo()
        
        idx = self.combo_pl.findText(real_name)
        self.combo_pl.setCurrentIndex(idx)
        self.load_playlist_file(filename)

    def on_update_click(self):
        curr_idx = self.combo_pl.currentIndex()
        if curr_idx < 0: return
        
        curr_name = self.combo_pl.currentText()
        filename = None
        for k, v in self.playlists_data.items():
            if v['name'] == curr_name:
                filename = k
                break
        
        if filename and 'url' in self.playlists_data[filename]:
            self._download_playlist(self.playlists_data[filename]['url'], curr_name)
        else:
            QMessageBox.information(self, "Инфо", "Это локальный плейлист, обновление только для веб-ссылок.")

    def on_delete_click(self):
        curr_name = self.combo_pl.currentText()
        if not curr_name: return
        
        if QMessageBox.question(self, "Удалить?", f"Удалить плейлист {curr_name}?") != QMessageBox.Yes:
            return

        filename = None
        for k, v in self.playlists_data.items():
            if v['name'] == curr_name:
                filename = k
                break
        
        if filename:
            del self.playlists_data[filename]
            self._save_state()
            self._refresh_playlist_combo()
            self.channels.clear()
            self.list_widget.clear()

    def _refresh_playlist_combo(self):
        self.combo_pl.clear()
        for k, v in self.playlists_data.items():
            self.combo_pl.addItem(v['name'])

    def on_playlist_change(self, idx):
        if idx < 0: return
        name = self.combo_pl.itemText(idx)
        for k, v in self.playlists_data.items():
            if v['name'] == name:
                self.load_playlist_file(k)
                return

    def _save_state(self, last_pl=None):
        data = {'playlists': self.playlists_data}
        if hasattr(self, 'cb_autoupdate'):
            data['auto_update'] = self.cb_autoupdate.isChecked()
        if last_pl:
            data['last_playlist'] = last_pl
        elif os.path.exists(PLAYLISTS_JSON):
             try:
                 with open(PLAYLISTS_JSON, 'r') as f:
                     old = json.load(f)
                     data['last_playlist'] = old.get('last_playlist')
             except: pass
             
        with open(PLAYLISTS_JSON, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    def closeEvent(self, event):
        if self.mpv_player:
            self.mpv_player.terminate()
        event.accept()

if __name__ == "__main__":
    # === ДОБАВИТЬ ЭТОТ БЛОК (Фикс иконки в панели задач Windows) ===
    if os.name == 'nt':
        myappid = 'mycompany.iptv.player.mpv.1.0' # Любая уникальная строка
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass
    # ==============================================================

    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    # === УСТАНОВКА ГЛОБАЛЬНОЙ ИКОНКИ ПРИЛОЖЕНИЯ ===
    app_icon_path = os.path.join(SCRIPT_DIR, "iptv.ico")
    if os.path.exists(app_icon_path):
        app.setWindowIcon(QIcon(app_icon_path))
    # ==============================================

    player = MPVPlayer()
    player.show()
    sys.exit(app.exec())