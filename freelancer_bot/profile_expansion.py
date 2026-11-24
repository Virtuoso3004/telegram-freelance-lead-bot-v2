"""Idempotent operator expansion of the active search profile.

The bot is used for broad vibe-coding work, so the confirmed profile needs
terms for AI-assisted coding, agents, automation and no-code delivery.  The
expansion runs once per deploy: terms already present are skipped, and the
profile revision is bumped only when something actually changed.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import psycopg

ROLE_ADDITIONS = (
    "AI-кодинг инженер",
    "vibe coding разработчик",
    "разработчик AI-агентов",
    "автоматизатор",
    "no-code разработчик",
    "low-code разработчик",
    "интегратор API",
    "MVP-разработчик",
)

SKILL_ADDITIONS = (
    "Cursor",
    "Windsurf",
    "Claude Code",
    "GitHub Copilot",
    "Kilo Code",
    "OpenCode",
    "vibe coding",
    "AI agents",
    "LLM integrations",
    "OpenAI API",
    "Anthropic API",
    "LangChain",
    "n8n",
    "Make",
    "Zapier",
    "Retool",
    "Bubble",
    "Webflow",
    "Supabase",
    "Vercel",
    "Cloudflare Workers",
    "serverless",
    "GraphQL",
    "webhooks",
    "SaaS MVP",
    "rapid prototyping",
)

CATEGORY_ADDITIONS = (
    "AI-автоматизация",
    "AI-интеграции",
    "разработка AI-агентов",
    "no-code/low-code",
    "MVP-разработка",
    "автоматизация бизнес-процессов",
    "интеграции",
    "чат-боты",
    "Telegram-боты",
)


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _load_additions() -> dict[str, tuple[str, ...]]:
    configured = os.environ.get("PROFILE_EXPANSION_JSON", "").strip()
    if not configured:
        return {"roles": ROLE_ADDITIONS, "skills": SKILL_ADDITIONS, "categories": CATEGORY_ADDITIONS}
    try:
        parsed = json.loads(configured)
    except ValueError:
        return {"roles": ROLE_ADDITIONS, "skills": SKILL_ADDITIONS, "categories": CATEGORY_ADDITIONS}
    result: dict[str, tuple[str, ...]] = {}
    for field in ("roles", "skills", "categories"):
        values = parsed.get(field) if isinstance(parsed, dict) else None
        if isinstance(values, list):
            result[field] = tuple(str(value) for value in values if str(value).strip())
        else:
            result[field] = ()
    return result


def _merge_terms(existing: object, additions: tuple[str, ...]) -> tuple[list[dict], list[str]]:
    terms = [dict(term) for term in (existing or []) if isinstance(term, dict)]
    seen = {_normalized(str(term.get("normalized_value") or term.get("value") or "")) for term in terms}
    added: list[str] = []
    for value in additions:
        key = _normalized(value)
        if not key or key in seen:
            continue
        terms.append(
            {
                "value": value,
                "normalized_value": key,
                "origin": "explicit",
                "evidence": "operator expansion: vibe coding",
            }
        )
        seen.add(key)
        added.append(value)
    return terms, added


def apply_profile_expansion(database_url: str) -> None:
    """Add vibecoding terms to the active primary profile, once."""
    url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    if not url:
        return
    additions = _load_additions()
    with psycopg.connect(url, connect_timeout=10) as connection:
        row = connection.execute(
            """SELECT id, roles, skills, categories, semantic_text_original,
                      semantic_text_normalized, revision
               FROM search_profiles
               WHERE is_active = TRUE AND is_primary = TRUE
                 AND confirmation_status = 'confirmed'
               ORDER BY activated_at DESC NULLS LAST, id
               LIMIT 1"""
        ).fetchone()
        if row is None:
            print("render.profile_expansion no_active_profile", flush=True)
            return
        profile_id, roles, skills, categories, original, normalized, revision = row
        new_roles, added_roles = _merge_terms(roles, additions["roles"])
        new_skills, added_skills = _merge_terms(skills, additions["skills"])
        new_categories, added_categories = _merge_terms(categories, additions["categories"])
        added = added_roles + added_skills + added_categories
        if not added:
            return
        original_text = str(original or "")
        semantic_text = " | ".join([original_text, *added]).strip(" |")
        normalized_text = re.sub(r"\s+", " ", semantic_text).strip()
        connection.execute(
            """UPDATE search_profiles
               SET roles = %s, skills = %s, categories = %s,
                   semantic_text_original = %s,
                   semantic_text_normalized = %s,
                   revision = revision + 1,
                   updated_at = %s
               WHERE id = %s""",
            (
                json.dumps(new_roles, ensure_ascii=False),
                json.dumps(new_skills, ensure_ascii=False),
                json.dumps(new_categories, ensure_ascii=False),
                semantic_text,
                normalized_text,
                datetime.now(timezone.utc),
                profile_id,
            ),
        )
        connection.commit()
    print(
        "render.profile_expansion "
        + json.dumps(
            {
                "roles": len(added_roles),
                "skills": len(added_skills),
                "categories": len(added_categories),
                "revision": revision + 1,
            },
            sort_keys=True,
        ),
        flush=True,
    )
