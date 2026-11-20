import base64
import binascii
from contextlib import ExitStack
from pathlib import Path

from openai import OpenAI


class ImageGenerationService:
    """OpenAI Images API operations, independent from the UI toolkit."""

    def generate(
        self,
        api_key,
        prompt,
        model,
        size,
        quality,
        output_format,
        reference_images=(),
    ):
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("Wpisz opis obrazu.")
        if len(prompt) > 32000:
            raise ValueError("Opis obrazu nie może przekraczać 32 000 znaków.")
        if len(reference_images) > 16:
            raise ValueError("Można dołączyć maksymalnie 16 obrazów referencyjnych.")

        with OpenAI(api_key=api_key, timeout=300.0, max_retries=1) as client:
            if reference_images:
                with ExitStack() as stack:
                    image_files = [
                        stack.enter_context(Path(path).open("rb")) for path in reference_images
                    ]
                    response = client.images.edit(
                        model=model,
                        image=image_files,
                        prompt=prompt,
                        size=size,
                        quality=quality,
                        output_format=output_format,
                        n=1,
                    )
            else:
                response = client.images.generate(
                    model=model,
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    output_format=output_format,
                    n=1,
                )

        image = response.data[0] if response.data else None
        encoded_image = getattr(image, "b64_json", None) if image else None
        if not encoded_image:
            raise RuntimeError("API nie zwróciło danych obrazu.")

        try:
            return base64.b64decode(encoded_image, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError("API zwróciło nieprawidłowe dane obrazu.") from exc
