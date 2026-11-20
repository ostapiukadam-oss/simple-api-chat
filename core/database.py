import json
import sqlite3

DatabaseError = sqlite3.Error


class ChatDatabase:
    """SQLite persistence for conversations, messages, usage, and preferences."""

    def __init__(self, path, attachment_store):
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.attachment_store = attachment_store
        self._create_schema()
        self._migrate()
        self.connection.commit()

    def _create_schema(self):
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                display_text TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL,
                elapsed_seconds REAL NOT NULL DEFAULT 0,
                model TEXT NOT NULL,
                request_id TEXT,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

    def _migrate(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(conversations)")}
        if "created_at" not in columns:
            self.connection.execute("ALTER TABLE conversations ADD COLUMN created_at TEXT")
        if "updated_at" not in columns:
            self.connection.execute("ALTER TABLE conversations ADD COLUMN updated_at TEXT")
        if version < 1:
            self.connection.execute(
                "UPDATE conversations SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP), "
                "updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)"
            )
            self.connection.execute("PRAGMA user_version = 1")
            version = 1
        self.connection.execute(
            "UPDATE conversations SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP), "
            "updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP) "
            "WHERE created_at IS NULL OR updated_at IS NULL"
        )
        if version < 2:
            rows = self.connection.execute("SELECT id, content FROM messages").fetchall()
            for message_id, serialized in rows:
                content = json.loads(serialized)
                persisted, changed = self.attachment_store.persist_content(content)
                if changed:
                    self.connection.execute(
                        "UPDATE messages SET content = ? WHERE id = ?",
                        (json.dumps(persisted, ensure_ascii=False), message_id),
                    )
            self.connection.execute("PRAGMA user_version = 2")

    def get_setting(self, key, default=None):
        row = self.connection.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        self.connection.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.connection.commit()

    def list_conversations(self):
        return self.connection.execute(
            """SELECT id, title, updated_at, model FROM conversations
               ORDER BY COALESCE(updated_at, created_at, '') DESC, id DESC"""
        ).fetchall()

    def create_conversation(self, title, model):
        cursor = self.connection.execute(
            """INSERT INTO conversations (title, model, created_at, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (title, model),
        )
        self.connection.commit()
        return cursor.lastrowid

    def get_conversation_model(self, conversation_id):
        row = self.connection.execute(
            "SELECT model FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        return row[0] if row else None

    def get_conversation_title(self, conversation_id):
        row = self.connection.execute(
            "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        return row[0] if row else None

    def get_display_messages(self, conversation_id):
        return self.connection.execute(
            "SELECT role, display_text FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()

    def get_last_assistant_display(self, conversation_id):
        row = self.connection.execute(
            """SELECT display_text FROM messages
               WHERE conversation_id = ? AND role = 'assistant'
               ORDER BY id DESC LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        return row[0] if row else None

    def get_api_history(self, conversation_id, reuse_attachments=False, language="pl"):
        rows = self.connection.execute(
            "SELECT id, role, content FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        last_pending_user = bool(rows and rows[-1][1] == "user")
        history = []
        for index, (_message_id, role, serialized) in enumerate(rows):
            content = json.loads(serialized)
            if isinstance(content, list):
                include_files = reuse_attachments or (last_pending_user and index == len(rows) - 1)
                parts = []
                for part in content:
                    has_path = part.get("attachment_path") or (
                        isinstance(part.get("image_url"), dict)
                        and "attachment_path" in part["image_url"]
                    )
                    if not has_path:
                        parts.append(part)
                    elif include_files:
                        parts.append(self.attachment_store.hydrate(part))
                    else:
                        name = part.get("filename", "załącznik")
                        placeholder = (
                            f"Earlier attachment ({name}) is not included again."
                            if language == "en"
                            else f"Wcześniejszy załącznik ({name}) nie jest ponownie dołączony."
                        )
                        parts.append({"type": "input_text", "text": placeholder})
                content = parts
            history.append({"role": role, "content": content})
        return history

    def save_message(self, conversation_id, role, content, display_text):
        stored, _changed = self.attachment_store.persist_content(content)
        self.connection.execute(
            """INSERT INTO messages (conversation_id, role, content, display_text)
               VALUES (?, ?, ?, ?)""",
            (conversation_id, role, json.dumps(stored, ensure_ascii=False), display_text),
        )
        self.touch_conversation(conversation_id)
        self.connection.commit()

    def rename_conversation(self, conversation_id, title):
        self.connection.execute(
            "UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id)
        )
        self.connection.commit()

    def set_conversation_model(self, conversation_id, model):
        self.connection.execute(
            "UPDATE conversations SET model = ? WHERE id = ?", (model, conversation_id)
        )
        self.connection.commit()

    def delete_conversation(self, conversation_id):
        self.connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        self.connection.commit()

    def set_conversation_title(self, conversation_id, title):
        self.rename_conversation(conversation_id, title)

    def touch_conversation(self, conversation_id):
        self.connection.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conversation_id,),
        )

    def usage_totals(self, conversation_id):
        if conversation_id is None:
            return (0, 0, 0, 0, 0)
        return self.connection.execute(
            """SELECT COALESCE(SUM(input_tokens), 0),
                      COALESCE(SUM(output_tokens), 0),
                      COALESCE(SUM(estimated_cost_usd), 0),
                      COALESCE(SUM(elapsed_seconds), 0),
                      COALESCE(SUM(CASE WHEN estimated_cost_usd IS NULL THEN 1 ELSE 0 END), 0)
               FROM api_usage WHERE conversation_id = ?""",
            (conversation_id,),
        ).fetchone()

    def latest_usage(self, conversation_id):
        row = self.connection.execute(
            """SELECT input_tokens, output_tokens, estimated_cost_usd,
                      elapsed_seconds, model, request_id
               FROM api_usage WHERE conversation_id = ? ORDER BY id DESC LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "input_tokens": row[0],
            "output_tokens": row[1],
            "cost": row[2],
            "elapsed": row[3],
            "model": row[4],
            "request_id": row[5],
        }

    def save_usage(self, conversation_id, usage):
        self.connection.execute(
            """INSERT INTO api_usage
               (conversation_id, input_tokens, output_tokens, estimated_cost_usd,
                elapsed_seconds, model, request_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                conversation_id,
                usage["input_tokens"],
                usage["output_tokens"],
                usage["cost"],
                usage["elapsed"],
                usage["model"],
                usage["request_id"],
            ),
        )
        self.touch_conversation(conversation_id)
        self.connection.commit()

    def close(self):
        self.connection.close()
