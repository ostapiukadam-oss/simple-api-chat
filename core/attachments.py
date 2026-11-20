import base64
import codecs
import hashlib
import mimetypes
from pathlib import Path

from .config import (
    IMAGE_EXTENSIONS,
    MAX_BATCH_BYTES,
    MAX_FILE_BYTES,
    TEXT_EXTENSIONS,
)


def decode_text_file(data):
    """Decode common text encodings used by Windows editors and log files."""
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return data.decode("utf-32")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")

    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass

    for encoding in ("cp1250", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


class AttachmentStore:
    def __init__(self, app_dir):
        self.app_dir = Path(app_dir)
        self.storage_dir = self.app_dir / "attachments"

    def build_user_content(self, text, paths):
        content = []
        total_size = 0
        if text:
            content.append({"type": "input_text", "text": text})

        for path in paths:
            path = Path(path)
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError(f"Plik {path.name} przekracza limit 10 MB.")
            data = path.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                raise ValueError(f"Plik {path.name} przekracza limit 10 MB.")
            total_size += len(data)
            if total_size > MAX_BATCH_BYTES:
                raise ValueError("Łączny rozmiar załączników w jednej wiadomości przekracza 20 MB.")

            extension = path.suffix.lower()
            encoded = base64.b64encode(data).decode("ascii")
            if extension == ".pdf":
                content.append(
                    {
                        "type": "input_file",
                        "filename": path.name,
                        "file_data": f"data:application/pdf;base64,{encoded}",
                    }
                )
            elif extension in IMAGE_EXTENSIONS:
                mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
                content.extend(
                    [
                        {"type": "input_text", "text": f"Załączony obraz: {path.name}"},
                        {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"},
                    ]
                )
            elif extension in TEXT_EXTENSIONS:
                file_text = decode_text_file(data)
                content.append(
                    {
                        "type": "input_text",
                        "text": f"Zawartość załączonego pliku {path.name}:\n\n{file_text}",
                    }
                )
            else:
                raise ValueError(f"Nieobsługiwany format: {path.name}")
        return content

    def persist_content(self, content):
        if not isinstance(content, list):
            return content, False
        stored = []
        changed = False
        for original in content:
            part = dict(original)
            data_url = part.get("file_data")
            field = "file_data"
            if not data_url and part.get("type") == "input_image":
                image_url = part.get("image_url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                data_url = image_url
                field = "image_url"
            if (
                isinstance(data_url, str)
                and data_url.startswith("data:")
                and ";base64," in data_url
            ):
                header, encoded = data_url.split(",", 1)
                raw = base64.b64decode(encoded)
                mime = header[5:].split(";", 1)[0]
                suffix = mimetypes.guess_extension(mime) or ".bin"
                filename = f"{hashlib.sha256(raw).hexdigest()}{suffix}"
                self.storage_dir.mkdir(parents=True, exist_ok=True)
                target = self.storage_dir / filename
                if not target.exists():
                    target.write_bytes(raw)
                part.pop("file_data", None)
                if field == "image_url":
                    part["image_url"] = {"attachment_path": filename}
                else:
                    part["attachment_path"] = filename
                changed = True
            stored.append(part)
        return stored, changed

    def hydrate(self, part):
        filename = part.get("attachment_path")
        if not filename:
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                filename = image_url.get("attachment_path")
        if not filename:
            return part

        target = (self.storage_dir / Path(filename).name).resolve()
        base = self.storage_dir.resolve()
        if base not in target.parents or not target.is_file():
            raise FileNotFoundError(f"Nie znaleziono załącznika: {filename}")
        raw = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        data_url = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
        hydrated = dict(part)
        if "attachment_path" in hydrated:
            hydrated.pop("attachment_path")
            hydrated["file_data"] = data_url
        else:
            hydrated["image_url"] = data_url
        return hydrated
