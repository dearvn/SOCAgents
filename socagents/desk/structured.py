"""Parse a role's JSON output, with one repair attempt."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from socagents.core.errors import ModelError
from socagents.model_gateway.types import Message, ModelProvider, Usage

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    match = _FENCE_RE.search(text)
    if match:
        candidate = match.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("No JSON object found in the reply.")
        candidate = text[start : end + 1]
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("The reply is JSON but not an object.")
    return data


async def parse_or_repair[T: BaseModel](
    model: ModelProvider,
    *,
    system: str,
    text: str,
    output_model: type[T],
    max_tokens: int,
) -> tuple[T, Usage]:
    """Return the parsed output and the usage of the repair call (zero if none was needed)."""
    try:
        return output_model.model_validate(extract_json(text)), Usage()
    except (ValueError, ValidationError) as exc:
        problem = str(exc)[:1500]
    repair = await model.complete(
        system=system,
        messages=[
            Message(
                role="user",
                content=(
                    "Your previous reply was not valid JSON for the required schema.\n"
                    f"Problem: {problem}\n\nPrevious reply:\n{text[:6000]}\n\n"
                    "Reply again with only the corrected JSON object."
                ),
            )
        ],
        tools=[],
        max_tokens=max_tokens,
    )
    try:
        return output_model.model_validate(extract_json(repair.text)), repair.usage
    except (ValueError, ValidationError) as exc:
        raise ModelError(
            f"Invalid structured output after one repair: {str(exc)[:300]}", code="invalid_output"
        ) from exc
