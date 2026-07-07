"""
services/search_service.py — Mixtape

Handles song search logic.
"""

from app import db
from models import Song


def search_songs(query: str) -> list[dict]:
    """
    Search for songs by title or artist name.

    Returns all songs where the title or artist contains the query string
    (case-insensitive), along with their associated tags.

    Args:
        query: The search string to match against title and artist fields.

    Returns:
        A list of song dicts. Each dict includes all song fields plus a
        'tags' list of tag name strings.
    """
    # Note: we deliberately do NOT join against song_tags here. Tags are loaded
    # via the Song.tags relationship (see to_dict). Joining song_tags would
    # produce one result row per (song, tag) pair, so a song with N tags would
    # appear N times — the source of the duplicate-results bug. Filtering on the
    # song's own columns needs no join at all.
    results = (
        db.session.query(Song)
        .filter(
            db.or_(
                Song.title.ilike(f"%{query}%"),
                Song.artist.ilike(f"%{query}%"),
            )
        )
        .all()
    )

    return [song.to_dict() for song in results]


def get_song(song_id: str) -> dict:
    """
    Get a single song by ID.

    Args:
        song_id: The UUID of the song.

    Returns:
        A song dict, or raises ValueError if not found.
    """
    song = db.session.get(Song, song_id)
    if not song:
        raise ValueError(f"Song {song_id} not found")
    return song.to_dict()
