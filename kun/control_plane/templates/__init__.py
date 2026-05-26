"""Game-production file templates.

These modules are pure-data: each exports a function returning a `{path:
content}` dict used by `kun.control_plane.game_production` to seed a new
project's working tree. They are domain-specific (game production) and
must not be imported outside that domain.

This subpackage exists so the bulk of control_plane (~4.8k LOC of game
template strings) doesn't sit next to the core runtime/store/v6 modules.
"""
