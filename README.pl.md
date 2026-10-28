# Mój czat — OpenAI API

[English](README.md) · [Polski](README.pl.md)

Desktopowy klient API OpenAI zbudowany w Pythonie i Tkinterze. Aplikacja obsługuje
rozmowy, streaming odpowiedzi, modele Responses API i Chat Completions, wyszukiwanie
internetowe, załączniki oraz historię zapisywaną lokalnie w SQLite.

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
Aktualne załączniki i treść wiadomości są wysyłane do API podczas zapytań; stare pliki
nie są ponownie dołączane do każdej kolejnej wiadomości.
Szacunki kosztu korzystają z edytowalnego pliku
`%USERPROFILE%\myChat\pricing.json`.

## Ograniczenia

- Obsługiwane typy plików są zdefiniowane w `main.py`; pojedynczy plik ma limit 10 MB,
  a partia wiadomości — 20 MB.
- Wyszukiwanie internetowe wymaga Responses API i może wiązać się z dodatkowymi opłatami.
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
