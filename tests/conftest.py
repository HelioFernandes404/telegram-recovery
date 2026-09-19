import socket
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is forbidden in tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def message_factory():
    def make(
        message_id=1,
        caption="#F2072\nCamadas de Rede",
        *,
        mime="video/mp4",
        filename="aula.mp4",
        video=True,
        size=1234,
        document=True,
        document_id=77,
    ):
        attributes = []
        if filename is not None:
            attributes.append(DocumentAttributeFilename(filename))
        if video:
            attributes.append(DocumentAttributeVideo(duration=42.5, w=1920, h=1080))
        return SimpleNamespace(
            id=message_id,
            message=caption,
            date=datetime(2024, 1, 1, tzinfo=UTC),
            document=SimpleNamespace(
                id=document_id, attributes=attributes, mime_type=mime, size=size
            )
            if document
            else None,
        )

    return make


class FakeClient:
    """The only exposed method is read-only history retrieval."""

    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    async def get_messages(self, entity, **kwargs):
        self.calls.append({"entity": entity, **kwargs})
        assert kwargs["reverse"] is True
        return sorted((m for m in self.messages if m.id > kwargs["min_id"]), key=lambda m: m.id)[
            : kwargs["limit"]
        ]


@pytest.fixture
def client_factory():
    return FakeClient
