"""Tests for medialib.lib.mediaserver - which server the settings name, how
it names films, and the cache a library read is kept in."""

import pytest

from medialib.lib import clioptions, jellyfinserver, mediaserver, plexserver
from medialib.lib.filmmatch import Film
from medialib.lib.mediaserver import Movie

pytestmark = pytest.mark.pure


@pytest.fixture(autouse=True)
def _no_settings(monkeypatch):
    for name in ("mediaServer", "mediaServerNaming"):
        monkeypatch.delenv(name, raising=False)


def test_plex_unless_the_setting_says_otherwise(monkeypatch):
    assert mediaserver.provider().name == "plex"
    monkeypatch.setenv("mediaServer", "Jellyfin")
    assert mediaserver.provider().name == "jellyfin"


def test_a_server_it_does_not_know_is_refused(monkeypatch):
    monkeypatch.setenv("mediaServer", "kodi")
    with pytest.raises(clioptions.UsageError) as error:
        mediaserver.provider()
    assert '"kodi"' in error.value.message and "jellyfin" in error.value.message


def test_names_follow_the_server_unless_set_apart(monkeypatch):
    monkeypatch.setenv("mediaServer", "jellyfin")
    assert mediaserver.naming().name == "jellyfin"
    monkeypatch.setenv("mediaServerNaming", "plex")
    assert mediaserver.naming().name == "plex"


def test_every_provider_names_a_client_that_answers_the_protocol():
    for provider in mediaserver.PROVIDERS.values():
        client = provider.client()
        assert callable(client.from_environment) and callable(client.local)
        server = client.local()
        for method in ("movie_sections", "library", "movies", "copies",
                       "mark_played", "rate", "watchlist", "catalogue_key",
                       "add_to_watchlist"):
            assert callable(getattr(server, method)), (provider.name, method)
        assert provider.name == provider.client().NAME


def test_the_clients_are_the_ones_registered():
    assert mediaserver.PROVIDERS["plex"].client() is plexserver
    assert mediaserver.PROVIDERS["jellyfin"].client() is jellyfinserver


def test_the_hint_names_the_settings_to_set():
    assert mediaserver.PROVIDERS["plex"].hint() == "set plexUrl and plexToken"
    assert "jellyfinApiKey" in mediaserver.PROVIDERS["jellyfin"].hint()


def test_a_cache_is_this_server_s_only():
    sections = [("1", "Films")]
    kept = mediaserver.cache("jellyfin", "http://a", sections, 1, 1, [], [])
    assert mediaserver.cache_is_for(kept, "jellyfin", "http://a", sections)
    assert not mediaserver.cache_is_for(kept, "plex", "http://a", sections)
    assert not mediaserver.cache_is_for(kept, "jellyfin", "http://b", sections)
    assert not mediaserver.cache_is_for(None, "jellyfin", "http://a", sections)


def test_a_cache_from_before_servers_were_named_is_plex_s():
    sections = [("1", "Films")]
    kept = mediaserver.cache("plex", "http://a", sections, 1, 1, [], [])
    del kept["provider"]
    assert mediaserver.cache_is_for(kept, "plex", "http://a", sections)


def test_a_change_is_kept_with_the_day_it_stamped():
    movie = Movie("k", Film("Lantern", "2007"), False, "Films")
    kept = mediaserver.cache("jellyfin", "http://a", [], 1, 1, [movie], [])
    mediaserver.updated(kept, movie._replace(played=True), "2024-05-01")
    mediaserver.updated(kept, movie._replace(played=True))
    entry = kept["movies"][0]
    assert entry["played"] and entry["marked"] == ["2024-05-01"]
    assert mediaserver.unpacked(entry).played
