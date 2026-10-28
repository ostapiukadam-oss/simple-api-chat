# My Chat — OpenAI API

[English](README.md) · [Polski](README.pl.md)

A lightweight Windows desktop client for the OpenAI API, built with Python and Tkinter. It supports streaming conversations, the Responses API and Chat Completions, optional web search, file attachments, and locally stored conversation history.

## Features

- Use your own OpenAI API key and choose an available model.
- Stream responses in the chat window.
- Search the web with the Responses API.
- Attach multiple files and images.
- Keep conversation history in a local SQLite database.
- View token usage, estimated costs, response time, model, and request details.
- Adjust answer text size from 70% to 160% with the `A−` and `A+` controls.
- Switch the interface between Polish and English.

## Download

Download the portable Windows executable from the [latest release](https://github.com/ostapiukadam-oss/simple-api-chat/releases/latest/download/MyChat.exe). It does not require an installer or administrator privileges.

The app uses the API key and billing account configured by each user. API usage may incur charges from OpenAI.

## Run from source

Requirements: Windows and Python 3.10 or newer.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

In PyCharm, select `.venv` as the interpreter and run `main.py`.

## Build the executable

From PowerShell in the project directory:

```powershell
python -m pip install -r requirements-build.txt
.\build_exe.ps1
```

The executable is written to `dist\MyChat.exe`.

## Local data and privacy

Application data is stored in `%USERPROFILE%\myChat`:

- Conversations: `rozmowy.sqlite3` (the database is not encrypted).
- Saved API key: `api_key.dpapi`, protected with Windows DPAPI for the current Windows user.
- Attachments: `attachments\` and pasted clipboard images: `clipboard_images\`.
- Editable model price estimates: `pricing.json`.

The app can also read the `OPENAI_API_KEY` environment variable. The executable does not include the saved key file. Message text and current attachments are sent to the API when you make a request; previous attachments are not automatically sent again with later messages.

## Limits and notes

- Maximum individual attachment size: 10 MB; maximum total attachment size per message: 20 MB.
- Web search requires the Responses API and may incur additional charges.
- Cost figures are estimates based on the local pricing table; they are not a substitute for your OpenAI account usage page.
- A DPAPI-protected key is available to the same Windows user on the same computer.

## Quality checks

Install the development tools and run:

```powershell
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
pytest
```

GitHub Actions runs these checks on pushes and pull requests.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
