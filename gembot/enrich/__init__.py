"""Resolution and enrichment.

* ``entity``   - hard ids (Steam/itch URL canonicalisation, shortened-link expansion), title
  candidates, and the :class:`~gembot.enrich.entity.Resolver` that clusters mentions into games.
* ``signals``  - hype / negativity / Roblox-meme detection in comment samples.
* ``comments`` - Stage B: fetch comments and author audiences for shortlisted games.
* ``llm``      - optional Claude classifier (only with ``ANTHROPIC_API_KEY``).
"""
