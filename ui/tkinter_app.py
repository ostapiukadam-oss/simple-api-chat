import logging
import os
import queue
import re
import shutil
import textwrap
import threading
import time
import tkinter as tk
import traceback
import uuid
import webbrowser
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import urlsplit

import ttkbootstrap as tb
from PIL import Image, ImageGrab, ImageTk
from pygments import lex
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.token import Token

from core.api_client import (
    OpenAIService,
    format_error,
    to_chat_messages,
    unsupported_model_reason,
    uses_chat_completions,
)
from core.attachments import AttachmentStore
from core.config import (
    APP_DIR,
    DB_PATH,
    DEFAULT_MODEL,
    IMAGE_EXTENSIONS,
    INITIAL_MODELS,
    KEY_FILE,
    LEGACY_APP_DIR,
    MAX_BATCH_BYTES,
    MAX_FILE_BYTES,
    SUPPORTED_EXTENSIONS,
)
from core.database import ChatDatabase, DatabaseError
from core.image_generation import ImageGenerationService
from core.pricing import PricingCatalog
from core.security import protect_api_key, unprotect_api_key


UI_COLORS = {
    "app": "#f3f6fa",
    "sidebar": "#edf2f7",
    "surface": "#ffffff",
    "border": "#dbe3ed",
    "text": "#1e293b",
    "muted": "#64748b",
    "accent": "#2563eb",
    "selection": "#dceaff",
    "user_message": "#e8f1ff",
    "code": "#18212b",
}


