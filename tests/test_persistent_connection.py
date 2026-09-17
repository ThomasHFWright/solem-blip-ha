"""One integration always releases Bluetooth between operations."""
from custom_components.solem_blip.client_factory import create_solem_client, StatelessSolemClient


def test_legacy_persistent_options_cannot_hold_the_link():
    client = create_solem_client(persistent=True, hold_link=True, mac_address="AA:BB:CC:DD:EE:FF")
    assert isinstance(client, StatelessSolemClient)
    assert not hasattr(client, "hold_link")
