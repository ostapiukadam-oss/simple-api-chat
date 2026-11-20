# Mój czat — OpenAI API

[English](README.md) · [Polski](README.pl.md)

Desktopowy klient API OpenAI zbudowany w Pythonie i Tkinterze. Aplikacja obsługuje
rozmowy, streaming odpowiedzi, modele Responses API i Chat Completions, wyszukiwanie
internetowe, załączniki, generowanie obrazów przez OpenAI Images API oraz historię
zapisywaną lokalnie w SQLite.

Generator obrazów obsługuje tworzenie z opisu oraz edycję na podstawie dołączonych
obrazów referencyjnych.

W widoku rozmowy przyciski `A−` i `A+` zmieniają rozmiar tekstu odpowiedzi
w zakresie 70–160%; wybrany poziom jest zapamiętywany. Skrócony pasek na dole
pokazuje ostatnie tokeny, koszt i czas, a po najechaniu wyświetla pełne statystyki
bieżącej rozmowy.

## Wymagania

- Python 3.10 lub nowszy
- Windows jest wymagany do zapisywania klucza API przez DPAPI
- Klucz API OpenAI i dostęp do API; używanie API może generować opłaty

## Instalacja

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

W PyCharm wybierz interpreter `.venv` i uruchom `main.py`.

## Struktura projektu

- `main.py` uruchamia aplikację.
- `ui/` zawiera bieżący interfejs Tkinter i kod prezentacji.
- `core/database.py` odpowiada za schemat SQLite, migracje i zapis rozmów.
- `core/api_client.py` obsługuje żądania OpenAI i zgodność modeli.
- `core/image_generation.py` obsługuje generowanie obrazów przez OpenAI Images API.
- `core/attachments.py` odczytuje, koduje i przechowuje załączniki.
- `core/pricing.py` wczytuje szacunkowe ceny modeli.
- `core/security.py` chroni zapisany klucz API przez Windows DPAPI.
- `core/config.py` zawiera ścieżki, limity i domyślne ustawienia.

Pakiet `core/` nie zależy od Tkintera. Można dodać inny interfejs w `ui/` i
korzystać z tych samych usług bazy danych oraz API.

## Przenośny plik EXE

W PowerShellu, w katalogu projektu, zbuduj plik:

```powershell
python -m pip install -r requirements-build.txt
.\build_exe.ps1
```

Gotowy, jednoplikowy program znajdzie się w `dist\MyChat.exe`. Nie wymaga
instalatora ani uprawnień administratora. Możesz skopiować sam plik EXE w inne
miejsce; dane pozostaną w `%USERPROFILE%\myChat`. Pierwsze uruchomienie kopiuje
istniejące dane ze starego folderu `~/.moj_czat_openai`, jeśli są dostępne.

## Dane lokalne i prywatność

Rozmowy są przechowywane w `%USERPROFILE%\myChat\rozmowy.sqlite3`. Baza nie jest
szyfrowana. Zapisany klucz API jest chroniony przez Windows DPAPI. Wklejone obrazy
ze schowka są zapisywane w `%USERPROFILE%\myChat\clipboard_images\`, a załączniki
wiadomości w `%USERPROFILE%\myChat\attachments\`. W bazie przechowywane są ich odwołania.
Wygenerowany obraz można podejrzeć i zapisać jako PNG, JPEG lub WebP w wybranej lokalizacji;
okno zapisu domyślnie otwiera folder `%USERPROFILE%\myChat\generated_images\`.
Aktualne załączniki i treść wiadomości są wysyłane do API podczas zapytań; stare pliki
nie są ponownie dołączane do każdej kolejnej wiadomości.
Szacunki kosztu korzystają z edytowalnego pliku
`%USERPROFILE%\myChat\pricing.json`.

## Ograniczenia

- Obsługiwane typy plików są zdefiniowane w `core/config.py`; pojedynczy plik ma limit 10 MB,
  a partia wiadomości — 20 MB.
- Edycja obrazu obsługuje do 16 obrazów referencyjnych; aplikacja ogranicza je do 10 MB na plik
  i 20 MB łącznie.
- Wyszukiwanie internetowe wymaga Responses API i może wiązać się z dodatkowymi opłatami.
- Generowanie obrazów korzysta z API OpenAI i może wiązać się z opłatami na koncie API.
- Szacowanie kosztu zależy od lokalnej tabeli cen i nie zastępuje rozliczenia konta OpenAI.
- Klucz zapisany przez DPAPI jest dostępny dla tego samego konta Windows na tym komputerze.

## Kontrole jakości

Konfiguracja Ruff znajduje się w `pyproject.toml`. Zainstaluj narzędzia deweloperskie
i uruchom kontrole:

```powershell
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
pytest
```