class ChatApp(tb.Window):
    def __init__(self):
        super().__init__(themename="flatly")

        self.title("Mój czat — OpenAI API")

        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()

        width = min(1200, max(480, screen_width - 100))
        height = min(820, max(420, screen_height - 120))

        self.geometry(f"{width}x{height}")

        self.minsize(
            min(620, width),
            min(500, height),
        )
        self.resizable(True, True)

        self.migrate_legacy_data()
        APP_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=APP_DIR / "app.log",
            level=logging.WARNING,
            format="%(asctime)s %(levelname)s %(message)s",
        )
        self.attachment_store = AttachmentStore(APP_DIR)
        self.pricing = PricingCatalog(APP_DIR / "pricing.json")
        self.prices = self.pricing.prices
        self.db = ChatDatabase(DB_PATH, self.attachment_store)
        self.api_service = OpenAIService()
        self.image_generation_service = ImageGenerationService()

        self.current_id = None
        self.conversation_ids = []
        self.visible_conversation_ids = []
        self.pending_files = []
        self.results = queue.Queue()
        self.stream_active = False
        self.active_stream = None
        self.active_cancel_event = None

        self.busy = False
        self.sidebar_visible = True
        self.resize_job = None

        self.api_key_var = tk.StringVar(value=self.load_api_key())
        self.model_var = tk.StringVar(value=DEFAULT_MODEL)
        self.status_var = tk.StringVar(value="Gotowe")
        self.chat_title_var = tk.StringVar(value="Nowa rozmowa")
        self.files_var = tk.StringVar(value="Brak załączników")
        self.show_key_var = tk.BooleanVar(value=False)
        self.web_search_var = tk.BooleanVar(
            value=self.db.get_setting("web_search_enabled", "0") == "1"
        )
        self.reuse_attachments_var = tk.BooleanVar(
            value=self.db.get_setting("reuse_attachments_enabled", "0") == "1"
        )
        self.last_raw_api_text = ""
        self._tooltip_window = None
        self._conversation_tooltip_job = None
        self.language = self.db.get_setting("language", "pl")
        stored_zoom = self.db.get_setting("answer_zoom")
        try:
            self.answer_zoom = min(160, max(70, int(stored_zoom))) if stored_zoom else 100
        except (TypeError, ValueError):
            self.answer_zoom = 100

        self.build_menu()
        self.build_ui()
        self.apply_language()
        self.refresh_conversations()

        if self.conversation_ids:
            self.open_conversation(self.conversation_ids[0])
        else:
            self.new_conversation()

        self.bind("<Configure>", self.schedule_layout_update)
        self.bind("<Control-f>", self.focus_conversation_search)
        self.bind("<Control-F>", self.focus_conversation_search)
        self.after(100, self.update_layout)
        self.after(100, self.poll_results)

        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self._last_callback_error = None
        self._last_callback_error_at = 0.0

    # ========================================================
    # MENU GŁÓWNE
    # ========================================================

    @staticmethod
    def migrate_legacy_data():
        if not LEGACY_APP_DIR.exists():
            return
        for source in LEGACY_APP_DIR.rglob("*"):
            if not source.is_file():
                continue
            destination = APP_DIR / source.relative_to(LEGACY_APP_DIR)
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def build_menu(self):
        self.menu_bar = tk.Menu(self, tearoff=False)

        self.conversations_menu = tk.Menu(
            self.menu_bar,
            tearoff=False,
        )
        self.menu_bar.add_cascade(
            label="Rozmowy",
            menu=self.conversations_menu,
        )

        self.settings_menu = tk.Menu(self.menu_bar, tearoff=False)
        self.settings_menu.add_command(label="API i modele…", command=self.show_settings)
        self.menu_bar.add_cascade(label="Ustawienia", menu=self.settings_menu)

        edit_menu = tk.Menu(self.menu_bar, tearoff=False)
        self.edit_menu = edit_menu

        edit_menu.add_command(
            label="Kopiuj",
            command=lambda: self.edit_focused("copy"),
        )
        edit_menu.add_command(
            label="Wytnij",
            command=lambda: self.edit_focused("cut"),
        )
        edit_menu.add_command(
            label="Wklej",
            command=lambda: self.edit_focused("paste"),
        )
        edit_menu.add_separator()
        edit_menu.add_command(
            label="Zaznacz wszystko",
            command=lambda: self.edit_focused("select_all"),
        )

        self.menu_bar.add_cascade(
            label="Edycja",
            menu=edit_menu,
        )

        help_menu = tk.Menu(self.menu_bar, tearoff=False)
        self.help_menu = help_menu
        help_menu.add_command(
            label="Skróty i informacje",
            command=self.show_help,
        )
        self.menu_bar.add_cascade(
            label="Pomoc",
            menu=help_menu,
        )

        self.configure(menu=self.menu_bar)

    def show_help(self):
        if self.language == "en":
            messagebox.showinfo(
                "Help",
                "Enter / Ctrl+Enter — send message\n"
                "Shift+Enter — new line\n"
                "Right click — copy, cut and paste\n\n"
                f"Conversation database and attachments:\n{DB_PATH}\n\n"
                "Conversation history is not encrypted. The saved API key is protected "
                "with Windows DPAPI. Files and conversation content are sent to the API "
                "when you make a request.",
                parent=self,
            )
            return
        messagebox.showinfo(
            "Pomoc",
            "Enter / Ctrl+Enter — wyślij wiadomość\n"
            "Shift+Enter — nowa linia\n"
            "PPM — kopiowanie, wycinanie i wklejanie\n\n"
            "Przy wąskim oknie panel boczny jest ukrywany.\n"
            "Rozmowy są wtedy dostępne w menu „Rozmowy”.\n\n"
            f"Historia i załączniki:\n{DB_PATH}\n\n"
            "Baza nie jest szyfrowana. Klucz API nie jest "
            "zapisywany przez aplikację.\n"
            "Załączniki i historia są wysyłane do API "
            "podczas wykonywania zapytań.",
            parent=self,
        )

    def toggle_language(self):
        self.language = "en" if self.language == "pl" else "pl"
        self.db.set_setting("language", self.language)
        self.apply_language()

    def apply_language(self):
        english = self.language == "en"
        text = {
            "title": "My Chat — OpenAI API" if english else "Mój czat — OpenAI API",
            "conversations": "Chats" if english else "Rozmowy",
            "settings_menu": "Settings" if english else "Ustawienia",
            "edit": "Edit" if english else "Edycja",
            "help_menu": "Help" if english else "Pomoc",
            "new": "+ New chat" if english else "+ Nowa rozmowa",
            "search": "Search chats…" if english else "Szukaj rozmów…",
            "rename": "Rename" if english else "Zmień nazwę",
            "delete_chat": "Delete chat" if english else "Usuń rozmowę",
            "settings": "API and models ⚙" if english else "API i modele ⚙",
            "model_short": "Model" if english else "Model",
            "raw_text": "Show raw API text" if english else "Pokaż surowy tekst API",
            "conversation_heading": "Conversation" if english else "Rozmowa",
            "activity_heading": "Date" if english else "Data",
            "input_placeholder": "Write a message… (Enter sends, Shift+Enter adds a line)"
            if english
            else "Napisz wiadomość… (Enter wysyła, Shift+Enter nowa linia)",
            "status_fields": ("Model", "Tokens", "Cost", "Time")
            if english
            else ("Model", "Tokeny", "Koszt", "Czas"),
            "settings_title": "Settings — API and models"
            if english
            else "Ustawienia — API i modele",
            "connection": "Connection" if english else "Połączenie",
            "api_key": "API key:" if english else "Klucz API:",
            "show": "Show" if english else "Pokaż",
            "save_key": "Save API key" if english else "Zapisz klucz API",
            "delete_key": "Delete API key" if english else "Usuń klucz API",
            "model": "Model:" if english else "Model:",
            "get_models": "Load models" if english else "Pobierz modele",
            "internet": "Web search / link analysis" if english else "Internet / analiza linków",
            "reuse_files": "Reuse previous attachments in context"
            if english
            else "Dołącz poprzednie pliki do kontekstu",
            "attach": "Attach files" if english else "Załącz pliki",
            "clear": "Clear" if english else "Wyczyść",
            "paste_image": "Paste image" if english else "Wklej obraz",
            "generate_image": "Generate image" if english else "Generuj obraz",
            "copy": "Copy answer" if english else "Kopiuj odpowiedź",
            "retry": "Retry" if english else "Ponów",
            "send": "Send" if english else "Wyślij",
            "stop": "Stop" if english else "Zatrzymaj",
            "language": "Polski" if english else "English",
            "edit_copy": "Copy" if english else "Kopiuj",
            "edit_cut": "Cut" if english else "Wytnij",
            "edit_paste": "Paste" if english else "Wklej",
            "edit_select": "Select all" if english else "Zaznacz wszystko",
            "help": "Shortcuts and info" if english else "Skróty i informacje",
        }
        self.title(text["title"])
        self.new_button.configure(text=text["new"])
        self.search_placeholder = True
        self.search_entry.delete(0, tk.END)
        self.search_entry.insert(0, text["search"])
        self.search_entry.configure(foreground="#7a8796")
        self.search_entry.bind("<FocusIn>", self.clear_search_placeholder)
        self.search_entry.bind("<FocusOut>", self.restore_search_placeholder)
        self.rename_button.configure(text=text["rename"])
        self.delete_button.configure(text=text["delete_chat"])
        self.settings_button.configure(text=text["settings"])
        self.language_button.configure(text=text["language"])
        self.raw_text_button.configure(text=text["raw_text"])
        self.header_model_label.configure(text=text["model_short"])
        self.conversations.heading("title", text=text["conversation_heading"])
        self.conversations.heading("activity", text=text["activity_heading"])
        self.pricing_link.configure(text="Add price" if english else "Dodaj cenę")
        self.settings_window.title(text["settings_title"])
        self.settings_frame.configure(text=text["connection"])
        self.key_label.configure(text=text["api_key"])
        self.show_key_check.configure(text=text["show"])
        self.save_key_button.configure(text=text["save_key"])
        self.delete_key_button.configure(text=text["delete_key"])
        self.model_label.configure(text=text["model"])
        self.refresh_models_button.configure(text=text["get_models"])
        self.web_search_check.configure(text=text["internet"])
        self.reuse_attachments_check.configure(text=text["reuse_files"])
        self.attach_button.configure(text=text["attach"])
        self.clear_files_button.configure(text=text["clear"])
        self.paste_image_button.configure(text=text["paste_image"])
        self.generate_image_button.configure(text=text["generate_image"])
        self.copy_button.configure(text=text["copy"])
        self.retry_button.configure(text="↻")
        self.send_button.configure(text=text["send"])
        self.stop_button.configure(text=text["stop"])
        self.set_input_placeholder(text["input_placeholder"])
        self.menu_bar.entryconfigure(0, label=text["conversations"])
        self.menu_bar.entryconfigure(1, label=text["settings_menu"])
        self.menu_bar.entryconfigure(2, label=text["edit"])
        self.menu_bar.entryconfigure(3, label=text["help_menu"])
        for index, key in enumerate(("edit_copy", "edit_cut", "edit_paste")):
            self.edit_menu.entryconfigure(index, label=text[key])
        self.edit_menu.entryconfigure(4, label=text["edit_select"])
        self.help_menu.entryconfigure(0, label=text["help"])
        self.settings_menu.entryconfigure(
            0, label=("API and models…" if english else "API i modele…")
        )
        self.refresh_conversations()
        if self.current_id is not None:
            self.chat.configure(state="normal")
            self.chat.delete("1.0", tk.END)
            self._code_header_frames.clear()
            self.chat.configure(state="disabled")
            for role, body in self.db.get_display_messages(self.current_id):
                self.append_chat(role, body)
            self.chat.yview_moveto(0.0)
            self.update_usage_status()
        self.update_files_label()

    def clear_search_placeholder(self, _event=None):
        if getattr(self, "search_placeholder", False):
            self.search_entry.delete(0, tk.END)
            self.search_entry.configure(foreground="#253247")
            self.search_placeholder = False

    def set_input_placeholder(self, text):
        self.input_placeholder_text = text
        if self.input_placeholder_active or not self.input_box.get("1.0", "end-1c").strip():
            self.input_box.delete("1.0", tk.END)
            self.input_box.insert("1.0", text, "input_placeholder")
            self.input_placeholder_active = True

    def clear_input_placeholder(self, _event=None):
        if self.input_placeholder_active:
            self.input_box.delete("1.0", tk.END)
            self.input_placeholder_active = False

    def restore_input_placeholder(self, _event=None):
        if not self.input_box.get("1.0", "end-1c").strip():
            self.input_box.delete("1.0", tk.END)
            self.input_box.insert("1.0", self.input_placeholder_text, "input_placeholder")
            self.input_placeholder_active = True

    def restore_search_placeholder(self, _event=None):
        if not self.search_entry.get().strip():
            self.search_placeholder = True
            self.search_entry.delete(0, tk.END)
            self.search_entry.insert(
                0, "Search chats…" if self.language == "en" else "Szukaj rozmów…"
            )
            self.search_entry.configure(foreground="#7a8796")

    # ========================================================
    # INTERFEJS — ELASTYCZNY UKŁAD GRID
    # ========================================================

    def build_ui(self):
        style = ttk.Style(self)
        self.configure(background=UI_COLORS["app"])
        style.configure(
            "TButton",
            padding=(11, 8),
            font=("Segoe UI", 9),
            focuscolor=UI_COLORS["app"],
        )
        style.configure(
            "primary.TButton",
            padding=(12, 8),
            font=("Segoe UI", 9, "bold"),
            background=UI_COLORS["accent"],
            foreground="#ffffff",
            bordercolor=UI_COLORS["accent"],
        )
        style.map(
            "primary.TButton",
            background=[("pressed", "#1d4ed8"), ("active", "#1d4ed8")],
            bordercolor=[("pressed", "#1d4ed8"), ("active", "#1d4ed8")],
        )
        style.configure(
            "TFrame",
            background=UI_COLORS["app"],
        )
        style.configure("App.TFrame", background=UI_COLORS["app"])
        style.configure(
            "Sidebar.TFrame",
            background=UI_COLORS["sidebar"],
            bordercolor=UI_COLORS["border"],
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Surface.TFrame",
            background=UI_COLORS["surface"],
            bordercolor=UI_COLORS["border"],
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Composer.TFrame",
            background=UI_COLORS["surface"],
            bordercolor=UI_COLORS["border"],
            borderwidth=1,
            relief="solid",
        )
        style.configure("TLabel", font=("Segoe UI", 9), foreground=UI_COLORS["text"])
        style.configure(
            "Header.TLabel",
            background=UI_COLORS["surface"],
            foreground=UI_COLORS["text"],
        )
        style.configure(
            "Sidebar.TLabel",
            background=UI_COLORS["sidebar"],
            foreground=UI_COLORS["text"],
        )
        style.configure(
            "Composer.TLabel",
            background=UI_COLORS["surface"],
            foreground=UI_COLORS["text"],
        )
        style.configure("TLabelframe", padding=8)
        style.configure("TLabelframe.Label", font=("Segoe UI", 9, "bold"))
        style.configure("TEntry", padding=(9, 7))
        style.configure("Search.TEntry", padding=(10, 9))
        style.configure("TCombobox", padding=(7, 5))
        style.configure("Model.TCombobox", padding=(8, 6))
        style.configure(
            "Vertical.TScrollbar",
            arrowsize=11,
            background="#c7d1dd",
            troughcolor="#edf2f7",
            bordercolor="#edf2f7",
            lightcolor="#edf2f7",
            darkcolor="#aab7c5",
        )
        style.configure(
            "Conversations.Treeview",
            rowheight=44,
            font=("Segoe UI", 9),
            background=UI_COLORS["surface"],
            fieldbackground=UI_COLORS["surface"],
            foreground=UI_COLORS["text"],
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Conversations.Treeview.Heading",
            font=("Segoe UI", 8, "bold"),
            padding=(8, 8),
            background=UI_COLORS["sidebar"],
            foreground=UI_COLORS["muted"],
        )
        style.map(
            "Conversations.Treeview",
            background=[("selected", UI_COLORS["selection"])],
            foreground=[("selected", "#173b70")],
        )

        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # Panel boczny.
        self.sidebar = ttk.Frame(self, padding=(12, 12), style="Sidebar.TFrame")
        self.sidebar.grid(
            row=0,
            column=0,
            sticky="nsew",
        )
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar.rowconfigure(2, weight=1)

        self.new_button = tb.Button(
            self.sidebar,
            text="+ Nowa rozmowa",
            command=self.new_conversation,
            bootstyle="primary",
        )
        self.new_button.grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self.on_search_changed)
        self.search_entry = ttk.Entry(
            self.sidebar,
            textvariable=self.search_var,
            font=("Segoe UI", 10),
            style="Search.TEntry",
        )
        self.search_entry.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.search_entry.bind("<Escape>", self.clear_conversation_search)

        list_frame = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        list_frame.grid(
            row=2,
            column=0,
            sticky="nsew",
        )
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.conversations = ttk.Treeview(
            list_frame,
            columns=("title", "activity"),
            show="headings",
            selectmode="browse",
            style="Conversations.Treeview",
        )
        self.conversations.heading("title", text="Rozmowa")
        self.conversations.heading("activity", text="Data")
        self.conversations.column("title", width=150, minwidth=90, stretch=True, anchor="w")
        self.conversations.column("activity", width=66, minwidth=58, stretch=False, anchor="e")
        self.conversations.grid(
            row=0,
            column=0,
            sticky="nsew",
        )
        self.conversations.bind(
            "<<TreeviewSelect>>",
            self.on_conversation_selected,
        )
        self.conversations.bind("<Motion>", self.schedule_conversation_tooltip)
        self.conversations.bind("<Leave>", self.hide_tooltip)

        list_scroll = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.conversations.yview,
        )
        list_scroll.grid(
            row=0,
            column=1,
            sticky="ns",
        )
        self.conversations.configure(yscrollcommand=list_scroll.set)

        self.rename_button = tb.Button(
            self.sidebar,
            text="Zmień nazwę",
            command=self.rename_conversation,
            bootstyle="secondary-outline",
        )
        self.rename_button.grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(8, 4),
        )

        self.delete_button = tb.Button(
            self.sidebar,
            text="Usuń rozmowę",
            command=self.delete_conversation,
            bootstyle="danger-outline",
        )
        self.delete_button.grid(
            row=4,
            column=0,
            sticky="ew",
        )

        # Główna część.
        self.main = ttk.Frame(self, padding=10, style="App.TFrame")
        self.main.grid(
            row=0,
            column=1,
            sticky="nsew",
        )
        self.main.columnconfigure(0, weight=1)

        # Tylko historia czatu przejmuje dodatkową wysokość.
        self.main.rowconfigure(1, weight=1)

        self.settings_window = tk.Toplevel(self)
        self.settings_window.title("Ustawienia — API i modele")
        self.settings_window.resizable(True, False)
        self.settings_window.withdraw()
        self.settings_window.protocol("WM_DELETE_WINDOW", self.settings_window.withdraw)
        settings = ttk.LabelFrame(
            self.settings_window,
            text="Połączenie",
            padding=8,
        )
        settings.pack(fill="both", expand=True, padx=8, pady=8)
        settings.columnconfigure(1, weight=1)

        self.chat_header = ttk.Frame(self.main, padding=(10, 7), style="Surface.TFrame")
        self.chat_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.chat_header.columnconfigure(0, weight=1)
        self.chat_title_label = ttk.Label(
            self.chat_header,
            textvariable=self.chat_title_var,
            font=("Segoe UI", 12, "bold"),
            width=34,
            anchor="w",
            style="Header.TLabel",
        )
        self.chat_title_label.grid(row=0, column=0, sticky="w", padx=(2, 12))
        self.chat_title_label.bind("<Enter>", self.show_current_title_tooltip)
        self.chat_title_label.bind("<Leave>", self.hide_tooltip)
        self.header_model_label = ttk.Label(self.chat_header, text="Model", style="Header.TLabel")
        self.header_model_label.grid(row=0, column=1, sticky="e", padx=(0, 5))
        self.header_model_combo = ttk.Combobox(
            self.chat_header,
            textvariable=self.model_var,
            values=INITIAL_MODELS,
            width=19,
            state="normal",
            exportselection=False,
            style="Model.TCombobox",
        )
        self.header_model_combo.grid(row=0, column=2, sticky="e", padx=(0, 8))
        self.header_model_combo.bind("<<ComboboxSelected>>", self.save_selected_model)
        self.header_model_combo.bind("<FocusOut>", self.save_selected_model)
        self.settings_button = tb.Button(
            self.chat_header,
            text="API i modele ⚙",
            command=self.show_settings,
            takefocus=False,
            bootstyle="secondary-outline",
        )
        self.settings_button.grid(row=0, column=3, sticky="e")

        self.settings_frame = settings
        self.key_label = ttk.Label(settings, text="Klucz API:")
        self.key_label.grid(
            row=0,
            column=0,
            sticky="w",
        )

        self.key_entry = ttk.Entry(
            settings,
            textvariable=self.api_key_var,
            show="*",
            width=15,
            exportselection=False,
        )
        self.key_entry.grid(
            row=0,
            column=1,
            sticky="ew",
            padx=6,
        )

        self.show_key_check = ttk.Checkbutton(
            settings,
            text="Pokaż",
            variable=self.show_key_var,
            command=self.toggle_key_visibility,
        )
        self.show_key_check.grid(
            row=0,
            column=2,
            sticky="w",
        )

        self.save_key_button = tb.Button(
            settings,
            text="Zapisz klucz API",
            command=self.save_api_key,
            bootstyle="primary",
        )
        self.save_key_button.grid(row=0, column=3, padx=(8, 3))

        self.delete_key_button = tb.Button(
            settings,
            text="Usuń klucz API",
            command=self.delete_api_key,
            bootstyle="danger-outline",
        )
        self.delete_key_button.grid(row=0, column=4, padx=3)

        self.model_label = ttk.Label(settings, text="Model:")
        self.model_label.grid(
            row=1,
            column=0,
            sticky="w",
            pady=(8, 0),
        )

        self.model_combo = ttk.Combobox(
            settings,
            textvariable=self.model_var,
            values=INITIAL_MODELS,
            width=15,
            height=20,
            exportselection=False,
        )
        self.model_combo.grid(
            row=1,
            column=1,
            sticky="ew",
            padx=6,
            pady=(8, 0),
        )
        self.model_combo.bind(
            "<<ComboboxSelected>>",
            self.save_selected_model,
        )
        self.model_combo.bind(
            "<FocusOut>",
            self.save_selected_model,
        )

        self.refresh_models_button = tb.Button(
            settings,
            text="Pobierz modele",
            command=self.fetch_models,
            bootstyle="secondary-outline",
        )
        self.refresh_models_button.grid(
            row=1,
            column=2,
            pady=(8, 0),
        )

        self.web_search_check = ttk.Checkbutton(
            settings,
            text="Internet / analiza linków",
            variable=self.web_search_var,
            command=lambda: self.save_boolean_setting("web_search_enabled", self.web_search_var),
        )
        self.web_search_check.grid(row=2, column=1, sticky="w", padx=6, pady=(8, 0))

        self.reuse_attachments_check = ttk.Checkbutton(
            settings,
            text="Dołącz poprzednie pliki do kontekstu",
            variable=self.reuse_attachments_var,
            command=lambda: self.save_boolean_setting(
                "reuse_attachments_enabled", self.reuse_attachments_var
            ),
        )
        self.reuse_attachments_check.grid(row=3, column=1, sticky="w", padx=6, pady=(6, 0))

        self.language_button = tb.Button(
            settings,
            text="English",
            command=self.toggle_language,
            bootstyle="secondary-outline",
        )
        self.language_button.grid(row=4, column=1, sticky="w", padx=6, pady=(8, 0))
        self.raw_text_button = tb.Button(
            settings,
            text="Pokaż surowy tekst API",
            command=self.show_raw_api_text,
            state="disabled",
            bootstyle="secondary-outline",
        )
        self.raw_text_button.grid(row=4, column=2, columnspan=2, sticky="w", pady=(8, 0))

        # Historia.
        self.chat = ScrolledText(
            self.main,
            width=1,
            height=5,
            wrap="word",
            state="disabled",
            font=("Segoe UI", 11),
            background=UI_COLORS["app"],
            foreground=UI_COLORS["text"],
            selectbackground="#315f91",
            selectforeground="#ffffff",
            inactiveselectbackground="#6f98c2",
            insertbackground="#202123",
            padx=12,
            pady=10,
            spacing1=1,
            spacing3=2,
            relief="flat",
            exportselection=False,
        )
        self.chat.grid(
            row=1,
            column=0,
            sticky="nsew",
        )
        self._code_header_frames = []
        self.chat.bind("<Configure>", self.update_code_header_widths, add="+")

        for tag, color in (
            ("user", UI_COLORS["accent"]),
            ("assistant", "#0f766e"),
            ("error", "#b00020"),
        ):
            self.chat.tag_configure(
                tag,
                foreground=color,
                font=("Segoe UI", 11, "bold"),
                spacing1=10,
            )

        self.chat.tag_configure(
            "user_block",
            background=UI_COLORS["user_message"],
            lmargin1=16,
            lmargin2=16,
            rmargin=16,
            spacing1=3,
            spacing3=2,
        )
        self.chat.tag_configure(
            "assistant_block",
            background=UI_COLORS["surface"],
            lmargin1=16,
            lmargin2=16,
            rmargin=16,
            spacing1=3,
            spacing3=2,
        )
        self.chat.tag_configure("bold", font=("Segoe UI", 11, "bold"))
        self.chat.tag_configure("italic", font=("Segoe UI", 11, "italic"))
        self.chat.tag_configure(
            "inline_code", background="#e9edf2", foreground="#9b2453", font=("Consolas", 10)
        )
        self.chat.tag_configure(
            "code_block",
            background="#202832",
            foreground="#e8edf2",
            font=("Consolas", 10),
            lmargin1=30,
            lmargin2=30,
            rmargin=24,
            spacing1=1,
            spacing3=1,
        )
        self.chat.tag_configure("heading1", font=("Segoe UI", 16, "bold"), spacing1=8, spacing3=3)
        self.chat.tag_configure("heading2", font=("Segoe UI", 13, "bold"), spacing1=6, spacing3=2)
        self.chat.tag_configure("heading3", font=("Segoe UI", 11, "bold"), spacing1=4, spacing3=2)
        self.chat.tag_configure(
            "quote",
            foreground="#596b81",
            lmargin1=24,
            lmargin2=24,
            rmargin=16,
            spacing1=1,
            spacing3=2,
        )
        self.chat.tag_configure("list_item", lmargin1=28, lmargin2=46, spacing1=1, spacing3=1)
        self.chat.tag_configure(
            "table",
            font=("Consolas", 9),
            background="#f0f3f7",
            lmargin1=12,
            lmargin2=12,
            rmargin=12,
        )
        self.chat.tag_configure("code_keyword", foreground="#ff8f70", font=("Consolas", 10, "bold"))
        self.chat.tag_configure("code_string", foreground="#b5e48c", font=("Consolas", 10))
        self.chat.tag_configure(
            "code_comment", foreground="#94a3b8", font=("Consolas", 10, "italic")
        )
        self.chat.tag_configure("code_number", foreground="#f6bd60", font=("Consolas", 10))
        self.chat.tag_configure("code_name", foreground="#8bd3dd", font=("Consolas", 10))
        self.chat.tag_configure("code_operator", foreground="#d6b3ff", font=("Consolas", 10))
        self.chat.tag_configure("link", foreground=UI_COLORS["accent"], underline=True)
        # Keep the native selection highlight above message and Markdown
        # background tags so selected text stays visible in read-only mode.
        self.chat.tag_raise("sel")

        # Załączniki.
        self.composer = ttk.Frame(self.main, padding=9, style="Composer.TFrame")
        self.composer.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.composer.columnconfigure(0, weight=1)

        attachments = ttk.Frame(self.composer, style="Composer.TFrame")
        attachments.grid(row=0, column=0, sticky="ew")
        attachments.columnconfigure(2, weight=1)

        self.attach_button = tb.Button(
            attachments,
            text="Załącz pliki",
            command=self.add_files,
            bootstyle="secondary-outline",
        )
        self.attach_button.grid(
            row=0,
            column=0,
        )

        self.clear_files_button = tb.Button(
            attachments,
            text="Wyczyść",
            command=self.clear_files,
            bootstyle="secondary-outline",
        )
        self.clear_files_button.grid(
            row=0,
            column=1,
            padx=5,
        )

        self.files_label = ttk.Label(
            attachments,
            textvariable=self.files_var,
            width=1,
            anchor="w",
            style="Composer.TLabel",
        )
        self.files_label.grid(
            row=0,
            column=2,
            sticky="ew",
        )

        self.attachment_tiles = ttk.Frame(attachments, style="Composer.TFrame")
        self.attachment_tiles.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.attachment_tiles.columnconfigure((0, 1, 2, 3), weight=1)

        self.paste_image_button = tb.Button(
            attachments,
            text="Wklej obraz",
            command=self.paste_clipboard_image,
            bootstyle="secondary-outline",
        )
        self.paste_image_button.grid(row=0, column=3, padx=(5, 0))

        self.generate_image_button = tb.Button(
            attachments,
            text="Generuj obraz",
            command=self.open_image_generator,
            bootstyle="primary-outline",
        )
        self.generate_image_button.grid(row=0, column=4, padx=(5, 0))

        # Pole wiadomości.
        self.input_box = ScrolledText(
            self.composer,
            width=1,
            height=3,
            wrap="word",
            font=("Segoe UI", 11),
            padx=8,
            pady=8,
            undo=True,
            exportselection=False,
            background="#ffffff",
            foreground="#202123",
            insertbackground="#202123",
            relief="flat",
            highlightthickness=1,
            highlightbackground=UI_COLORS["border"],
            highlightcolor=UI_COLORS["accent"],
        )
        self.input_box.grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(7, 0),
        )

        self.input_box.bind("<Return>", self.send_shortcut)
        self.input_box.bind(
            "<Control-Return>",
            self.send_shortcut,
        )
        self.input_box.bind(
            "<Shift-Return>",
            self.insert_newline,
        )
        self.input_box.bind("<Control-Shift-v>", self.paste_image_shortcut)
        self.input_box.bind("<Control-Shift-V>", self.paste_image_shortcut)
        self.input_box.tag_configure("input_placeholder", foreground="#7a8796")
        self.input_placeholder_active = False
        self.input_placeholder_text = "Napisz wiadomość… (Enter wysyła, Shift+Enter nowa linia)"
        self.input_box.bind("<FocusIn>", self.clear_input_placeholder)
        self.input_box.bind("<FocusOut>", self.restore_input_placeholder)
        self.set_input_placeholder(self.input_placeholder_text)

        for scrolled in (self.chat, self.input_box):
            scrollbar = getattr(scrolled, "vbar", None)
            if scrollbar is not None:
                scrollbar.configure(
                    width=10,
                    borderwidth=0,
                    relief="flat",
                    background="#c7d1dd",
                    troughcolor="#edf2f7",
                    activebackground="#91a1b4",
                )

        # Przyciski.
        actions = ttk.Frame(self.composer, style="Composer.TFrame")
        actions.grid(
            row=2,
            column=0,
            sticky="ew",
            pady=(8, 0),
        )
        actions.columnconfigure(0, weight=1)

        self.left_actions = ttk.Frame(actions)
        self.left_actions.grid(row=0, column=0, sticky="w")
        self.copy_button = tb.Button(
            self.left_actions,
            text="Kopiuj odpowiedź",
            command=self.copy_last_answer,
            bootstyle="secondary-outline",
        )
        self.copy_button.grid(row=0, column=0, sticky="w")

        self.zoom_down_button = tb.Button(
            self.left_actions,
            text="A−",
            width=4,
            command=lambda: self.adjust_answer_zoom(-10),
            bootstyle="secondary-outline",
        )
        self.zoom_down_button.grid(row=0, column=1, sticky="w", padx=(14, 3))
        self.zoom_percent_label = ttk.Label(
            self.left_actions,
            text=f"{self.answer_zoom}%",
            width=5,
            anchor="center",
        )
        self.zoom_percent_label.grid(row=0, column=2, sticky="w")
        self.zoom_up_button = tb.Button(
            self.left_actions,
            text="A+",
            width=4,
            command=lambda: self.adjust_answer_zoom(10),
            bootstyle="secondary-outline",
        )
        self.zoom_up_button.grid(row=0, column=3, sticky="w", padx=(3, 0))

        self.retry_button = tb.Button(
            actions,
            text="↻",
            command=self.retry_last_request,
            width=3,
            bootstyle="secondary-outline",
        )
        self.retry_button.grid(row=0, column=1, padx=(8, 5))
        self.retry_button.bind("<Enter>", self.show_retry_tooltip)
        self.retry_button.bind("<Leave>", self.hide_tooltip)

        self.send_button = tb.Button(
            actions,
            text="Wyślij",
            command=self.send_message,
            bootstyle="primary",
        )
        self.send_button.grid(row=0, column=2)

        self.stop_button = tb.Button(
            actions,
            text="Zatrzymaj",
            command=self.stop_generation,
            state="disabled",
            bootstyle="warning-outline",
        )
        self.stop_button.grid(row=0, column=3, padx=(5, 0))
        self.stop_button.grid_remove()

        self.status_line = ttk.Frame(self.composer, style="Composer.TFrame")
        self.status_line.grid(row=3, column=0, sticky="ew", pady=(7, 0))
        self.status_line.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(
            self.status_line,
            textvariable=self.status_var,
            anchor="w",
            style="Composer.TLabel",
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.pricing_link = ttk.Label(
            self.status_line,
            text="Dodaj cenę",
            foreground=UI_COLORS["accent"],
            cursor="hand2",
            style="Composer.TLabel",
        )
        self.pricing_link.grid(row=0, column=1, sticky="e", padx=(8, 12))
        self.pricing_link.bind("<Button-1>", self.open_pricing_file)
        self.pricing_link.grid_remove()
        self.status_summary_var = tk.StringVar(value="")
        self.status_summary_label = ttk.Label(
            self.status_line,
            textvariable=self.status_summary_var,
            anchor="e",
            cursor="question_arrow",
            style="Composer.TLabel",
        )
        self.status_summary_label.grid(row=0, column=2, sticky="e")
        self.status_summary_label.bind("<Enter>", self.show_status_tooltip)
        self.status_summary_label.bind("<Leave>", self.hide_tooltip)

        self.apply_answer_zoom()

        # PPM i zaznaczanie dla pól tekstowych.
        for widget in (
            self.key_entry,
            self.model_combo,
            self.chat,
            self.input_box,
        ):
            self.install_context_menu(widget)

    def schedule_layout_update(self, event):
        if event.widget is not self:
            return

        if self.resize_job is not None:
            self.after_cancel(self.resize_job)

        self.resize_job = self.after(
            60,
            self.update_layout,
        )

    def update_layout(self):
        self.resize_job = None

        # Na wąskim ekranie nie zabieramy miejsca panelowi czatu.
        should_show = self.winfo_width() >= 700

        if should_show != self.sidebar_visible:
            if should_show:
                self.sidebar.grid()
            else:
                self.sidebar.grid_remove()

            self.sidebar_visible = should_show

        self.status_label.configure(wraplength=max(200, self.main.winfo_width() - 24))
        self.update_chat_margins()

    def save_boolean_setting(self, key, variable):
        self.db.set_setting(key, "1" if variable.get() else "0")

    def adjust_answer_zoom(self, change):
        self.answer_zoom = min(160, max(70, self.answer_zoom + change))
        self.db.set_setting("answer_zoom", self.answer_zoom)
        self.apply_answer_zoom()

    def apply_answer_zoom(self):
        if not hasattr(self, "chat"):
            return
        scale = self.answer_zoom / 100
        body_size = max(8, round(11 * scale))
        code_size = max(8, round(10 * scale))
        self.chat.configure(font=("Segoe UI", body_size))
        for role in ("user", "assistant", "error"):
            self.chat.tag_configure(role, font=("Segoe UI", body_size, "bold"))
        self.chat.tag_configure("bold", font=("Segoe UI", body_size, "bold"))
        self.chat.tag_configure("italic", font=("Segoe UI", body_size, "italic"))
        self.chat.tag_configure("inline_code", font=("Consolas", code_size))
        self.chat.tag_configure("heading1", font=("Segoe UI", max(12, round(16 * scale)), "bold"))
        self.chat.tag_configure("heading2", font=("Segoe UI", max(11, round(13 * scale)), "bold"))
        self.chat.tag_configure("heading3", font=("Segoe UI", body_size, "bold"))
        self.chat.tag_configure("table", font=("Consolas", max(8, round(9 * scale))))
        self.chat.tag_configure("code_block", font=("Consolas", code_size))
        self.chat.tag_configure("code_keyword", font=("Consolas", code_size, "bold"))
        self.chat.tag_configure("code_string", font=("Consolas", code_size))
        self.chat.tag_configure("code_comment", font=("Consolas", code_size, "italic"))
        self.chat.tag_configure("code_number", font=("Consolas", code_size))
        self.chat.tag_configure("code_name", font=("Consolas", code_size))
        self.chat.tag_configure("code_operator", font=("Consolas", code_size))
        if hasattr(self, "zoom_percent_label"):
            self.zoom_percent_label.configure(text=f"{self.answer_zoom}%")

    def update_chat_margins(self):
        if not hasattr(self, "chat"):
            return
        width = self.chat.winfo_width()
        if width <= 1:
            return
        inset = 16
        for tag in ("user_block", "assistant_block"):
            self.chat.tag_configure(
                tag,
                lmargin1=inset,
                lmargin2=inset,
                rmargin=inset,
                spacing1=3,
                spacing3=4,
            )
        self.chat.tag_configure(
            "code_block",
            lmargin1=inset + 8,
            lmargin2=inset + 8,
            rmargin=inset + 8,
            spacing1=3,
            spacing3=4,
        )

    def toggle_key_visibility(self):
        self.key_entry.configure(show="" if self.show_key_var.get() else "*")

    def show_settings(self):
        self.settings_window.deiconify()
        self.settings_window.lift()
        self.settings_window.focus_force()

    def load_api_key(self):
        environment_key = os.environ.get("OPENAI_API_KEY", "")
        if environment_key:
            return environment_key
        try:
            if KEY_FILE.exists():
                return unprotect_api_key(KEY_FILE.read_bytes())
        except (OSError, ValueError) as exc:
            error_text = str(exc)
            self.after(
                250,
                lambda: messagebox.showwarning(
                    "Klucz API",
                    f"Nie udało się odczytać zapisanego klucza:\n{error_text}",
                    parent=self,
                ),
            )
        return ""

    def save_api_key(self):
        key = self.api_key_var.get().strip()
        if not key:
            messagebox.showwarning("Brak klucza", "Wpisz klucz API przed zapisem.", parent=self)
            self.key_entry.focus_set()
            return
        try:
            KEY_FILE.write_bytes(protect_api_key(key))
        except OSError as exc:
            messagebox.showerror("Nie udało się zapisać klucza", str(exc), parent=self)
            return
        messagebox.showinfo(
            "Klucz API",
            "Klucz zapisano w zaszyfrowanej formie dla tego konta Windows.",
            parent=self,
        )

    def delete_api_key(self):
        if not messagebox.askyesno(
            "Usuń klucz API",
            "Usunąć klucz API zapisany na tym komputerze?",
            parent=self,
        ):
            return
        try:
            KEY_FILE.unlink(missing_ok=True)
        except OSError as exc:
            messagebox.showerror("Nie udało się usunąć klucza", str(exc), parent=self)
            return
        if os.environ.get("OPENAI_API_KEY"):
            messagebox.showinfo(
                "Klucz API",
                "Usunięto zapisany klucz. Zmienna OPENAI_API_KEY nadal jest aktywna.",
                parent=self,
            )
        else:
            self.api_key_var.set("")
            messagebox.showinfo("Klucz API", "Usunięto zapisany klucz.", parent=self)

    # ========================================================
    # MENU KONTEKSTOWE PPM
    # ========================================================

    def install_context_menu(self, widget):
        widget.bind(
            "<Button-3>",
            lambda event, target=widget: self.show_context_menu(event, target),
        )
        widget.bind(
            "<Control-a>",
            lambda event, target=widget: self.select_all_shortcut(target),
        )
        widget.bind(
            "<Control-A>",
            lambda event, target=widget: self.select_all_shortcut(target),
        )

    def show_context_menu(self, event, widget):
        widget.focus_set()

        editable = self.is_editable(widget)
        has_selection = bool(self.get_selection(widget))

        menu = tk.Menu(self, tearoff=False)

        menu.add_command(
            label="Kopiuj",
            command=lambda: self.edit_widget(widget, "copy"),
            state="normal" if has_selection else "disabled",
        )
        menu.add_command(
            label="Wytnij",
            command=lambda: self.edit_widget(widget, "cut"),
            state=("normal" if editable and has_selection else "disabled"),
        )
        menu.add_command(
            label="Wklej",
            command=lambda: self.edit_widget(widget, "paste"),
            state="normal" if editable else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label="Zaznacz wszystko",
            command=lambda: self.edit_widget(widget, "select_all"),
        )

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

        return "break"

    @staticmethod
    def is_editable(widget):
        try:
            return str(widget.cget("state")) == "normal"
        except tk.TclError:
            return False

    @staticmethod
    def get_selection(widget):
        try:
            if isinstance(widget, tk.Text):
                return widget.get("sel.first", "sel.last")

            if isinstance(widget, (tk.Entry, ttk.Entry)):
                if widget.selection_present():
                    start = widget.index("sel.first")
                    end = widget.index("sel.last")
                    return widget.get()[start:end]

        except tk.TclError:
            pass

        return ""

    @staticmethod
    def delete_selection(widget):
        try:
            if isinstance(widget, tk.Text):
                widget.delete("sel.first", "sel.last")
            elif widget.selection_present():
                widget.delete(
                    widget.index("sel.first"),
                    widget.index("sel.last"),
                )
        except tk.TclError:
            pass

    def edit_focused(self, action):
        widget = self.focus_get()

        if isinstance(widget, (tk.Text, tk.Entry, ttk.Entry)):
            self.edit_widget(widget, action)

    def edit_widget(self, widget, action):
        try:
            if action == "select_all":
                if isinstance(widget, tk.Text):
                    widget.tag_add("sel", "1.0", "end-1c")
                    widget.mark_set("insert", "1.0")
                else:
                    widget.selection_range(0, tk.END)
                    widget.icursor(tk.END)
                return

            if action in {"copy", "cut"}:
                selected = self.get_selection(widget)

                if selected:
                    self.clipboard_clear()
                    self.clipboard_append(selected)

                    if action == "cut" and self.is_editable(widget):
                        self.delete_selection(widget)
                return

            if action == "paste" and self.is_editable(widget):
                value = self.clipboard_get()

                if not isinstance(widget, tk.Text):
                    value = value.replace("\r", "").replace("\n", " ")

                self.delete_selection(widget)
                widget.insert("insert", value)

        except tk.TclError:
            # Np. pusty schowek albo schowek bez tekstu.
            pass

    def select_all_shortcut(self, widget):
        self.edit_widget(widget, "select_all")
        return "break"

    # ========================================================
    # WYŚWIETLANIE I STAN APLIKACJI
    # ========================================================

    def append_chat(self, role, text):
        labels = {
            "user": "You" if self.language == "en" else "Ty",
            "assistant": "Assistant" if self.language == "en" else "Asystent",
            "error": "Error" if self.language == "en" else "Błąd",
        }

        self.chat.configure(state="normal")
        block_tag = "user_block" if role == "user" else "assistant_block"
        existing_text = self.chat.get("1.0", "end-1c")
        if existing_text and not existing_text.endswith("\n"):
            self.chat.insert(tk.END, "\n")
        start = self.chat.index(tk.END)
        self.chat.insert(tk.END, f"{labels.get(role, role)}\n", (role, block_tag))
        self._insert_markdown(text, block_tag)
        self.chat.insert(tk.END, "\n")
        self.chat.tag_add(block_tag, start, tk.END)
        self.chat.configure(state="disabled")
        self.chat.see(tk.END)

    def _insert_markdown(self, text, block_tag):
        text = re.sub(r"\n{3,}", "\n\n", text)
        lines = text.splitlines(keepends=True)
        index = 0
        while index < len(lines):
            line = lines[index]
            fence = re.match(r"^\s*```([\w.+-]*)", line)
            if fence:
                language = fence.group(1)
                index += 1
                code_lines = []
                while index < len(lines) and not lines[index].lstrip().startswith("```"):
                    code_lines.append(lines[index])
                    index += 1
                self._insert_code_block("".join(code_lines), language, block_tag)
                index += 1
                continue

            compact_lines = self._split_compact_numbered_list(line)
            if compact_lines is None:
                compact_lines = self._split_compact_bullet_list(line)
            if compact_lines:
                lines[index : index + 1] = compact_lines
                line = lines[index]

            if (
                self._is_table_row(line)
                and index + 1 < len(lines)
                and self._is_table_separator(lines[index + 1])
            ):
                table_lines = [line]
                index += 2
                while index < len(lines) and self._is_table_row(lines[index]):
                    table_lines.append(lines[index])
                    index += 1
                self._insert_markdown_table(table_lines, block_tag)
                continue

            heading = re.match(r"^\s{0,3}(#{1,6})\s+(.*?)(\n?)$", line)
            if heading:
                level = min(len(heading.group(1)), 3)
                self._insert_inline_markdown(
                    heading.group(2) + heading.group(3), block_tag, f"heading{level}"
                )
            elif re.match(r"^\s*>\s?", line):
                quote = re.sub(r"^\s*>\s?", "", line)
                self._insert_inline_markdown(quote, block_tag, "quote")
            elif re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", line):
                indentation, marker, body = self._parse_markdown_list_item(line)
                prefix = "• " if not marker[0].isdigit() else marker
                self.chat.insert(tk.END, indentation + prefix, (block_tag, "list_item"))
                self._insert_inline_markdown(body, block_tag, "list_item")
            else:
                self._insert_inline_markdown(line, block_tag)
            index += 1

    @staticmethod
    def _split_compact_numbered_list(line):
        matches = list(re.finditer(r"(?<!\w)(\d+)[.)]\s+", line))
        if len(matches) < 2:
            return None
        numbers = [int(match.group(1)) for match in matches]
        if any(numbers[i] + 1 != numbers[i + 1] for i in range(len(numbers) - 1)):
            return None
        lines = []
        prefix = line[: matches[0].start()].strip()
        if prefix:
            lines.append(prefix + "\n")
        for position, match in enumerate(matches):
            end = matches[position + 1].start() if position + 1 < len(matches) else len(line)
            item = line[match.start() : end].strip()
            lines.append(
                item + ("\n" if position + 1 < len(matches) or line.endswith("\n") else "")
            )
        return lines

    @staticmethod
    def _parse_markdown_list_item(line):
        item = re.match(r"^(\s*)([-*+]\s+|\d+[.)]\s+)(.*?)(\r?\n)?$", line)
        if item is None:
            return "", "", line
        body = item.group(3) + (item.group(4) or "")
        return item.group(1), item.group(2), body

    @staticmethod
    def _split_compact_bullet_list(line):
        markers = list(re.finditer(r"•\s*", line))
        if len(markers) < 2:
            return None
        prefix = line[: markers[0].start()].strip()
        lines = [prefix + "\n"] if prefix else []
        nested = bool(prefix and prefix.endswith(":"))
        indentation = "  " if nested else ""
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(line)
            item = line[marker.end() : end].strip()
            if item:
                lines.append(
                    indentation
                    + "- "
                    + item
                    + ("\n" if index + 1 < len(markers) or line.endswith("\n") else "")
                )
        return lines

    @staticmethod
    def _is_table_row(line):
        return "|" in line.strip().strip("|")

    @staticmethod
    def _is_table_separator(line):
        cells = line.strip().strip("|").split("|")
        return bool(cells) and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells)

    def _insert_markdown_table(self, lines, block_tag):
        rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
        columns = max(map(len, rows))
        rows = [row + [""] * (columns - len(row)) for row in rows]

        # Fit the table to the visible chat width, then wrap long cell values.
        # Previously values were sliced to 32 characters, silently dropping
        # the rest of each cell from the rendered answer.
        font = tkfont.Font(root=self.chat, font=self.chat.tag_cget("table", "font"))
        widget_width = self.chat.winfo_width()
        if widget_width < 300:
            # On the first render Tk may not have completed geometry layout;
            # estimate from the display so the table does not collapse to a
            # handful of characters and leave most of the chat area unused.
            widget_width = max(300, self.chat.winfo_screenwidth() - 240)
        available_pixels = max(1, widget_width - 96)
        available_chars = available_pixels // max(1, font.measure("0"))
        separators_width = 2 * (columns - 1)
        per_column_limit = max(
            8,
            min(56, (available_chars - separators_width) // columns),
        )
        widths = [
            min(per_column_limit, max(1, max(len(row[col]) for row in rows)))
            for col in range(columns)
        ]

        for row_index, row in enumerate(rows):
            wrapped = [
                self._wrap_table_cell(value, widths[column]) for column, value in enumerate(row)
            ]
            row_height = max(map(len, wrapped))
            for line_index in range(row_height):
                formatted_cells = []
                for column in range(columns):
                    cell_line = (
                        wrapped[column][line_index] if line_index < len(wrapped[column]) else ""
                    )
                    formatted_cells.append(cell_line.ljust(widths[column]))
                formatted = "  ".join(formatted_cells)
                self.chat.insert(
                    tk.END,
                    formatted.rstrip() + "\n",
                    (block_tag, "table", "bold" if row_index == 0 else block_tag),
                )

    @staticmethod
    def _wrap_table_cell(value, width):
        value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
        paragraphs = value.splitlines() or [""]
        wrapped = []
        for paragraph in paragraphs:
            wrapped.extend(
                textwrap.wrap(
                    paragraph,
                    width=width,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
                or [""]
            )
        return wrapped

    def _insert_code_block(self, code, language, block_tag):
        existing_text = self.chat.get("1.0", "end-1c")
        if existing_text and not existing_text.endswith("\n"):
            # Keep the separator on the preceding paragraph, outside the dark code tag.
            self.chat.insert(tk.END, "\n")

        display_language = language or ("Code" if self.language == "en" else "Kod")
        try:
            lexer = get_lexer_by_name(language, stripnl=False) if language else guess_lexer(code)
            if not language:
                display_language = getattr(lexer, "name", display_language)
        except Exception:
            lexer = None

        header = tk.Frame(self.chat, background="#202832", padx=10, pady=5)
        header.columnconfigure(0, weight=1)
        tk.Label(
            header,
            text=display_language,
            background="#202832",
            foreground="#b8c6d6",
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        tk.Button(
            header,
            text="Copy" if self.language == "en" else "Kopiuj",
            command=lambda value=code: self.copy_code_block(value),
            background="#344252",
            foreground="#ffffff",
            activebackground="#46596e",
            activeforeground="#ffffff",
            relief="flat",
            borderwidth=0,
            padx=9,
            pady=2,
            takefocus=False,
            cursor="hand2",
        ).grid(row=0, column=1, sticky="e")
        header.grid_propagate(False)
        header.configure(width=max(300, self.chat.winfo_width() - 74), height=34)
        self._code_header_frames.append(header)
        self.chat.window_create(tk.END, window=header, align="top", stretch=True)
        self.chat.insert(tk.END, "\n")
        self.after_idle(self.update_code_header_widths)

        self.chat.tag_configure("code_block", spacing1=1, spacing3=1)
        try:
            if lexer is None:
                raise ValueError("No syntax lexer available")
            for token_type, value in lex(code, lexer):
                style = "code_block"
                if token_type in Token.Keyword or token_type in Token.Keyword.Type:
                    style = "code_keyword"
                elif token_type in Token.Literal.String:
                    style = "code_string"
                elif token_type in Token.Comment:
                    style = "code_comment"
                elif token_type in Token.Literal.Number:
                    style = "code_number"
                elif token_type in Token.Name.Function or token_type in Token.Name.Class:
                    style = "code_name"
                elif token_type in Token.Operator:
                    style = "code_operator"
                self.chat.insert(tk.END, value, (block_tag, "code_block", style))
        except Exception:
            self.chat.insert(tk.END, code, (block_tag, "code_block"))
        if code and not code.endswith("\n"):
            self.chat.insert(tk.END, "\n", (block_tag, "code_block"))

    def update_code_header_widths(self, event=None):
        width = max(300, (event.width if event else self.chat.winfo_width()) - 74)
        for frame in getattr(self, "_code_header_frames", []):
            try:
                if frame.winfo_exists():
                    frame.configure(width=width)
            except tk.TclError:
                continue

    def copy_code_block(self, code):
        self.clipboard_clear()
        self.clipboard_append(code)
        self.status_var.set("Code copied." if self.language == "en" else "Skopiowano blok kodu.")

    def _insert_inline_markdown(self, text, block_tag, extra_tag=None):
        pattern = re.compile(
            r"(`[^`]+`|\*\*.+?\*\*|\*[^*]+\*|\[[^\]]+\]\(https?://[^\s)]+\)|https?://[^\s<>]+)"
        )
        cursor = 0
        for match in pattern.finditer(text):
            self.chat.insert(
                tk.END,
                text[cursor : match.start()],
                tuple(tag for tag in (block_tag, extra_tag) if tag),
            )
            token = match.group(0)
            if token.startswith("[") or token.startswith("http"):
                link = re.match(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", token)
                if link:
                    label, url = link.groups()
                else:
                    url = token.rstrip(".,;:!?)]}")
                    parsed_url = urlsplit(url)
                    label = parsed_url.netloc or url
                    if label.startswith("www."):
                        label = label[4:]
                    token = label
                tag = f"url_{abs(hash(url))}"
                self.chat.tag_configure(tag, foreground="#0866c6", underline=True)
                self.chat.tag_bind(
                    tag, "<Button-1>", lambda _event, target=url: webbrowser.open(target)
                )
                self.chat.tag_bind(
                    tag,
                    "<Enter>",
                    lambda event, target=url: self.show_tooltip(
                        target, event.x_root + 12, event.y_root + 16
                    ),
                )
                self.chat.tag_bind(tag, "<Leave>", self.hide_tooltip)
                tags = tuple(tag_item for tag_item in (block_tag, extra_tag, tag) if tag_item)
                self.chat.insert(tk.END, label, tags)
            elif token.startswith("**"):
                tags = tuple(tag_item for tag_item in (block_tag, extra_tag, "bold") if tag_item)
                self.chat.insert(tk.END, token[2:-2], tags)
            elif token.startswith("*"):
                tags = tuple(tag_item for tag_item in (block_tag, extra_tag, "italic") if tag_item)
                self.chat.insert(tk.END, token[1:-1], tags)
            else:
                tags = tuple(
                    tag_item for tag_item in (block_tag, extra_tag, "inline_code") if tag_item
                )
                self.chat.insert(tk.END, token[1:-1], tags)
            cursor = match.end()
        self.chat.insert(tk.END, text[cursor:], tuple(tag for tag in (block_tag, extra_tag) if tag))

    def set_busy(self, busy, status=None):
        self.busy = busy
        state = "disabled" if busy else "normal"

        # Pole prompta pozostaje aktywne — można przygotować
        # kolejną wiadomość podczas oczekiwania na odpowiedź.
        for widget in (
            self.new_button,
            self.rename_button,
            self.delete_button,
            self.send_button,
            self.retry_button,
            self.attach_button,
            self.clear_files_button,
            self.paste_image_button,
            self.reuse_attachments_check,
            self.language_button,
            self.key_entry,
            self.show_key_check,
            self.model_combo,
            self.header_model_combo,
            self.refresh_models_button,
        ):
            widget.configure(state=state)

        if busy:
            self.conversations.state(["disabled"])
        else:
            self.conversations.state(["!disabled"])

        self.send_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        if busy:
            self.stop_button.grid()
        else:
            self.stop_button.grid_remove()

        self.menu_bar.entryconfigure(0, state=state)

        self.status_var.set(
            status
            if status is not None
            else (
                ("Waiting for API…" if self.language == "en" else "Oczekiwanie na API…")
                if busy
                else ("Ready" if self.language == "en" else "Gotowe")
            )
        )

    def stop_generation(self):
        if self.busy:
            if self.active_cancel_event is not None:
                self.active_cancel_event.set()
            self.stop_button.configure(state="disabled")
            self.status_var.set("Zatrzymywanie generowania…")
            stream = self.active_stream
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    logging.debug("Could not close API stream after stop", exc_info=True)

    def copy_last_answer(self):
        answer = self.db.get_last_assistant_display(self.current_id)
        if answer is None:
            messagebox.showinfo(
                "Kopiowanie",
                "W tej rozmowie nie ma jeszcze odpowiedzi.",
                parent=self,
            )
            return

        self.clipboard_clear()
        self.clipboard_append(answer)

        if not self.busy:
            self.status_var.set("Skopiowano ostatnią odpowiedź.")

    def show_raw_api_text(self):
        if not self.last_raw_api_text:
            messagebox.showinfo(
                "Debug" if self.language == "en" else "Debugowanie",
                "No API response has been received yet."
                if self.language == "en"
                else "Aplikacja nie odebrała jeszcze odpowiedzi z API.",
                parent=self.settings_window,
            )
            return

        window = tk.Toplevel(self)
        window.title("Raw API text" if self.language == "en" else "Surowy tekst API")
        window.geometry("850x560")
        window.minsize(480, 300)
        window.transient(self)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        raw_text = ScrolledText(
            window,
            wrap="word",
            font=("Consolas", 10),
            background="#f5f7fa",
            foreground="#202123",
            selectbackground="#315f91",
            selectforeground="#ffffff",
            padx=10,
            pady=10,
        )
        raw_text.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        raw_text.insert("1.0", self.last_raw_api_text)
        raw_text.configure(state="disabled")
        buttons = ttk.Frame(window)
        buttons.grid(row=1, column=0, sticky="e", padx=10, pady=(0, 10))
        ttk.Button(
            buttons,
            text="Copy" if self.language == "en" else "Kopiuj",
            command=lambda: self.copy_code_block(self.last_raw_api_text),
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            buttons,
            text="Close" if self.language == "en" else "Zamknij",
            command=window.destroy,
        ).pack(side="left")

    def show_current_title_tooltip(self, event):
        title = getattr(self, "current_conversation_title", "")
        if title:
            self.show_tooltip(title, event.x_root + 12, event.y_root + 16)

    def show_retry_tooltip(self, event):
        label = "Retry the last request" if self.language == "en" else "Ponów ostatnie zapytanie"
        self.show_tooltip(label, event.x_root + 12, event.y_root + 16)

    # ========================================================
    # ROZMOWY I BAZA DANYCH
    # ========================================================

    def refresh_conversations(self):
        rows = self.db.list_conversations()

        self.conversation_ids = [row[0] for row in rows]
        self.conversation_info = {
            row[0]: {"title": row[1], "updated_at": row[2], "model": row[3]} for row in rows
        }
        query = (
            self.search_var.get().strip().casefold()
            if hasattr(self, "search_var") and not getattr(self, "search_placeholder", False)
            else ""
        )
        visible_rows = [row for row in rows if query in row[1].casefold()]
        self.visible_conversation_ids = [row[0] for row in visible_rows]

        existing_items = self.conversations.get_children()
        if existing_items:
            self.conversations.delete(*existing_items)
        self.conversations_menu.delete(0, tk.END)

        self.conversations_menu.add_command(
            label="+ New chat" if self.language == "en" else "+ Nowa rozmowa",
            command=self.new_conversation,
        )
        self.conversations_menu.add_command(
            label="Rename current chat" if self.language == "en" else "Zmień nazwę bieżącej",
            command=self.rename_conversation,
        )
        self.conversations_menu.add_command(
            label="Delete current chat" if self.language == "en" else "Usuń bieżącą rozmowę",
            command=self.delete_conversation,
        )
        self.conversations_menu.add_separator()

        title_width = max(90, self.conversations.column("title", "width"))
        title_chars = max(12, (title_width - 34) // 7)
        for conversation_id, title, updated_at, _model in visible_rows:
            shown_title = title.strip()
            if len(shown_title) > title_chars:
                shown_title = shown_title[: title_chars - 1].rstrip() + "…"
            marker = "▏ " if conversation_id == self.current_id else "  "
            date_text = ""
            if updated_at:
                try:
                    parsed_date = time.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                    date_text = time.strftime("%d.%m", parsed_date)
                except ValueError:
                    date_text = updated_at[:10]
            self.conversations.insert(
                "",
                tk.END,
                iid=str(conversation_id),
                values=(marker + shown_title, date_text),
            )

        for conversation_id, title, _updated_at, _model in rows:
            self.conversations_menu.add_command(
                label=title[:57].rstrip() + ("…" if len(title) > 57 else ""),
                command=lambda cid=conversation_id: self.open_conversation(cid),
            )

        if self.current_id in self.conversation_info:
            full_title = self.conversation_info[self.current_id]["title"]
            self.current_conversation_title = full_title
            max_title_chars = 42
            self.chat_title_var.set(
                full_title[: max_title_chars - 1].rstrip() + "…"
                if len(full_title) > max_title_chars
                else full_title
            )
        else:
            self.current_conversation_title = ""

        if self.current_id in self.visible_conversation_ids:
            item_id = str(self.current_id)
            self.conversations.selection_set(item_id)
            self.conversations.see(item_id)

    def new_conversation(self):
        if self.busy:
            return

        model = self.model_var.get().strip() or DEFAULT_MODEL

        conversation_id = self.db.create_conversation("Nowa rozmowa", model)
        self.open_conversation(conversation_id)

    def on_conversation_selected(self, _event=None):
        if self.busy:
            return

        selection = self.conversations.selection()

        if selection:
            conversation_id = int(selection[0])

            if conversation_id != self.current_id:
                self.open_conversation(conversation_id)

    def schedule_conversation_tooltip(self, event):
        if self._conversation_tooltip_job is not None:
            self.after_cancel(self._conversation_tooltip_job)
            self._conversation_tooltip_job = None
        item_id = self.conversations.identify_row(event.y)
        if not item_id:
            self.hide_tooltip()
            return
        info = self.conversation_info.get(int(item_id), {})
        title = info.get("title", "")
        model = info.get("model", "")
        updated_at = info.get("updated_at", "")
        details = "\n".join(part for part in (title, updated_at, model) if part)
        x_root, y_root = event.x_root, event.y_root
        self._conversation_tooltip_job = self.after(
            500,
            lambda: self.show_tooltip(details, x_root + 14, y_root + 18),
        )

    def show_tooltip(self, text, x, y):
        if not text:
            return
        self.hide_tooltip()
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tip.configure(background="#263445")
        label = tk.Label(
            tip,
            text=text,
            justify="left",
            background="#263445",
            foreground="#f5f7fa",
            font=("Segoe UI", 9),
            padx=9,
            pady=6,
            wraplength=420,
        )
        label.pack()
        tip.geometry(f"+{x}+{y}")
        self._tooltip_window = tip

    def hide_tooltip(self, _event=None):
        if self._conversation_tooltip_job is not None:
            try:
                self.after_cancel(self._conversation_tooltip_job)
            except tk.TclError:
                pass
            self._conversation_tooltip_job = None
        if self._tooltip_window is not None:
            try:
                self._tooltip_window.destroy()
            except tk.TclError:
                pass
            self._tooltip_window = None

    def on_search_changed(self, *_args):
        if hasattr(self, "conversations"):
            self.refresh_conversations()

    def clear_conversation_search(self, _event=None):
        self.search_var.set("")
        self.search_entry.focus_set()
        return "break"

    def focus_conversation_search(self, _event=None):
        if not self.sidebar_visible:
            screen_width = self.winfo_screenwidth()
            width = min(screen_width - 40, 900)
            height = self.winfo_height()
            self.geometry(f"{width}x{height}")
            self.after(100, self.search_entry.focus_set)
        else:
            self.search_entry.focus_set()
        return "break"

    def open_conversation(self, conversation_id):
        if self.busy:
            return

        model = self.db.get_conversation_model(conversation_id)
        if model is None:
            return

        # Zapis ręcznie wpisanego modelu poprzedniej rozmowy.
        if self.current_id is not None:
            self.save_selected_model()

        self.current_id = conversation_id
        self.model_var.set(model)

        self.clear_files()
        self.input_box.delete("1.0", tk.END)
        self.input_placeholder_active = False

        self.chat.configure(state="normal")
        self.chat.delete("1.0", tk.END)
        self._code_header_frames.clear()
        self.chat.configure(state="disabled")

        for role, text in self.db.get_display_messages(conversation_id):
            self.append_chat(role, text)

        self.chat.yview_moveto(0.0)

        self.refresh_conversations()
        self.update_usage_status()
        self.input_box.focus_set()

    def model_prices(self, model):
        return PricingCatalog.lookup(self.prices, model)

    def update_usage_status(self, latest=None):
        totals = self.db.usage_totals(self.current_id)
        in_tokens, out_tokens, total_cost, total_time, unpriced_calls = totals
        if latest is None and self.current_id is not None:
            latest = self.db.latest_usage(self.current_id)

        model = latest.get("model") if latest else (self.model_var.get().strip() or "—")
        last_in = latest.get("input_tokens", 0) if latest else 0
        last_out = latest.get("output_tokens", 0) if latest else 0
        last_cost = latest.get("cost") if latest else None
        last_elapsed = latest.get("elapsed", 0) if latest else 0
        request_id = latest.get("request_id", "") if latest else ""

        def number(value):
            return f"{int(value or 0):,}".replace(",", " ")

        missing_price = self.model_prices(model) is None
        if self.language == "en":
            last_cost_text = f"${last_cost:.6f}" if last_cost is not None else "n/a"
            total_cost_text = f"${total_cost:.6f}"
            last_tokens = f"Latest: {number(last_in)} in / {number(last_out)} out"
            chat_tokens = f"Chat: {number(in_tokens)} in / {number(out_tokens)} out"
            last_time = f"Latest: {last_elapsed:.1f} s"
            chat_time = f"Chat total: {total_time:.1f} s"
            price_label = f"{unpriced_calls} unpriced call(s)" if unpriced_calls else "none"
            model_heading = "Model"
            last_cost_heading = "Latest cost"
            chat_cost_heading = "Chat cost"
            request_heading = "Request ID"
            self.pricing_link.configure(text="Add price")
        else:
            last_cost_text = f"${last_cost:.6f}" if last_cost is not None else "n/d"
            total_cost_text = f"${total_cost:.6f}"
            last_tokens = f"Ostatnia: {number(last_in)} wej. / {number(last_out)} wyj."
            chat_tokens = f"Rozmowa: {number(in_tokens)} wej. / {number(out_tokens)} wyj."
            last_time = f"Ostatnia: {last_elapsed:.1f} s"
            chat_time = f"Łącznie: {total_time:.1f} s"
            price_label = f"{unpriced_calls} bez ceny" if unpriced_calls else "brak"
            model_heading = "Model"
            last_cost_heading = "Koszt ostatni"
            chat_cost_heading = "Koszt rozmowy"
            request_heading = "ID żądania"
            self.pricing_link.configure(text="Dodaj cenę")

        self.status_summary_var.set(
            f"{number(last_in)}/{number(last_out)} tok · {last_cost_text} · {last_elapsed:.1f} s"
        )
        self.status_detail_text = "\n".join(
            (
                f"{model_heading}: {model}",
                last_tokens,
                chat_tokens,
                f"{last_cost_heading}: {last_cost_text}",
                f"{chat_cost_heading}: {total_cost_text} ({price_label})",
                last_time,
                chat_time,
                f"{request_heading}: {request_id or '—'}",
            )
        )

        if missing_price:
            self.pricing_link.grid()
        else:
            self.pricing_link.grid_remove()

    def show_status_tooltip(self, event):
        details = getattr(self, "status_detail_text", "")
        if details:
            self.show_tooltip(details, event.x_root + 12, event.y_root + 16)

    def open_pricing_file(self, _event=None):
        pricing_path = APP_DIR / "pricing.json"
        try:
            if os.name == "nt":
                os.startfile(str(pricing_path))
            else:
                webbrowser.open(pricing_path.as_uri())
        except OSError as exc:
            messagebox.showerror(
                "Pricing" if self.language == "en" else "Cennik",
                f"Could not open {pricing_path}: {exc}"
                if self.language == "en"
                else f"Nie można otworzyć pliku {pricing_path}: {exc}",
                parent=self,
            )

    def save_usage(self, usage):
        self.db.save_usage(self.current_id, usage)

    def rename_conversation(self):
        if self.busy or self.current_id is None:
            return

        current_title = self.db.get_conversation_title(self.current_id)
        if current_title is None:
            return

        title = simpledialog.askstring(
            "Nazwa rozmowy",
            "Wpisz nową nazwę:",
            initialvalue=current_title,
            parent=self,
        )

        if title and title.strip():
            self.db.rename_conversation(self.current_id, title.strip()[:100])
            self.refresh_conversations()

    def delete_conversation(self):
        if self.busy or self.current_id is None:
            return

        if not messagebox.askyesno(
            "Usuwanie rozmowy",
            "Usunąć rozmowę i zapisane w niej załączniki?",
            parent=self,
        ):
            return

        self.db.delete_conversation(self.current_id)

        self.current_id = None
        self.refresh_conversations()

        if self.conversation_ids:
            self.open_conversation(self.conversation_ids[0])
        else:
            self.new_conversation()

    def save_selected_model(self, _event=None):
        if self.busy or self.current_id is None:
            return

        model = self.model_var.get().strip()

        if model:
            self.db.set_conversation_model(self.current_id, model)

    def save_message(self, role, content, display_text):
        self.db.save_message(self.current_id, role, content, display_text)

    def persist_attachments(self, content):
        return self.attachment_store.persist_content(content)

    def hydrate_attachment(self, part):
        return self.attachment_store.hydrate(part)

    def load_api_history(self):
        return self.db.get_api_history(
            self.current_id,
            reuse_attachments=self.reuse_attachments_var.get(),
            language=self.language,
        )

    # ========================================================
    # MODELE
    # ========================================================

    def fetch_models(self):
        if self.busy:
            return

        api_key = self.api_key_var.get().strip()

        if not api_key:
            messagebox.showwarning(
                "Brak klucza",
                "Najpierw wpisz klucz API.",
                parent=self,
            )
            self.key_entry.focus_set()
            return

        self.set_busy(True, "Pobieranie listy modeli…")

        threading.Thread(
            target=self.fetch_models_worker,
            args=(api_key,),
            daemon=True,
        ).start()

    def fetch_models_worker(self, api_key):
        try:
            self.results.put(("models", self.api_service.list_models(api_key)))
        except Exception as exc:
            self.results.put(("models_error", self.format_error(exc)))

    @staticmethod
    def unsupported_model_reason(model):
        return unsupported_model_reason(model)

    @staticmethod
    def uses_chat_completions(model):
        return uses_chat_completions(model)

    @staticmethod
    def to_chat_messages(history):
        return to_chat_messages(history)

    # ========================================================
    # ZAŁĄCZNIKI
    # ========================================================

    def add_files(self):
        if self.busy:
            return

        values = filedialog.askopenfilenames(
            title="Wybierz pliki",
            parent=self,
            filetypes=[
                ("Wszystkie pliki", "*.*"),
                ("PDF", "*.pdf"),
                ("Obrazy", "*.png *.jpg *.jpeg *.webp"),
                ("Tekst", "*.txt *.md *.csv *.py *.json"),
            ],
        )

        self.add_paths([Path(value) for value in values])

    def paste_image_shortcut(self, _event=None):
        self.paste_clipboard_image()
        return "break"

    def paste_clipboard_image(self):
        try:
            clipboard = ImageGrab.grabclipboard()
            if isinstance(clipboard, list):
                self.add_paths([Path(path) for path in clipboard])
                return
            if not isinstance(clipboard, Image.Image):
                messagebox.showinfo("Schowek", "W schowku nie znaleziono obrazu.", parent=self)
                return
            clipboard_dir = APP_DIR / "clipboard_images"
            clipboard_dir.mkdir(parents=True, exist_ok=True)
            path = clipboard_dir / f"schowek_{uuid.uuid4().hex[:8]}.png"
            clipboard.convert("RGB").save(path, format="PNG")
            if path.stat().st_size > MAX_FILE_BYTES:
                path.unlink(missing_ok=True)
                raise ValueError("Obraz ze schowka przekracza limit 10 MB.")
            self.pending_files.append(path)
            self.update_files_label()
        except (OSError, ValueError) as exc:
            messagebox.showerror("Nie można wkleić obrazu", str(exc), parent=self)

    def add_paths(self, paths):
        for path in paths:
            path = Path(path)
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                messagebox.showwarning(
                    "Nieobsługiwany format", f"Nieobsługiwany plik: {path.name}", parent=self
                )
                continue
            if not path.is_file():
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                messagebox.showwarning(
                    "Plik za duży", f"{path.name} przekracza limit 10 MB.", parent=self
                )
                continue
            if path not in self.pending_files:
                self.pending_files.append(path)
        self.update_files_label()

    def clear_files(self):
        self.pending_files.clear()
        self.update_files_label()

    def update_files_label(self):
        for child in self.attachment_tiles.winfo_children():
            child.destroy()

        if not self.pending_files:
            self.files_var.set("No attachments" if self.language == "en" else "Brak załączników")
            return

        self.files_var.set(
            f"{len(self.pending_files)} attachment(s) • ready to send"
            if self.language == "en"
            else f"{len(self.pending_files)} załącznik(i) • gotowe do wysłania"
        )
        for index, path in enumerate(self.pending_files):
            tile = ttk.Frame(self.attachment_tiles, padding=8, style="Composer.TFrame")
            tile.grid(row=index // 4, column=index % 4, sticky="ew", padx=(0, 6), pady=(0, 5))
            tile.columnconfigure(1, weight=1)
            image = None
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                try:
                    preview = Image.open(path)
                    preview.thumbnail((54, 54))
                    image = ImageTk.PhotoImage(preview.copy())
                    thumbnail = ttk.Label(tile, image=image)
                    thumbnail.grid(row=0, column=0, rowspan=3, padx=(0, 7))
                    thumbnail.image = image
                except OSError:
                    pass
            size = self.format_file_size(path.stat().st_size)
            ttk.Label(tile, text=path.name, width=24, anchor="w").grid(row=0, column=1, sticky="ew")
            tb.Label(
                tile, text=f"{path.suffix[1:].upper() or 'PLIK'} • {size}", bootstyle="secondary"
            ).grid(row=1, column=1, sticky="w")
            tb.Label(
                tile,
                text="Ready to send" if self.language == "en" else "Gotowy do wysłania",
                bootstyle="success",
            ).grid(row=2, column=1, sticky="w")
            ttk.Button(
                tile,
                text="×",
                width=3,
                command=lambda item=path: self.remove_attachment(item),
            ).grid(row=0, column=2, rowspan=3, padx=(6, 0))

    @staticmethod
    def format_file_size(size):
        if size < 1024 * 1024:
            return f"{size / 1024:.0f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    def remove_attachment(self, path):
        if path in self.pending_files:
            self.pending_files.remove(path)
            self.update_files_label()

    def build_user_content(self, text):
        return self.attachment_store.build_user_content(text, self.pending_files)

    def open_image_generator(self):
        api_key = self.api_key_var.get().strip()
        if not api_key:
            messagebox.showwarning(
                "Brak klucza API" if self.language != "en" else "Missing API key",
                "Zapisz lub wpisz klucz API w ustawieniach."
                if self.language != "en"
                else "Enter or save an API key in Settings.",
                parent=self,
            )
            self.show_settings()
            return

        english = self.language == "en"
        dialog = tk.Toplevel(self)
        dialog.title("Generate image" if english else "Generuj obraz")
        dialog.configure(background=UI_COLORS["app"])
        dialog.resizable(True, True)
        dialog.minsize(520, 560)
        dialog.geometry("720x740")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(2, weight=1)

        prompt_frame = ttk.LabelFrame(
            dialog,
            text="Image description" if english else "Opis obrazu",
            padding=8,
        )
        prompt_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        prompt_frame.columnconfigure(0, weight=1)
        reference_images = []
        reference_summary_var = tk.StringVar(
            value="Brak obrazów referencyjnych" if not english else "No reference images selected"
        )

        reference_toolbar = ttk.Frame(prompt_frame)
        reference_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        reference_toolbar.columnconfigure(0, weight=1)
        ttk.Label(reference_toolbar, textvariable=reference_summary_var).grid(
            row=0, column=0, sticky="w"
        )

        reference_tiles = ttk.Frame(prompt_frame, style="Composer.TFrame")
        reference_tiles.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        reference_tiles.columnconfigure((0, 1, 2, 3), weight=1)

        def refresh_reference_tiles():
            for child in reference_tiles.winfo_children():
                child.destroy()
            if reference_images:
                count = len(reference_images)
                if english:
                    summary = f"{count} reference image(s)"
                elif count == 1:
                    summary = "1 obraz referencyjny"
                elif 2 <= count <= 4:
                    summary = f"{count} obrazy referencyjne"
                else:
                    summary = f"{count} obrazów referencyjnych"
                reference_summary_var.set(summary)
            else:
                reference_summary_var.set(
                    "Brak obrazów referencyjnych" if not english else "No reference images selected"
                )

            for index, path in enumerate(reference_images):
                tile = ttk.Frame(reference_tiles, padding=5, style="Composer.TFrame")
                tile.grid(
                    row=index // 4,
                    column=index % 4,
                    sticky="ew",
                    padx=(0, 5),
                    pady=(0, 4),
                )
                tile.columnconfigure(1, weight=1)
                try:
                    with Image.open(path) as image:
                        image.thumbnail((38, 38), Image.Resampling.LANCZOS)
                        thumbnail = ImageTk.PhotoImage(image.copy())
                    thumbnail_label = ttk.Label(tile, image=thumbnail)
                    thumbnail_label.grid(row=0, column=0, rowspan=2, padx=(0, 6))
                    thumbnail_label.image = thumbnail
                except OSError:
                    pass
                display_name = path.name
                if len(display_name) > 20:
                    display_name = display_name[:17] + "…"
                ttk.Label(tile, text=display_name).grid(row=0, column=1, sticky="w")
                ttk.Label(
                    tile,
                    text=self.format_file_size(path.stat().st_size),
                    style="Composer.TLabel",
                ).grid(row=1, column=1, sticky="w")
                tb.Button(
                    tile,
                    text="×",
                    width=2,
                    command=lambda selected=path: remove_reference_image(selected),
                    bootstyle="secondary-outline",
                ).grid(row=0, column=2, rowspan=2, padx=(5, 0))

        def remove_reference_image(path):
            if path in reference_images:
                reference_images.remove(path)
                refresh_reference_tiles()

        def add_reference_images():
            selected_paths = filedialog.askopenfilenames(
                parent=dialog,
                title="Wybierz obrazy referencyjne" if not english else "Choose reference images",
                filetypes=[
                    (
                        "Obrazy PNG, JPEG, WebP" if not english else "PNG, JPEG, WebP images",
                        "*.png *.jpg *.jpeg *.webp",
                    ),
                    ("Wszystkie pliki" if not english else "All files", "*.*"),
                ],
            )
            if not selected_paths:
                return

            rejected = []
            total_bytes = 0
            for existing_path in list(reference_images):
                try:
                    total_bytes += existing_path.stat().st_size
                except OSError:
                    reference_images.remove(existing_path)
                    rejected.append(existing_path.name)
            for selected_path in selected_paths:
                path = Path(selected_path)
                if path in reference_images:
                    continue
                if path.suffix.lower() not in IMAGE_EXTENSIONS or not path.is_file():
                    rejected.append(path.name)
                    continue
                if len(reference_images) >= 16:
                    rejected.append(path.name)
                    continue
                try:
                    file_size = path.stat().st_size
                    if file_size > MAX_FILE_BYTES or total_bytes + file_size > MAX_BATCH_BYTES:
                        rejected.append(path.name)
                        continue
                    with Image.open(path) as image:
                        if image.format not in {"PNG", "JPEG", "WEBP"}:
                            rejected.append(path.name)
                            continue
                        image.verify()
                except (OSError, ValueError):
                    rejected.append(path.name)
                    continue
                reference_images.append(path)
                total_bytes += file_size

            refresh_reference_tiles()
            if rejected:
                limits = (
                    "Limit: 16 obrazów, 10 MB na plik i 20 MB łącznie."
                    if not english
                    else "Limits: 16 images, 10 MB per file, 20 MB total."
                )
                messagebox.showwarning(
                    "Niektórych plików nie dodano" if not english else "Some files were skipped",
                    f"{limits}\n\n" + "\n".join(rejected[:8]),
                    parent=dialog,
                )

        reference_button = tb.Button(
            reference_toolbar,
            text="Załącz plik" if not english else "Attach image",
            command=add_reference_images,
            bootstyle="secondary-outline",
        )
        reference_button.grid(row=0, column=1, sticky="e")

        prompt_box = ScrolledText(
            prompt_frame,
            height=5,
            wrap="word",
            font=("Segoe UI", 10),
            padx=7,
            pady=6,
            undo=True,
            exportselection=False,
        )
        prompt_box.grid(row=1, column=0, sticky="ew")

        options = ttk.Frame(dialog, padding=(10, 4, 10, 6))
        options.grid(row=1, column=0, sticky="ew")
        options.columnconfigure(1, weight=1)
        options.columnconfigure(3, weight=1)

        model_var = tk.StringVar(value="gpt-image-2.6-flare")
        size_var = tk.StringVar(value="auto")
        quality_var = tk.StringVar(value="auto")
        format_var = tk.StringVar(value="png")
        ttk.Label(options, text="Model:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        model_combo = ttk.Combobox(
            options,
            textvariable=model_var,
            values=("gpt-image-2.6-flare", "gpt-image-2.6-sunburst"),
            state="readonly",
            width=25,
        )
        model_combo.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        ttk.Label(options, text="Format:").grid(row=0, column=2, sticky="w", padx=(0, 4))
        format_combo = ttk.Combobox(
            options,
            textvariable=format_var,
            values=("png", "jpeg", "webp"),
            state="readonly",
            width=8,
        )
        format_combo.grid(row=0, column=3, sticky="ew")
        ttk.Label(options, text="Rozmiar:" if not english else "Size:").grid(
            row=1, column=0, sticky="w", padx=(0, 4), pady=(6, 0)
        )
        size_combo = ttk.Combobox(
            options,
            textvariable=size_var,
            values=("auto", "1024x1024", "1536x1024", "1024x1536"),
            state="readonly",
            width=15,
        )
        size_combo.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=(6, 0))
        ttk.Label(options, text="Jakość:" if not english else "Quality:").grid(
            row=1, column=2, sticky="w", padx=(0, 4), pady=(6, 0)
        )
        quality_combo = ttk.Combobox(
            options,
            textvariable=quality_var,
            values=("auto", "low", "medium", "high", "xhigh", "max"),
            state="readonly",
            width=12,
        )
        quality_combo.grid(row=1, column=3, sticky="ew", pady=(6, 0))

        preview_frame = ttk.LabelFrame(
            dialog,
            text="Podgląd" if not english else "Preview",
            padding=8,
        )
        preview_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=6)
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        preview_canvas = tk.Canvas(
            preview_frame,
            background="#f1f3f6",
            highlightthickness=0,
            xscrollincrement=24,
            yscrollincrement=24,
        )
        horizontal_scrollbar = ttk.Scrollbar(
            preview_frame, orient="horizontal", command=preview_canvas.xview
        )
        vertical_scrollbar = ttk.Scrollbar(
            preview_frame, orient="vertical", command=preview_canvas.yview
        )
        preview_canvas.configure(
            xscrollcommand=horizontal_scrollbar.set,
            yscrollcommand=vertical_scrollbar.set,
        )
        preview_canvas.grid(row=0, column=0, sticky="nsew")
        vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        preview_frame.rowconfigure(1, weight=0)
        preview_frame.columnconfigure(1, weight=0)

        status_var = tk.StringVar(
            value=(
                "Generowanie może wiązać się z opłatą API; obrazy referencyjne będą wysłane do OpenAI."
                if not english
                else "Generation may incur API charges; reference images are sent to OpenAI."
            )
        )
        ttk.Label(dialog, textvariable=status_var, anchor="w").grid(
            row=3, column=0, sticky="ew", padx=12, pady=(0, 6)
        )

        actions = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        actions.grid(row=4, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        save_button = tb.Button(
            actions,
            text="Zapisz obraz…" if not english else "Save image…",
            state="disabled",
            bootstyle="secondary-outline",
        )
        save_button.grid(row=0, column=1, padx=(0, 6))
        close_button = tb.Button(
            actions,
            text="Zamknij" if not english else "Close",
            command=dialog.destroy,
            bootstyle="secondary-outline",
        )
        close_button.grid(row=0, column=2, padx=(0, 6))
        generate_button = tb.Button(
            actions,
            text="Generuj" if not english else "Generate",
            bootstyle="primary",
        )
        generate_button.grid(row=0, column=3)

        results = queue.Queue(maxsize=1)
        generated = {"bytes": None, "format": None, "photo": None, "canvas_image_id": None}

        def update_placeholder(_event=None):
            if generated["canvas_image_id"] is not None:
                return
            placeholder = preview_canvas.find_withtag("preview-placeholder")
            if placeholder:
                preview_canvas.coords(
                    placeholder[0],
                    preview_canvas.winfo_width() // 2,
                    preview_canvas.winfo_height() // 2,
                )

        def show_preview_message(message):
            preview_canvas.delete("all")
            generated["canvas_image_id"] = None
            generated["photo"] = None
            preview_canvas.create_text(
                0,
                0,
                text=message,
                anchor="center",
                fill="#687586",
                tags=("preview-placeholder",),
            )
            preview_canvas.configure(scrollregion=(0, 0, 0, 0))
            dialog.after_idle(update_placeholder)

        show_preview_message(
            "Wygenerowany obraz pojawi się tutaj."
            if not english
            else "The generated image will appear here."
        )
        preview_canvas.bind("<Configure>", update_placeholder)
        preview_canvas.bind(
            "<ButtonPress-1>", lambda event: preview_canvas.scan_mark(event.x, event.y)
        )
        preview_canvas.bind(
            "<B1-Motion>", lambda event: preview_canvas.scan_dragto(event.x, event.y, gain=1)
        )

        def show_full_preview(_event=None):
            image_bytes = generated["bytes"]
            if not image_bytes:
                return
            with Image.open(BytesIO(image_bytes)) as image:
                original_width, original_height = image.size
            preview_window = tk.Toplevel(dialog)
            preview_window.title("Podgląd obrazu" if not english else "Image preview")
            preview_window.resizable(True, True)
            screen_width = preview_window.winfo_screenwidth()
            screen_height = preview_window.winfo_screenheight()
            window_width = min(original_width + 32, int(screen_width * 0.88))
            window_height = min(original_height + 72, int(screen_height * 0.88))
            preview_window.geometry(f"{max(420, window_width)}x{max(320, window_height)}")
            preview_window.minsize(360, 260)
            preview_window.lift()
            preview_window.columnconfigure(0, weight=1)
            preview_window.rowconfigure(0, weight=1)
            large_preview = ttk.Label(preview_window, anchor="center")
            large_preview.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
            preview_state = {"photo": None, "resize_job": None}

            def render_large_preview():
                if not preview_window.winfo_exists():
                    return
                width = large_preview.winfo_width() - 16
                height = large_preview.winfo_height() - 16
                if width < 30 or height < 30:
                    return
                try:
                    with Image.open(BytesIO(image_bytes)) as image:
                        image.thumbnail((width, height), Image.Resampling.LANCZOS)
                        photo = ImageTk.PhotoImage(image.copy())
                except (OSError, ValueError):
                    return
                preview_state["photo"] = photo
                large_preview.configure(image=photo)
                large_preview.image = photo

            def schedule_large_preview(_event=None):
                if preview_state["resize_job"] is not None:
                    preview_window.after_cancel(preview_state["resize_job"])
                preview_state["resize_job"] = preview_window.after(80, render_large_preview)

            large_preview.bind("<Configure>", schedule_large_preview)
            preview_window.after_idle(render_large_preview)

        preview_canvas.bind("<Double-Button-1>", show_full_preview)

        def save_generated_image():
            image_bytes = generated["bytes"]
            if not image_bytes:
                return
            output_format = generated["format"]
            extension = ".jpg" if output_format == "jpeg" else f".{output_format}"
            default_name = (
                f"generated_{time.strftime('%Y%m%d_%H%M%S')}{extension}"
                if english
                else f"obraz_{time.strftime('%Y%m%d_%H%M%S')}{extension}"
            )
            generated_dir = APP_DIR / "generated_images"
            try:
                generated_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(
                    "Nie udało się przygotować folderu"
                    if not english
                    else "Could not prepare image folder",
                    str(exc),
                    parent=dialog,
                )
                return
            selected_path = filedialog.asksaveasfilename(
                parent=dialog,
                title="Save generated image" if english else "Zapisz wygenerowany obraz",
                initialdir=str(generated_dir),
                initialfile=default_name,
                defaultextension=extension,
                filetypes=[
                    (f"{output_format.upper()} image", f"*{extension}"),
                    ("All files" if english else "Wszystkie pliki", "*.*"),
                ],
            )
            if not selected_path:
                return
            target = Path(selected_path)
            if target.suffix.lower() != extension:
                target = target.with_suffix(extension)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(image_bytes)
            except OSError as exc:
                messagebox.showerror(
                    "Nie udało się zapisać obrazu" if not english else "Could not save image",
                    str(exc),
                    parent=dialog,
                )
                return
            status_var.set((f"Zapisano: {target}" if not english else f"Saved: {target}"))

        save_button.configure(command=save_generated_image)

        def poll_generation():
            try:
                result_type, payload = results.get_nowait()
            except queue.Empty:
                dialog.after(150, poll_generation)
                return

            generate_button.configure(state="normal")
            if result_type == "error":
                status_var.set(
                    "Generowanie nie powiodło się." if not english else "Image generation failed."
                )
                messagebox.showerror(
                    "Błąd generowania" if not english else "Generation error",
                    self.format_error(payload),
                    parent=dialog,
                )
                return

            image_bytes, requested_format = payload
            generated["bytes"] = image_bytes
            generated["format"] = requested_format
            try:
                with Image.open(BytesIO(image_bytes)) as image:
                    image.load()
                    dimensions = f"{image.width} × {image.height}"
                    photo = ImageTk.PhotoImage(image.copy())
            except (OSError, ValueError) as exc:
                generated["bytes"] = None
                generated["format"] = None
                status_var.set(
                    "Nie można wyświetlić odpowiedzi obrazu."
                    if not english
                    else "Could not preview the returned image."
                )
                messagebox.showerror(
                    "Nieprawidłowy obraz" if not english else "Invalid image",
                    str(exc),
                    parent=dialog,
                )
                return

            generated["photo"] = photo
            generated["canvas_image_id"] = preview_canvas.create_image(
                0, 0, anchor="nw", image=photo
            )
            preview_canvas.configure(scrollregion=preview_canvas.bbox(generated["canvas_image_id"]))
            preview_canvas.xview_moveto(0)
            preview_canvas.yview_moveto(0)
            save_button.configure(state="normal")
            status_var.set(
                (
                    f"Gotowe • {dimensions} • przeciągnij obraz lub użyj pasków przewijania; dwuklik powiększa"
                    if not english
                    else f"Ready • {dimensions} • drag or use the scrollbars; double-click to enlarge"
                )
            )

        def start_generation():
            prompt = prompt_box.get("1.0", "end-1c").strip()
            if not prompt:
                messagebox.showwarning(
                    "Brak opisu" if not english else "Missing description",
                    "Wpisz opis obrazu." if not english else "Enter an image description.",
                    parent=dialog,
                )
                prompt_box.focus_set()
                return
            if len(prompt) > 32000:
                messagebox.showwarning(
                    "Opis jest za długi" if not english else "Description is too long",
                    "Opis może mieć maksymalnie 32 000 znaków."
                    if not english
                    else "The description can contain up to 32,000 characters.",
                    parent=dialog,
                )
                prompt_box.focus_set()
                return
            save_button.configure(state="disabled")
            generated["bytes"] = None
            generated["format"] = None
            show_preview_message("Generowanie…" if not english else "Generating…")
            status_var.set(
                (
                    "Edytowanie na podstawie obrazu…"
                    if reference_images and not english
                    else "Editing from reference image(s)…"
                    if reference_images
                    else "Łączenie z OpenAI Images API…"
                    if not english
                    else "Requesting image from OpenAI…"
                )
            )
            generate_button.configure(state="disabled")
            request = {
                "api_key": api_key,
                "prompt": prompt,
                "model": model_var.get(),
                "size": size_var.get(),
                "quality": quality_var.get(),
                "output_format": format_var.get(),
                "reference_images": tuple(reference_images),
            }

            def worker():
                try:
                    image_bytes = self.image_generation_service.generate(**request)
                except Exception as exc:
                    results.put(("error", exc))
                else:
                    results.put(("success", (image_bytes, request["output_format"])))

            threading.Thread(target=worker, daemon=True).start()
            dialog.after(150, poll_generation)

        generate_button.configure(command=start_generation)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        prompt_box.focus_set()

    # ========================================================
    # WYSYŁANIE
    # ========================================================

    def send_shortcut(self, _event=None):
        self.send_message()
        return "break"

    def insert_newline(self, _event=None):
        self.clear_input_placeholder()
        self.input_box.insert("insert", "\n")
        return "break"

    def get_request_settings(self):
        api_key = self.api_key_var.get().strip()
        model = self.model_var.get().strip()

        if not api_key:
            messagebox.showwarning(
                "Brak klucza API",
                "Wpisz klucz API w górnym polu.",
                parent=self,
            )
            self.key_entry.focus_set()
            return None

        if not model:
            messagebox.showwarning(
                "Brak modelu",
                "Wybierz lub wpisz identyfikator modelu.",
                parent=self,
            )
            self.model_combo.focus_set()
            return None

        reason = self.unsupported_model_reason(model)

        if reason:
            messagebox.showwarning(
                "Model nieobsługiwany",
                f"Model: {model}\n\n{reason}",
                parent=self,
            )
            return None

        return api_key, model

    def validate_history(self, model, history):
        if self.uses_chat_completions(model):
            self.to_chat_messages(history)

    def send_message(self):
        if self.busy:
            self.bell()
            return

        text = "" if self.input_placeholder_active else self.input_box.get("1.0", "end-1c").strip()

        if not text and not self.pending_files:
            self.status_var.set("Wpisz wiadomość lub dodaj załącznik.")
            self.input_box.focus_set()
            return

        settings = self.get_request_settings()

        if settings is None:
            return

        api_key, model = settings

        try:
            user_content = self.build_user_content(text)
            history = self.load_api_history()

            new_history = history + [
                {
                    "role": "user",
                    "content": user_content,
                }
            ]

            self.validate_history(model, new_history)

        except (OSError, ValueError, DatabaseError) as exc:
            messagebox.showerror(
                "Nie można wysłać wiadomości",
                str(exc),
                parent=self,
            )
            return

        names = [path.name for path in self.pending_files]

        display_text = text

        if names:
            display_text += ("\n\n" if display_text else "") + "Załączniki: " + ", ".join(names)

        try:
            self.save_selected_model()
            self.save_message(
                "user",
                user_content,
                display_text,
            )

            if not history:
                title_source = text or ", ".join(names)
                title = " ".join(title_source.split())[:45]

                self.db.set_conversation_title(self.current_id, title or "Rozmowa")

        except DatabaseError as exc:
            messagebox.showerror(
                "Błąd zapisu",
                f"Nie udało się zapisać wiadomości:\n{exc}",
                parent=self,
            )
            return

        self.refresh_conversations()
        self.append_chat("user", display_text)

        self.input_box.delete("1.0", tk.END)
        self.input_placeholder_active = False
        self.clear_files()

        self.start_request(api_key, model, new_history, self.web_search_var.get())

    def retry_last_request(self):
        if self.busy:
            return

        try:
            history = self.load_api_history()
        except (OSError, ValueError, DatabaseError) as exc:
            messagebox.showerror(
                "Nie można wczytać rozmowy",
                str(exc),
                parent=self,
            )
            return

        if not history or history[-1]["role"] != "user":
            messagebox.showinfo(
                "Ponawianie",
                "Nie ma wiadomości oczekującej na odpowiedź.\n"
                "Ta funkcja służy do ponowienia po błędzie API.",
                parent=self,
            )
            return

        settings = self.get_request_settings()

        if settings is None:
            return

        api_key, model = settings

        try:
            self.validate_history(model, history)
        except ValueError as exc:
            messagebox.showwarning(
                "Niezgodna historia",
                str(exc),
                parent=self,
            )
            return

        self.save_selected_model()
        self.start_request(api_key, model, history, self.web_search_var.get())

    def start_request(self, api_key, model, history, web_search=False):
        cancel_event = threading.Event()
        self.active_cancel_event = cancel_event
        self.stream_active = False
        self.active_request_id = uuid.uuid4().hex
        self.set_busy(
            True,
            f"Model {model} przygotowuje odpowiedź…",
        )

        threading.Thread(
            target=self.api_worker,
            args=(
                api_key,
                model,
                history,
                web_search,
                self.active_request_id,
                cancel_event,
            ),
            daemon=True,
        ).start()

    def api_worker(
        self, api_key, model, history, web_search=False, request_token=None, cancel_event=None
    ):
        def emit(result_type, payload):
            self.results.put(
                (
                    result_type,
                    {"request_token": request_token, "payload": payload},
                )
            )

        self.api_service.run_request(
            api_key=api_key,
            model=model,
            history=history,
            web_search=web_search,
            cancel_event=cancel_event,
            emit=emit,
            on_stream=lambda stream: setattr(self, "active_stream", stream),
            model_prices=self.model_prices,
            language=self.language,
        )

    def format_error(self, exc):
        return format_error(exc, self.language)

    # ========================================================
    # WYNIKI W GŁÓWNYM WĄTKU
    # ========================================================

    def poll_results(self):
        try:
            while True:
                result_type, payload = self.results.get_nowait()

                api_result_types = {
                    "stream_begin",
                    "stream_chunk",
                    "answer",
                    "cancelled",
                    "api_error",
                }
                if result_type in api_result_types:
                    if not isinstance(payload, dict) or payload.get("request_token") != getattr(
                        self, "active_request_id", None
                    ):
                        continue
                    payload = payload.get("payload")

                if result_type == "stream_begin":
                    self.stream_active = True
                    self.chat.configure(state="normal")
                    self.stream_start = self.chat.index("end-1c")
                    self.chat.insert(
                        "end-1c",
                        ("Assistant" if self.language == "en" else "Asystent") + "\n",
                        ("assistant", "assistant_block"),
                    )
                    self.chat.configure(state="disabled")
                elif result_type == "stream_chunk":
                    self.chat.configure(state="normal")
                    self.chat.insert("end-1c", payload)
                    self.chat.configure(state="disabled")
                    self.chat.see(tk.END)
                elif result_type in {"answer", "cancelled", "api_error"} and self.stream_active:
                    self.chat.configure(state="normal")
                    self.chat.delete(self.stream_start, "end-1c")
                    self.chat.configure(state="disabled")
                    self.stream_active = False
                    if result_type == "cancelled" and payload:
                        payload = {"text": payload, "usage": None}
                        result_type = "answer"

                if result_type == "models":
                    self.set_busy(False)
                    self.model_combo.configure(
                        values=payload,
                        height=20,
                    )
                    self.header_model_combo.configure(values=payload, height=20)
                    self.status_var.set(f"Pobrano {len(payload)} modeli. Wybierz model do rozmowy.")

                elif result_type == "models_error":
                    self.set_busy(False)
                    messagebox.showerror(
                        "Błąd pobierania modeli",
                        payload,
                        parent=self,
                    )

                elif result_type == "answer":
                    answer_text = payload.get("text", "") if isinstance(payload, dict) else payload
                    usage = payload.get("usage") if isinstance(payload, dict) else None
                    self.last_raw_api_text = answer_text
                    self.raw_text_button.configure(state="normal")
                    self.append_chat(
                        "assistant",
                        answer_text,
                    )

                    try:
                        self.save_message(
                            "assistant",
                            answer_text,
                            answer_text,
                        )
                        if usage:
                            self.save_usage(usage)
                    except DatabaseError as exc:
                        messagebox.showerror(
                            "Błąd zapisu odpowiedzi",
                            "Odpowiedź jest w oknie czatu, "
                            "ale nie została zapisana.\n"
                            "Zaznacz ją i skopiuj przez PPM.\n\n"
                            f"{exc}",
                            parent=self,
                        )

                    self.set_busy(False)
                    if usage:
                        self.update_usage_status(usage)
                    if (
                        result_type == "answer"
                        and isinstance(payload, dict)
                        and payload.get("usage") is None
                    ):
                        self.status_var.set(
                            "Generation stopped."
                            if self.language == "en"
                            else "Generowanie zatrzymane."
                        )
                    self.input_box.focus_set()

                elif result_type == "api_error":
                    self.append_chat(
                        "error",
                        payload + "\n\n"
                        "Wiadomość jest zapisana. "
                        "Po usunięciu przyczyny błędu "
                        "kliknij „Ponów”.",
                    )
                    self.set_busy(
                        False,
                        "Błąd API — szczegóły w oknie czatu.",
                    )

                    messagebox.showerror(
                        "Nie udało się uzyskać odpowiedzi",
                        payload,
                        parent=self,
                    )
                    self.input_box.focus_set()

                elif result_type == "cancelled":
                    self.set_busy(
                        False,
                        "Generation stopped."
                        if self.language == "en"
                        else "Generowanie zatrzymane.",
                    )
                    self.input_box.focus_set()

        except queue.Empty:
            pass

        finally:
            self.after(100, self.poll_results)

    # ========================================================
    # BŁĘDY INTERFEJSU I ZAMYKANIE
    # ========================================================

    def report_callback_exception(self, exception_type, exception_value, tb):
        # Dzięki temu wyjątek obsługi przycisku jest widoczny
        # również w aplikacji, a nie tylko w konsoli PyCharm.
        error_key = (exception_type.__name__, str(exception_value))
        now = time.monotonic()
        if error_key == self._last_callback_error and now - self._last_callback_error_at < 5:
            return
        self._last_callback_error = error_key
        self._last_callback_error_at = now
        logging.error(
            "Tkinter callback failed",
            exc_info=(exception_type, exception_value, tb),
        )
        traceback.print_exception(
            exception_type,
            exception_value,
            tb,
        )

        details = "".join(
            traceback.format_exception_only(
                exception_type,
                exception_value,
            )
        )

        messagebox.showerror(
            "Błąd aplikacji",
            details[:2000],
            parent=self,
        )

    def close_app(self):
        if self.busy:
            if not messagebox.askyesno(
                "Zamykanie",
                "Trwa operacja API. Zamknąć aplikację?\n\n"
                "Odpowiedź może nie zostać zapisana. "
                "Zamknięcie nie gwarantuje anulowania "
                "operacji po stronie serwera.",
                parent=self,
            ):
                return

        if not self.busy:
            self.save_selected_model()

        self.db.close()
        self.destroy()
