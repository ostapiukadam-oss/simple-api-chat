import logging
import time

from openai import OpenAI

from .config import SYSTEM_PROMPT, WEB_SEARCH_CALL_USD


def unsupported_model_reason(model):
    name = model.lower()
    if name in {"babbage-002", "davinci-002"}:
        return "Ten model wymaga starszego endpointu Completions, którego aplikacja nie obsługuje."
    if "instruct" in name:
        return "Ta wersja aplikacji nie obsługuje starszych modeli typu instruct."
    if name.startswith(("gpt-image", "chatgpt-image")):
        return "To model generowania obrazów. Wymaga osobnej obsługi Images API."
    if (
        name.startswith(
            ("gpt-audio", "gpt-realtime", "gpt-live", "gpt-transcribe", "tts-", "whisper-")
        )
        or "transcribe" in name
        or "-tts" in name
    ):
        return (
            "To model audio, transkrypcji lub komunikacji w czasie rzeczywistym. "
            "Ten interfejs nie obsługuje audio."
        )
    if name.startswith("text-embedding-"):
        return "To model embeddingów, nie model czatowy."
    if name.startswith("omni-moderation-"):
        return "To model moderacji, nie model czatowy."
    if name.startswith("sora-"):
        return "Ta aplikacja nie obsługuje modeli wideo."
    return None


def uses_chat_completions(model):
    name = model.lower()
    return (
        name.startswith("gpt-3.5-turbo")
        or name == "gpt-4"
        or name.startswith("gpt-4-")
        or name.startswith("gpt-4o-search-preview")
        or name.startswith("gpt-4o-mini-search-preview")
    )


def to_chat_messages(history):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for message in history:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        parts = []
        for part in content:
            part_type = part.get("type")
            if part_type == "input_text":
                parts.append({"type": "text", "text": part["text"]})
            elif part_type == "input_image":
                parts.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
            elif part_type == "input_file":
                raise ValueError(
                    "Historia zawiera PDF. W tej aplikacji PDF-y są obsługiwane przez "
                    "Responses API. Wybierz np. gpt-4.1 lub gpt-4o albo rozpocznij "
                    "rozmowę bez PDF-ów."
                )
            else:
                raise ValueError(f"Nieobsługiwany typ danych: {part_type}")

        converted = (
            "\n\n".join(part["text"] for part in parts)
            if all(part["type"] == "text" for part in parts)
            else parts
        )
        messages.append({"role": role, "content": converted})
    return messages


def format_error(exc, language="pl"):
    status = getattr(exc, "status_code", None)
    exception_name = type(exc).__name__.lower()
    english = language == "en"
    if status == 401:
        summary = (
            "The API key is invalid or revoked."
            if english
            else "Klucz API jest nieprawidłowy lub został unieważniony."
        )
    elif status == 403:
        summary = (
            "This API key does not have access to the selected model."
            if english
            else "Ten klucz API nie ma dostępu do wybranego modelu."
        )
    elif status == 429:
        summary = (
            "The API rate or spending limit was reached. Try again later or check your API account."
            if english
            else "Osiągnięto limit API lub wydatków. Spróbuj później albo sprawdź konto API."
        )
    elif "timeout" in exception_name:
        summary = (
            "The API request timed out. Check your connection and retry."
            if english
            else "Przekroczono czas oczekiwania na API. Sprawdź połączenie i ponów próbę."
        )
    elif "connection" in exception_name:
        summary = (
            "Could not connect to the API. Check your internet connection."
            if english
            else "Nie można połączyć się z API. Sprawdź połączenie internetowe."
        )
    elif status and status >= 500:
        summary = (
            "The API service returned a server error. Try again shortly."
            if english
            else "Usługa API zwróciła błąd serwera. Spróbuj ponownie za chwilę."
        )
    else:
        summary = "The request failed." if english else "Żądanie nie powiodło się."
    return f"{summary}\n\n{type(exc).__name__}: {str(exc)[:1500]}"


