"""Tests for the inter-agent message bus."""

from accessibility_audit_team.agents.base import AgentMessage, MessageBus


def test_send_and_receive():
    bus = MessageBus()
    msg = AgentMessage(from_agent="WAS", to_agent="REE", message_type="capture_evidence")
    bus.send(msg)
    received = bus.receive("REE")
    assert len(received) == 1
    assert received[0].from_agent == "WAS"
    assert received[0].message_type == "capture_evidence"


def test_receive_empties_queue():
    bus = MessageBus()
    bus.send(AgentMessage(from_agent="WAS", to_agent="REE", message_type="test"))
    bus.receive("REE")
    assert bus.receive("REE") == []


def test_message_isolation():
    bus = MessageBus()
    bus.send(AgentMessage(from_agent="WAS", to_agent="REE", message_type="for_ree"))
    bus.send(AgentMessage(from_agent="WAS", to_agent="ATS", message_type="for_ats"))

    ree_msgs = bus.receive("REE")
    ats_msgs = bus.receive("ATS")

    assert len(ree_msgs) == 1
    assert ree_msgs[0].message_type == "for_ree"
    assert len(ats_msgs) == 1
    assert ats_msgs[0].message_type == "for_ats"


def test_receive_nonexistent_agent():
    bus = MessageBus()
    assert bus.receive("NOBODY") == []


def test_pending_count():
    bus = MessageBus()
    assert bus.pending_count("REE") == 0
    bus.send(AgentMessage(from_agent="WAS", to_agent="REE", message_type="a"))
    bus.send(AgentMessage(from_agent="MAS", to_agent="REE", message_type="b"))
    assert bus.pending_count("REE") == 2
    bus.receive("REE")
    assert bus.pending_count("REE") == 0


def test_multiple_messages_ordered():
    bus = MessageBus()
    for i in range(5):
        bus.send(AgentMessage(from_agent="WAS", to_agent="QCR", message_type=f"msg_{i}"))
    received = bus.receive("QCR")
    assert [m.message_type for m in received] == [f"msg_{i}" for i in range(5)]
