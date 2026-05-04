from pathlib import Path

import pytest

from routegate import tunnel

FIXTURE = Path(__file__).parent / "fixtures" / "sample_config.yml"


@pytest.fixture
def doc() -> tunnel.TunnelDoc:
    return tunnel.load(FIXTURE)


def test_hostnames_listed(doc):
    assert doc.hostnames() == [
        "radarr.geoffflix.uk",
        "sonarr.geoffflix.uk",
        "audiobookshelf.geoffflix.uk",
    ]


def test_find_returns_entry(doc):
    e = doc.find("radarr.geoffflix.uk")
    assert e is not None
    assert e["service"] == "http://192.168.1.33:7878"


def test_round_trip_preserves_comments(doc):
    text = tunnel.dumps(doc)
    assert "Catch-all must remain last" in text
    assert "Ingress rules" in text


def test_add_inserts_before_catch_all(doc):
    doc.add("newapp.geoffflix.uk", "http://192.168.1.99:8000")
    last = doc.ingress[-1]
    assert "hostname" not in last  # catch-all still last
    second_last = doc.ingress[-2]
    assert second_last["hostname"] == "newapp.geoffflix.uk"
    assert second_last["service"] == "http://192.168.1.99:8000"


def test_add_duplicate_raises(doc):
    with pytest.raises(ValueError):
        doc.add("radarr.geoffflix.uk", "http://x:1")


def test_update_changes_service(doc):
    doc.update("radarr.geoffflix.uk", service="http://10.0.0.5:7878")
    assert doc.find("radarr.geoffflix.uk")["service"] == "http://10.0.0.5:7878"


def test_remove_drops_entry(doc):
    assert doc.remove("sonarr.geoffflix.uk") is True
    assert "sonarr.geoffflix.uk" not in doc.hostnames()
    # catch-all preserved
    assert "hostname" not in doc.ingress[-1]


def test_remove_missing_returns_false(doc):
    assert doc.remove("nonexistent.geoffflix.uk") is False


def test_full_round_trip_after_mutation(doc):
    doc.add("newapp.geoffflix.uk", "http://192.168.1.99:8000")
    doc.update("radarr.geoffflix.uk", service="http://10.0.0.5:7878")
    doc.remove("sonarr.geoffflix.uk")
    text = tunnel.dumps(doc)
    doc2 = tunnel.loads(text)
    assert doc2.hostnames() == [
        "radarr.geoffflix.uk",
        "audiobookshelf.geoffflix.uk",
        "newapp.geoffflix.uk",
    ]
    assert doc2.find("radarr.geoffflix.uk")["service"] == "http://10.0.0.5:7878"
    # comments still preserved
    assert "Catch-all must remain last" in text