class OpenAIService:
    """OpenAI network operations, independent from the selected UI toolkit."""

    def list_models(self, api_key):
        with OpenAI(api_key=api_key, timeout=30.0, max_retries=1) as client:
            return sorted({model.id for model in client.models.list()})

    def run_request(
        self,
        api_key,
        model,
        history,
        web_search,
        cancel_event,
        emit,
        on_stream,
        model_prices,
        language="pl",
    ):
        started_at = time.perf_counter()
        usage_info = {"input_tokens": 0, "output_tokens": 0}
        actual_model = model
        request_id = ""
        search_calls = 0

        try:
            with OpenAI(api_key=api_key, timeout=180.0, max_retries=1) as client:
                if uses_chat_completions(model):
                    if web_search:
                        raise ValueError(
                            "Internet wymaga Responses API. Wybierz model zgodny "
                            "z Responses API, np. gpt-4.1 lub gpt-4o."
                        )
                    stream = client.chat.completions.create(
                        model=model,
                        messages=to_chat_messages(history),
                        store=False,
                        stream=True,
                        stream_options={"include_usage": True},
                    )
                    on_stream(stream)
                    emit("stream_begin", None)
                    answer_parts = []
                    for chunk in stream:
                        if cancel_event.is_set():
                            break
                        actual_model = getattr(chunk, "model", None) or actual_model
                        chunk_usage = getattr(chunk, "usage", None)
                        if chunk_usage:
                            usage_info = {
                                "input_tokens": getattr(chunk_usage, "prompt_tokens", 0) or 0,
                                "output_tokens": getattr(chunk_usage, "completion_tokens", 0) or 0,
                            }
                        choices = getattr(chunk, "choices", [])
                        delta = choices[0].delta.content if choices else None
                        if delta:
                            answer_parts.append(delta)
                            emit("stream_chunk", delta)
                    answer = "".join(answer_parts)
                    response_obj = getattr(stream, "response", None)
                    request_id = getattr(response_obj, "_request_id", "") or getattr(
                        response_obj, "headers", {}
                    ).get("x-request-id", "")
                else:
                    response = None
                    request = {
                        "model": model,
                        "instructions": SYSTEM_PROMPT,
                        "input": history,
                        "store": False,
                    }
                    if web_search:
                        request["tools"] = [{"type": "web_search"}]
                        request["instructions"] += (
                            " Jeśli użytkownik pyta o bieżące informacje albo podaje URL, "
                            "użyj narzędzia web_search, aby otworzyć stronę lub znaleźć "
                            "wiarygodne źródła. Podawaj źródła w odpowiedzi."
                        )
                    emit("stream_begin", None)
                    answer_parts = []
                    stream = client.responses.create(**request, stream=True)
                    on_stream(stream)
                    for event in stream:
                        if cancel_event.is_set():
                            break
                        if event.type == "response.output_text.delta":
                            answer_parts.append(event.delta)
                            emit("stream_chunk", event.delta)
                        elif event.type in {"response.failed", "response.incomplete", "error"}:
                            detail = getattr(event, "error", None)
                            if detail is None:
                                failed_response = getattr(event, "response", None)
                                detail = (
                                    getattr(failed_response, "error", None)
                                    or getattr(failed_response, "incomplete_details", None)
                                    or failed_response
                                )
                            raise RuntimeError(
                                f"Strumień API zakończył się zdarzeniem {event.type}: {detail}"
                            )
                        elif event.type == "response.completed":
                            response = event.response
                            actual_model = getattr(response, "model", None) or actual_model
                            response_usage = getattr(response, "usage", None)
                            if response_usage:
                                usage_info = {
                                    "input_tokens": getattr(response_usage, "input_tokens", 0) or 0,
                                    "output_tokens": getattr(response_usage, "output_tokens", 0)
                                    or 0,
                                }
                            search_calls = sum(
                                1
                                for item in getattr(response, "output", [])
                                if getattr(item, "type", "") == "web_search_call"
                            )
                            request_id = (
                                getattr(response, "_request_id", "")
                                or getattr(getattr(stream, "response", None), "headers", {}).get(
                                    "x-request-id", ""
                                )
                                or getattr(response, "id", "")
                            )
                    answer = "".join(answer_parts)
                    citations = []
                    for item in getattr(response, "output", []):
                        for content in getattr(item, "content", []):
                            for annotation in getattr(content, "annotations", []):
                                if getattr(annotation, "type", None) == "url_citation":
                                    title = getattr(annotation, "title", "Źródło")
                                    url = getattr(annotation, "url", "")
                                    if url and url not in [entry[1] for entry in citations]:
                                        citations.append((title, url))
                    if citations:
                        answer += "\n\nŹródła:\n" + "\n".join(
                            f"- {title}: {url}" for title, url in citations
                        )

            if cancel_event.is_set():
                emit("cancelled", answer)
                return
            if not answer:
                raise RuntimeError(
                    "Model nie zwrócił odpowiedzi tekstowej. Może wymagać innego sposobu obsługi."
                )

            elapsed = time.perf_counter() - started_at
            prices = model_prices(actual_model)
            cost = None
            if prices is not None:
                cost = (
                    usage_info["input_tokens"] * prices[0] + usage_info["output_tokens"] * prices[1]
                ) / 1_000_000 + search_calls * WEB_SEARCH_CALL_USD
            usage_info.update(
                {
                    "model": actual_model,
                    "request_id": request_id,
                    "elapsed": elapsed,
                    "cost": cost,
                }
            )
            emit("answer", {"text": answer, "usage": usage_info})
        except Exception as exc:
            logging.exception("API request failed")
            if cancel_event.is_set():
                emit("cancelled", "")
            else:
                request_id = getattr(exc, "request_id", "") or ""
                detail = format_error(exc, language)
                if request_id:
                    detail += f"\nRequest ID: {request_id}"
                emit("api_error", detail)
        finally:
            on_stream(None)
