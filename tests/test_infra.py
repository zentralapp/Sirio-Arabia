"""Smoke de la infraestructura de tests (sin DB, sin .env)."""


def test_fixture_ahora_art_tiene_zona_argentina(ahora_art):
    momento = ahora_art(2026, 10, 29, 9, 1)

    assert momento.utcoffset().total_seconds() == -3 * 3600
