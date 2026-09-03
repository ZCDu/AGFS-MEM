"""Filtered, AI-summarized views of a user's wiki, plus the scheduled diary.

This is the "boss / colleagues" layer on top of a user's home wiki:

  - A user owns a private home wiki (kind="home", id == user_id) that may hold
    topic sub-scopes (kind="topic", physically under wikis/{user}/topics/{key}/).
  - Another user (colleague) or a platform admin (boss) can get a FILTERED,
    LLM-SUMMARIZED digest of that wiki -- not a raw dump -- by holding a view
    grant (or being an admin, for oversight).
  - Every such summary is TRANSPARENT: an audit note is written into the
    owner's home wiki and surfaced in their UI, so "the boss looked at my
    wiki" is never silent.
  - The auto-capture timer periodically generates a summary + a chronological
    "diary" of the user's actions, stored under the home wiki.
"""
