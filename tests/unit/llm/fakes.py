"""Fakes for the OpenAI-compatible chat surface — unit tests never hit the network."""

from __future__ import annotations


class FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class FakeChoice:
    def __init__(self, content: str | None, finish_reason: str = "stop") -> None:
        self.message = FakeMessage(content)
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(
        self,
        content: str | None,
        model: str = "vendor/fake-model",
        finish_reason: str = "stop",
    ) -> None:
        self.choices = [FakeChoice(content, finish_reason)]
        self.model = model
        self.usage = FakeUsage(11, 7)


class FakeCompletions:
    """Records every request and replays a scripted list of responses/exceptions."""

    def __init__(self, script) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._script.pop(0) if self._script else FakeResponse("{}")
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            return FakeResponse(item)
        return item


class FakeOpenAIClient:
    def __init__(self, script=()) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions(script)

    @property
    def calls(self) -> list[dict]:
        return self.chat.completions.calls
