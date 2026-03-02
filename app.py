import boto3
import os
import json
import re
import webbrowser
from notion_client import Client
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

notion = Client(auth=os.getenv("NOTION_TOKEN"))
bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")

STORY_DB = os.getenv("STORY_PLANNER_ID")
BATTLE_DB = os.getenv("BATTLING_FEATURES_ID")

def _normalize_name_key(name: str):
    if not name:
        return ""
    parts = re.findall(r"[A-Za-z]+", name.lower())
    return " ".join(parts)


def _build_name_alias_map(names):
    """Map obvious short/full variants (e.g. Sarah -> Sarah Daniels) to one canonical name."""
    cleaned = [n.strip() for n in names if isinstance(n, str) and n.strip()]
    alias_map = {}
    if not cleaned:
        return alias_map

    # First: normalize pure casing/punctuation variants to one display name.
    by_norm = {}
    for name in cleaned:
        norm = _normalize_name_key(name)
        if not norm:
            continue
        by_norm.setdefault(norm, []).append(name)

    canonical_by_norm = {}
    for norm, variants in by_norm.items():
        canonical = sorted(variants, key=lambda v: (len(v.split()), len(v)))[-1].strip()
        canonical_by_norm[norm] = canonical
        for v in variants:
            alias_map[v] = canonical

    # Second: map single first-name mentions to a unique full-name match.
    full_by_first = {}
    singles_by_first = {}
    for norm, canonical in canonical_by_norm.items():
        parts = norm.split()
        if not parts:
            continue
        first = parts[0]
        if len(parts) >= 2:
            full_by_first.setdefault(first, set()).add(canonical)
        else:
            singles_by_first.setdefault(first, set()).add(canonical)

    for first, singles in singles_by_first.items():
        fulls = full_by_first.get(first, set())
        if len(fulls) != 1:
            continue
        target = sorted(fulls, key=lambda v: (len(v.split()), len(v)))[-1]
        for s in singles:
            alias_map[s] = target

    return alias_map


def _canonicalize_name(name: str, alias_map):
    if not name:
        return ""
    return alias_map.get(name, alias_map.get(name.strip(), name.strip()))


def _build_ai_alias_map(character_aliases):
    alias_map = {}
    for row in (character_aliases or []):
        if not isinstance(row, dict):
            continue
        canonical = (row.get("canonical") or "").strip()
        if not canonical:
            continue
        alias_map[canonical] = canonical
        for a in (row.get("aliases") or []):
            if isinstance(a, str) and a.strip():
                alias_map[a.strip()] = canonical
    return alias_map


def _get_page_content(page_id: str) -> str:
    if not page_id:
        return ""
    pieces = []

    def _walk(block_id):
        try:
            start_cursor = None
            while True:
                kwargs = {"block_id": block_id, "page_size": 50}
                if start_cursor:
                    kwargs["start_cursor"] = start_cursor
                resp = notion.blocks.children.list(**kwargs)
                for block in resp.get("results", []):
                    btype = block.get("type", "")
                    frags = []
                    if btype == "paragraph":
                        frags = block["paragraph"].get("rich_text", [])
                    elif btype in ("heading_1", "heading_2", "heading_3"):
                        frags = block[btype].get("rich_text", [])
                    elif btype == "bulleted_list_item":
                        frags = block["bulleted_list_item"].get("rich_text", [])
                    elif btype == "numbered_list_item":
                        frags = block["numbered_list_item"].get("rich_text", [])
                    elif btype in ("quote", "callout", "toggle"):
                        frags = block[btype].get("rich_text", [])
                    if frags:
                        text = "".join(t.get("plain_text", "") for t in frags).strip()
                        if text:
                            pieces.append(text)
                    if block.get("has_children"):
                        _walk(block["id"])
                if not resp.get("has_more"):
                    break
                start_cursor = resp.get("next_cursor")
        except Exception as e:
            print(f"  [warn] Could not read blocks for {block_id}: {e}")

    _walk(page_id)
    return "\n".join(pieces).strip()


def get_story_entries():
    results = notion.databases.query(**{"database_id": STORY_DB})
    entries = []
    for page in results["results"]:
        props = page["properties"]
        entry = {"id": page["id"]}
        for key, val in props.items():
            if val["type"] == "title" and val["title"]:
                entry["name"] = val["title"][0]["plain_text"]
        if "Category" in props and props["Category"]["type"] == "multi_select":
            entry["category"] = [c["name"] for c in props["Category"]["multi_select"]]
        elif "Category" in props and props["Category"]["type"] == "select" and props["Category"]["select"]:
            entry["category"] = [props["Category"]["select"]["name"]]
        else:
            entry["category"] = []
        if "Chapter" in props:
            chap = props["Chapter"]
            if chap["type"] == "multi_select" and chap["multi_select"]:
                entry["chapter"] = [c["name"] for c in chap["multi_select"]][0]
            elif chap["type"] == "select" and chap["select"]:
                entry["chapter"] = chap["select"]["name"]
            else:
                entry["chapter"] = None
        else:
            entry["chapter"] = None
        if "Status" in props and props["Status"]["type"] == "status" and props["Status"]["status"]:
            entry["status"] = props["Status"]["status"]["name"]
        else:
            entry["status"] = "Unknown"
        if "name" in entry:
            entries.append(entry)

    print(f"  Reading page content for {len(entries)} Story Planner entries...")
    for e in entries:
        content = _get_page_content(e["id"])
        e["content"] = content
        print(f"    > {e['name'][:55]}: {len(content)} chars" if content else f"    > {e['name'][:55]}: (empty)")

    return entries


def build_story_prompt(story_entries):
    chapters = {}
    for e in story_entries:
        ch = e.get("chapter") or "Unassigned"
        chapters.setdefault(ch, []).append(e)

    story_text = "=== STORY PLANNER ===\n"
    sorted_items = sorted(chapters.items(), key=lambda kv: (0 if "overview" in (kv[0] or "").lower() else 1, (kv[0] or "").lower()))
    for ch, entries in sorted_items:
        story_text += f"\nChapter: {ch}\n"
        for e in entries:
            cats = ", ".join(e["category"]) if e["category"] else "No category"
            story_text += f"  - [{cats}] {e['name']} (Status: {e['status']})\n"
            if e.get("content"):
                indented = "\n".join(f"      {line}" for line in e["content"].splitlines() if line.strip())
                story_text += f"      PAGE CONTENT:\n{indented}\n"

    return story_text, chapters


def build_overview_brief(story_entries):
    """Collect core-theme context from Overview chapter first, including Story Outline 0.1."""
    overview_pages = []
    other_outline_pages = []
    for e in story_entries:
        ch = (e.get("chapter") or "").strip().lower()
        name = (e.get("name") or "").strip()
        content = (e.get("content") or "").strip()
        if not content:
            continue
        if ch == "overview":
            overview_pages.append((name, content))
        elif "story outline 0.1" in name.lower():
            other_outline_pages.append((name, content))

    ordered = sorted(overview_pages, key=lambda t: (0 if t[0].lower() == "story outline 0.1" else 1, t[0].lower()))
    ordered.extend(sorted(other_outline_pages, key=lambda t: t[0].lower()))
    if not ordered:
        return ""

    text = "=== CORE BRIEF (READ FIRST) ===\n"
    for name, content in ordered:
        text += f"\nPAGE: {name}\n{content}\n"
    return text.strip()


def ask_nova_story(story_text, overview_brief):
    prompt = f"""You are a lead narrative director at Atlus/Sega specializing in Persona-style games. You are reviewing design documents for a game called "7 Sins" where each chapter is one of the 7 deadly sins.

Your review must be specific, not broad. Anchor every judgement to:
1) core theme and message
2) stated inspirations
3) world-building foundations
from the Overview chapter pages (especially Story Outline 0.1).

CRITICAL ORDER:
- FIRST read CORE BRIEF completely.
- THEN read the full DATABASE.
- Evaluate consistency against the CORE BRIEF before giving critique.

CRITICAL CHARACTER LOGIC:
- Identify only actual in-story people as characters.
- Exclude concept labels, sin names, chapter labels, and generic terms.
- Use reasoning to resolve aliases (example: "Sarah" and "Sarah Daniels" can be the same person when context supports it).
- Include characters referenced in prose/dialogue even without dedicated character docs.
- The main protagonist/player avatar is intentionally unnamed and is a self-insert "you".
- Treat the MC as "You (Unnamed Protagonist)" and do NOT assign any named character (for example Sarah) as the MC.

CORE BRIEF (READ FIRST):
{overview_brief or "No overview pages found."}

DATABASE:
{story_text}

Respond with ONLY a JSON object. No markdown fences, no explanation before or after. The JSON must be complete and valid.

{{
  "narrative_overview": "3-5 sentence summary of current narrative state and emotional tone",
  "story_outline": "2-3 paragraphs: core theme, overall arc, key character journeys",
  "chapter_summaries": [
    {{"chapter": "chapter name", "summary": "analysis tied to core theme/inspirations"}}
  ],
  "page_summaries": [
    {{"page": "exact page title", "summary": "narrative summary of what is written"}}
  ],
  "characters": [
    {{
      "name": "actual person name (e.g. Sarah, Edward Daniels, Yumi) — NOT a sin name, NOT a chapter name, NOT a generic label. Read every page's content carefully to find all named characters.",
      "role": "Boss or Ally or NPC",
      "chapter": "exact chapter name this character belongs to",
      "sin": "deadly sin they represent",
      "emotional_note": "why players will care about them",
      "backstory_summary": "what they have suffered or gone through",
      "arc_status": "Developed or Partially Developed or Undeveloped"
    }}
  ],
  "character_aliases": [
    {{"canonical": "final canonical character name", "aliases": ["name variant 1", "name variant 2"], "reasoning": "why these names are the same person"}}
  ],
  "story_connections": [
    {{"from": "name", "to": "name", "relationship": "one sentence"}}
  ],
  "gaps": [
    {{"title": "gap title", "description": "what is missing and why it fails review", "severity": "Critical or Moderate or Minor"}}
  ],
  "recommendations": [
    {{"title": "title", "detail": "specific actionable note"}}
  ]
}}"""

    response = bedrock.converse(
        modelId="amazon.nova-lite-v1:0",
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 6000}
    )
    raw = response["output"]["message"]["content"][0]["text"].strip()

    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                raw = part
                break

    first = raw.find("{")
    last = raw.rfind("}")
    if first != -1 and last != -1 and last > first:
        raw = raw[first:last + 1]

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"\n[ERROR] JSON parse failed: {e}")
        open_braces = raw.count("{")
        close_braces = raw.count("}")
        open_brackets = raw.count("[")
        close_brackets = raw.count("]")
        repaired = raw
        repaired += "]" * max(0, open_brackets - close_brackets)
        repaired += "}" * max(0, open_braces - close_braces)
        try:
            return json.loads(repaired)
        except Exception:
            pass
        print("Repair failed. Using fallback.")
        print(raw[:1000])
        return {
            "narrative_overview": "Nova analysis could not be parsed as JSON this run.",
            "story_outline": raw[:3000],
            "chapter_summaries": [], "page_summaries": [],
            "characters": [], "character_aliases": [], "story_connections": [],
            "gaps": [], "recommendations": []
        }


def ask_nova_character_roster(overview_brief, story_text, current_characters):
    """Second-pass character extraction to recover missed characters and aliases."""
    current_names = [c.get("name", "") for c in (current_characters or []) if isinstance(c, dict)]
    prompt = f"""You are validating character extraction for "7 Sins".

Rules:
- Read CORE BRIEF first, then full DATABASE.
- Find any missing in-story characters that were not extracted yet.
- The main protagonist is intentionally unnamed and is a self-insert "you", so it should NOT be listed as a named character.
- Include only actual in-story people.
- Resolve aliases when context supports it.

CORE BRIEF:
{overview_brief or "No overview pages found."}

CURRENT EXTRACTED NAMES:
{", ".join(sorted(set(n for n in current_names if n))) or "None"}

DATABASE:
{story_text}

Respond ONLY JSON:
{{
  "additional_characters": [
    {{
      "name": "character name",
      "role": "Boss or Ally or NPC",
      "chapter": "chapter name",
      "sin": "sin or empty",
      "emotional_note": "short note",
      "backstory_summary": "short summary",
      "arc_status": "Developed or Partially Developed or Undeveloped"
    }}
  ],
  "character_aliases": [
    {{"canonical": "canonical name", "aliases": ["alias1", "alias2"], "reasoning": "short reason"}}
  ]
}}"""

    try:
        response = bedrock.converse(
            modelId="amazon.nova-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 4096}
        )
        raw = response["output"]["message"]["content"][0]["text"].strip()
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1:
            raw = raw[first:last + 1]
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None
        parsed.setdefault("additional_characters", [])
        parsed.setdefault("character_aliases", [])
        return parsed
    except Exception as e:
        print(f"  [warn] Character roster recovery failed: {e}")
        return None


def ask_nova_unnamed_protagonist(overview_brief, story_text, characters, story_connections):
    """Analyze the unnamed main protagonist using full story database context."""
    char_names = sorted(set(c.get("name", "") for c in (characters or []) if isinstance(c, dict) and c.get("name")))
    conn_lines = []
    for conn in (story_connections or []):
        fm = conn.get("from", "")
        to = conn.get("to", "")
        rel = conn.get("relationship", "")
        if fm or to or rel:
            conn_lines.append(f"- {fm} -> {to}: {rel}")

    prompt = f"""You are analyzing the main character design for "7 Sins".

Fact constraints:
- The protagonist is intentionally unnamed and is a self-insert "you".
- Do NOT assign any named character (like Sarah) as the MC.
- Use all available pages to infer how "you" are framed by the world, cast, and themes.

CORE BRIEF:
{overview_brief or "No overview pages found."}

KNOWN CHARACTERS:
{", ".join(char_names) or "None"}

KNOWN RELATIONSHIPS:
{chr(10).join(conn_lines) if conn_lines else "None mapped"}

FULL DATABASE:
{story_text}

Respond ONLY JSON:
{{
  "name": "You (Unnamed Protagonist)",
  "analysis": "2-4 paragraphs analyzing you as the self-insert protagonist: narrative role, agency, emotional throughline, and ties to the core theme/inspirations.",
  "strength": "one strongest current element",
  "risk": "one main narrative risk",
  "immediate_fix": "one specific action to improve self-insert protagonist integration"
}}"""

    try:
        response = bedrock.converse(
            modelId="amazon.nova-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 2048}
        )
        raw = response["output"]["message"]["content"][0]["text"].strip()
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1:
            raw = raw[first:last + 1]
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None
        return parsed
    except Exception as e:
        print(f"  [warn] Unnamed protagonist analysis failed: {e}")
        return None


def ask_nova_character(char_name, char_data_list, page_content_map, story_text_snippet):
    """Generate deep per-character analysis via Nova."""
    char_pages = []
    for e_name, content in page_content_map.items():
        if char_name.lower() in e_name.lower() and content:
            char_pages.append(f"PAGE: {e_name}\n{content}")

    roles = list({c.get("role", "") for c in char_data_list if c.get("role")})
    chapters = list({c.get("chapter", "") for c in char_data_list if c.get("chapter")})
    arc = char_data_list[0].get("arc_status", "Undeveloped") if char_data_list else "Undeveloped"
    sin = char_data_list[0].get("sin", "") if char_data_list else ""
    backstory = " ".join(c.get("backstory_summary", "") for c in char_data_list).strip()
    emotional = " ".join(c.get("emotional_note", "") for c in char_data_list).strip()

    page_block = "\n\n".join(char_pages) if char_pages else "No specific page content found."

    prompt = f"""You are a lead narrative director at Atlus/Sega specializing in Persona-style games, reviewing "7 Sins" — a game where each chapter embodies one of the 7 deadly sins.
Ground your analysis in the core theme/message/inspirations established in Overview pages (especially Story Outline 0.1). Avoid broad commentary.
Important: the main protagonist/player avatar is intentionally unnamed and is a self-insert "you". Do not treat Sarah or any named character as the main protagonist.

Perform a deep character analysis of: {char_name}
Role: {", ".join(roles) or "Unknown"}
Chapter(s): {", ".join(chapters) or "Unassigned"}
Sin: {sin or "Unknown"}
Arc Status: {arc}
Known Backstory: {backstory or "None documented"}
Emotional Notes: {emotional or "None documented"}

Character-relevant page content:
{page_block}

Story context:
{story_text_snippet}

Respond ONLY with a valid JSON object. No markdown, no preamble.
{{
  "verdict": "One punchy sentence — is this character working narratively or not?",
  "emotional_core": "What is the emotional truth of this character? What wound drives them?",
  "sin_embodiment": "How do they embody their sin? Is it literal, symbolic, or subverted?",
  "arc_analysis": "Honest breakdown of their current arc. Where does it soar, where does it fail?",
  "player_connection": "Will players care? Why or why not? Be specific.",
  "pivotal_moment": "What scene or moment would define this character if written well?",
  "weakness": "What is the single biggest narrative weakness right now?",
  "director_note": "One actionable note from the narrative director to improve this character immediately.",
  "relationship_web": "How do their relationships shape them? Who matters most to their arc?",
  "overall_score": "X/10 — with one sentence explanation"
}}"""

    try:
        response = bedrock.converse(
            modelId="amazon.nova-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 2048}
        )
        raw = response["output"]["message"]["content"][0]["text"].strip()
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1:
            raw = raw[first:last + 1]
        return json.loads(raw)
    except Exception as e:
        print(f"  [warn] Character analysis failed for {char_name}: {e}")
        return None


def ask_nova_chapter(chapter_name, entries, characters_in_chapter, story_text_snippet):
    """Generate deep per-chapter analysis via Nova."""
    docs = "\n".join(f"- [{', '.join(e.get('category') or [])}] {e.get('name','')} ({e.get('status','')})" for e in entries)
    char_names = [c.get("name", "") for c in characters_in_chapter if c.get("name")]
    page_content_block = "\n\n".join(
        f"PAGE: {e.get('name','')}\n{(e.get('content') or '')}"
        for e in entries if e.get("content")
    )

    prompt = f"""You are a lead narrative director at Atlus/Sega reviewing "7 Sins" — each chapter is one of the 7 deadly sins.
Ground your analysis in the core theme/message/inspirations established in Overview pages (especially Story Outline 0.1). Avoid broad commentary.
Important: the main protagonist/player avatar is intentionally unnamed and is a self-insert "you". Do not treat Sarah or any named character as the main protagonist.

Perform a deep chapter analysis of: {chapter_name}
Characters: {", ".join(char_names) or "None mapped"}
Documents in this chapter:
{docs}

Page content:
{page_content_block or "No content documented."}

Story context:
{story_text_snippet}

Respond ONLY with a valid JSON object. No markdown, no preamble.
{{
  "verdict": "One punchy sentence — is this chapter working or not?",
  "sin_theme": "How does this chapter use its sin as a narrative device?",
  "emotional_peak": "What is or should be the emotional high point of this chapter?",
  "pacing": "Analysis of the chapter's narrative pacing and structure.",
  "character_dynamics": "How do the characters in this chapter interact and drive the story?",
  "strongest_element": "What is working best in this chapter right now?",
  "critical_gap": "What is the most urgent missing piece?",
  "director_note": "One specific, actionable improvement the team should make immediately.",
  "overall_score": "X/10 — one sentence explanation"
}}"""

    try:
        response = bedrock.converse(
            modelId="amazon.nova-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 1536}
        )
        raw = response["output"]["message"]["content"][0]["text"].strip()
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1:
            raw = raw[first:last + 1]
        return json.loads(raw)
    except Exception as e:
        print(f"  [warn] Chapter analysis failed for {chapter_name}: {e}")
        return None


def ask_nova_timeline(story_text, story_entries):
    """Generate a single interleaved master timeline with main plot, sideplots, and past traumas."""
    # Build rich content block — all page content
    all_pages = ""
    for e in story_entries:
        content = (e.get("content") or "").strip()
        if content:
            all_pages += f"\n\nCHAPTER: {e.get('chapter','?')} | PAGE: {e.get('name','?')} | CATEGORIES: {', '.join(e.get('category') or [])}\n{content}"

    prompt = f"""You are a lead narrative director at Atlus/Sega reviewing "7 Sins" — a game where each chapter embodies one of the 7 deadly sins.
Ground your timeline reasoning in the core theme/message/inspirations established in Overview pages (especially Story Outline 0.1).

Your task: reconstruct a COMPLETE MASTER TIMELINE of ALL events in the story — main plot, sideplots, AND past traumatic/backstory events — ordered chronologically by when they OCCURRED (not when they are revealed). Include flashbacks and backstory events at their real chronological position, not where they appear in the narrative.

Story structure overview:
{story_text}

Full page content:
{all_pages}

Rules:
- Infer chronological order from context clues (before/after relationships, ages, cause-and-effect)
- Past traumas and backstory events go BEFORE the main story events that they caused
- Sideplots run parallel to main story events — mark them clearly
- Each event must specify which characters are involved
- Be specific — use actual names, places, and events from the documents
- Include even implied or referenced events if they affect the story

Respond ONLY with a valid JSON object. No markdown, no preamble.
{{
  "timeline": [
    {{
      "id": "unique short id e.g. evt_001",
      "title": "Short event name (5-8 words)",
      "description": "2-3 sentences describing what happens and why it matters narratively",
      "type": "main" or "sideplot" or "trauma" or "flashback" or "offscreen",
      "era": "past" or "present",
      "chapter": "chapter name this connects to, or 'Backstory' if pre-story",
      "characters": ["name1", "name2"],
      "emotional_weight": "low" or "medium" or "high" or "critical",
      "order": 1
    }}
  ],
  "timeline_note": "Brief note about narrative structure and how past traumas feed into present events"
}}"""

    try:
        response = bedrock.converse(
            modelId="amazon.nova-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 4096}
        )
        raw = response["output"]["message"]["content"][0]["text"].strip()
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1:
            raw = raw[first:last + 1]
        result = json.loads(raw)
        # Sort by order field
        if "timeline" in result:
            result["timeline"] = sorted(result["timeline"], key=lambda x: x.get("order", 999))
        return result
    except Exception as e:
        print(f"  [warn] Timeline analysis failed: {e}")
        return None


def generate_html(data, story_entries, chapters, character_analyses=None, chapter_analyses=None, timeline_data=None, unnamed_protagonist_analysis=None):
    timestamp = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    arc_colors = {"Developed": "#4ade80", "Partially Developed": "#facc15", "Undeveloped": "#f87171"}
    severity_colors = {"Critical": "#ef4444", "Moderate": "#f97316", "Minor": "#eab308"}
    sin_colors = {
        "Wrath": "#dc2626", "Pride": "#7c3aed", "Envy": "#16a34a",
        "Greed": "#ca8a04", "Lust": "#db2777", "Gluttony": "#ea580c",
        "Sloth": "#2563eb", "Unassigned": "#475569"
    }

    def get_chapter_color(chapter):
        for sin, color in sin_colors.items():
            if sin.lower() in chapter.lower():
                return color
        return sin_colors["Unassigned"]

    def chapter_matches(ck, cc):
        if not ck or not cc:
            return False
        ck, cc = ck.lower().strip(), cc.lower().strip()
        if ck == cc:
            return True
        ck_base = ck.split("-", 1)[-1].strip()
        cc_base = cc.split("-", 1)[-1].strip()
        return (ck_base and ck_base in cc) or (cc_base and cc_base in ck)

    sin_names = {"wrath", "pride", "envy", "greed", "lust", "gluttony", "sloth"}
    chapter_words = {"overview", "unassigned", "chapter", "prologue", "epilogue"}

    def is_invalid_character_name(name):
        if not name or not name.strip():
            return True
        n = name.lower().strip()
        # Reject if the name IS a sin (exact or with prefix like "1 - Wrath")
        if n in sin_names:
            return True
        # Reject names that are purely sin-based e.g. "Wrath", "3 - Pride", "Pride (Alan)"
        for sin in sin_names:
            if n == sin or n.endswith("- " + sin) or n.startswith(sin + " "):
                return True
        # Reject generic chapter/category labels
        for word in chapter_words:
            if n == word or n.startswith(word + " ") or n.endswith(" " + word):
                return True
        # Reject very short names (1-2 chars) — likely abbreviations/labels
        if len(name.strip()) <= 2:
            return True
        # Must contain at least one letter that could be a name
        if not any(c.isalpha() for c in name):
            return True
        return False

    characters = [
        c for c in (data.get("characters") or [])
        if isinstance(c, dict) and not is_invalid_character_name(c.get("name", ""))
    ]

    existing_keys = {(c.get("name", "").lower(), c.get("chapter", "").lower()) for c in characters}
    for e in story_entries:
        cats = e.get("category") or []
        doc_name = e.get("name") or ""
        cats_lower = [c.lower() for c in cats]
        # Broad category match — any design/character related category
        is_char = any(kw in c for c in cats_lower for kw in ("character", "boss", "ally", "npc", "design", "idol", "villain", "hero", "protagonist", "antagonist"))
        if not is_char or not doc_name:
            continue
        # Extract the character name from doc title (before ":" or "-")
        base_name = doc_name.split(":", 1)[0].split(" - ", 1)[0].strip()
        if is_invalid_character_name(base_name):
            continue
        ch = e.get("chapter") or "Unassigned"
        if (base_name.lower(), ch.lower()) in existing_keys:
            continue
        role = "NPC"
        if any("boss" in c for c in cats_lower):
            role = "Boss"
        elif any("ally" in c or "hero" in c or "protagonist" in c for c in cats_lower):
            role = "Ally"
        characters.append({"name": base_name, "role": role, "chapter": ch, "sin": "", "emotional_note": "", "backstory_summary": (e.get("content") or "")[:500], "arc_status": "Undeveloped"})
        existing_keys.add((base_name.lower(), ch.lower()))

    raw_connections = data.get("story_connections") or []
    ai_alias_map = _build_ai_alias_map(data.get("character_aliases") or [])
    alias_seed_names = [c.get("name", "") for c in characters]
    for conn in raw_connections:
        alias_seed_names.append(conn.get("from", ""))
        alias_seed_names.append(conn.get("to", ""))
    alias_map = _build_name_alias_map(alias_seed_names)
    alias_map.update(ai_alias_map)

    for c in characters:
        c["name"] = _canonicalize_name(c.get("name", ""), alias_map)

    story_connections = []
    for conn in raw_connections:
        from_name = _canonicalize_name(conn.get("from", ""), alias_map)
        to_name = _canonicalize_name(conn.get("to", ""), alias_map)
        rel_text = (conn.get("relationship") or "").strip()
        if not from_name and not to_name and not rel_text:
            continue
        story_connections.append({"from": from_name, "to": to_name, "relationship": rel_text})

    characters_json = json.dumps(characters, ensure_ascii=False)

    char_index_map = {}
    for c in characters:
        name = c.get("name")
        if not name:
            continue
        entry = char_index_map.setdefault(name, {"roles": set(), "chapters": set(), "statuses": set()})
        if c.get("role"): entry["roles"].add(c["role"])
        if c.get("chapter"): entry["chapters"].add(c["chapter"])
        if c.get("arc_status"): entry["statuses"].add(c["arc_status"])

    # Build character cards
    character_cards_html = ""
    role_colors = {"Boss": "#dc2626", "Ally": "#16a34a", "NPC": "#475569"}
    for name, info in sorted(char_index_map.items(), key=lambda x: x[0].lower()):
        roles = ", ".join(sorted(info["roles"])) or "Unknown"
        chs = ", ".join(sorted(info["chapters"])) or "No chapter"
        status = next(iter(sorted(info["statuses"])), "Undeveloped")
        arc_color = arc_colors.get(status, "#94a3b8")
        role_color = role_colors.get(next(iter(sorted(info["roles"])), "NPC"), "#475569")
        safe_name = name.replace('"', "&quot;")
        character_cards_html += f'''<div class="char-card" data-action="show-char" data-name="{safe_name}">
          <div class="char-card-top" style="border-top-color:{role_color}">
            <div class="char-card-name">{name}</div>
            <div class="char-card-role" style="color:{role_color}">{roles}</div>
          </div>
          <div class="char-card-bottom">
            <div class="char-card-chapter">{chs}</div>
            <div class="char-arc-pill" style="background:{arc_color}22;color:{arc_color};border:1px solid {arc_color}44">{status}</div>
          </div>
        </div>'''

    sorted_chapters = sorted(ch for ch in chapters.keys() if ch.lower() not in ("overview", "unassigned"))
    ai_chapter_summaries = data.get("chapter_summaries") or []
    chapter_overview = {}
    chapter_cards_html = ""
    plotlines_html = ""
    timeline_html = ""
    important_cats = {"General Story", "Boss Design", "In Game"}

    for idx, ch in enumerate(sorted_chapters):
        entries = chapters[ch]
        color = get_chapter_color(ch)
        sin_name = ch.split(" - ")[-1] if " - " in ch else ch
        total_docs = len(entries)
        completed = sum(1 for e in entries if e.get("status") in ("Complete", "Completed"))
        chars_in_ch = [c for c in characters if chapter_matches(ch, c.get("chapter"))]
        key_names = [c.get("name") for c in chars_in_ch if c.get("name")]

        ai_summary = next((s.get("summary", "") for s in ai_chapter_summaries if chapter_matches(ch, s.get("chapter", ""))), "")
        fallback = f"{total_docs} docs · {len(chars_in_ch)} characters · {completed}/{total_docs} complete"
        chapter_overview[str(idx)] = {
            "summary": ai_summary or fallback,
            "color": color,
            "sin": sin_name,
            "chars": key_names[:6]
        }

        char_pills = ""
        for char in chars_in_ch[:5]:
            arc = char.get("arc_status", "Undeveloped")
            arc_color = arc_colors.get(arc, "#94a3b8")
            role = char.get("role", "NPC")
            rc = role_colors.get(role, "#475569")
            safe_char = char["name"].replace('"', "&quot;")
            char_pills += f'<button class="char-pill" data-action="show-char" data-name="{safe_char}" style="border-color:{rc}22"><span class="pill-dot" style="background:{rc}"></span>{char["name"]}<span class="arc-dot" style="background:{arc_color}"></span></button>'

        docs_html = ""
        for e in entries:
            has_content = bool(e.get("content"))
            status = e.get("status", "Unknown")
            safe_e = e["name"].replace('"', "&quot;")
            docs_html += f'<div class="doc-row" data-action="show-page" data-name="{safe_e}"><span class="doc-row-name">{e["name"]}</span>{"<span class='doc-has-content'>●</span>" if has_content else ""}<span class="doc-status">{status}</span></div>'

        chapter_cards_html += f'''<div class="ch-card" id="chapter-{idx}" style="--ch-color:{color}" data-action="show-chapter" data-idx="{idx}">
          <div class="ch-card-header">
            <div class="ch-sin-badge" style="background:{color}18;color:{color};border:1px solid {color}30">{sin_name}</div>
            <div class="ch-title">{ch}</div>
            <div class="ch-meta">{total_docs} docs</div>
          </div>
          <div class="ch-chars">{char_pills or "<span class='no-data'>No characters mapped yet</span>"}</div>
          <div class="ch-docs">{docs_html}</div>
        </div>'''

        cat_map = {}
        for e in entries:
            for cat in (e.get("category") or ["Uncategorized"]):
                cat_map.setdefault(cat, []).append(e.get("name", ""))
        beats = "".join(
            f'<div class="pl-beat"><span class="pl-cat">{cat}</span><span class="pl-names">{", ".join(sorted(set(ns)))}</span></div>'
            for cat, ns in sorted(cat_map.items())
        )
        plotlines_html += f'<div class="pl-card" data-action="select-chapter" data-idx="{idx}" style="border-top:2px solid {color}"><div class="pl-header"><span class="pl-title">{ch}</span><span class="pl-count">{total_docs}</span></div><div class="pl-beats">{beats}</div></div>'

        tl_events = sorted(set(e.get("name", "") for e in entries if set(e.get("category") or []) & important_cats))
        if tl_events:
            evts = "".join(f'<div class="tl-evt"><span class="tl-dot" style="background:{color}"></span>{n}</div>' for n in tl_events)
            timeline_html += f'<div class="tl-seg" data-action="select-chapter" data-idx="{idx}"><div class="tl-seg-title" style="color:{color}">{ch}</div>{evts}</div>'

    gaps_html = ""
    for g in (data.get("gaps") or []):
        sev = g.get("severity", "Minor")
        sc = severity_colors.get(sev, "#eab308")
        gaps_html += f'<div class="gap-card" style="border-left:3px solid {sc}"><div class="gap-top"><span class="gap-sev" style="color:{sc}">{sev}</span><span class="gap-title">{g.get("title","")}</span></div><p class="gap-desc">{g.get("description","")}</p></div>'

    recs_html = ""
    for i, r in enumerate((data.get("recommendations") or []), 1):
        recs_html += f'<div class="rec-card"><div class="rec-num">{i:02d}</div><div><div class="rec-title">{r.get("title","")}</div><p class="rec-detail">{r.get("detail","")}</p></div></div>'

    relationship_names = set(char_index_map.keys())
    for conn in story_connections:
        if conn.get("from"):
            relationship_names.add(conn["from"])
        if conn.get("to"):
            relationship_names.add(conn["to"])

    relationship_index = {name: [] for name in relationship_names}
    for conn in story_connections:
        from_name = (conn.get("from") or "").strip()
        to_name = (conn.get("to") or "").strip()
        rel_text = (conn.get("relationship") or "").strip()
        if from_name and from_name in relationship_index:
            relationship_index[from_name].append({
                "direction": "outgoing",
                "other": to_name,
                "relationship": rel_text,
                "reasoning": rel_text
            })
        if to_name and to_name in relationship_index:
            relationship_index[to_name].append({
                "direction": "incoming",
                "other": from_name,
                "relationship": rel_text,
                "reasoning": rel_text
            })

    relationship_character_list_html = ""
    for name in sorted(relationship_names, key=lambda n: n.lower()):
        info = char_index_map.get(name, {"roles": set(), "chapters": set()})
        roles = ", ".join(sorted(info.get("roles") or [])) or "Unknown"
        chapters_txt = ", ".join(sorted(info.get("chapters") or [])) or "Unassigned"
        rel_count = len(relationship_index.get(name, []))
        safe_name = name.replace('"', "&quot;")
        relationship_character_list_html += (
            f'<button class="rel-char-item" data-action="rel-char-select" data-name="{safe_name}">'
            f'<div class="rel-char-name">{name}</div>'
            f'<div class="rel-char-meta">{roles} · {chapters_txt} · {rel_count} links</div>'
            f'</button>'
        )

    page_summaries_map = {ps.get("page"): ps.get("summary", "") for ps in (data.get("page_summaries") or []) if ps.get("page")}
    page_content_map = {}
    for e in story_entries:
        name = e.get("name")
        if not name:
            continue
        page_content_map[name] = (page_summaries_map.get(name) or e.get("content") or "").strip()

    ai_chapter_summaries_map = {cs.get("chapter", ""): cs.get("summary", "") for cs in (data.get("chapter_summaries") or []) if cs.get("chapter")}
    outline_rows = ""
    for ch in sorted_chapters:
        color = get_chapter_color(ch)
        sin_name = ch.split(" - ")[-1] if " - " in ch else ch
        # Best summary: chapter_analyses > ai_chapter_summaries_map > fallback
        ch_analysis = (chapter_analyses or {}).get(ch)
        if ch_analysis and ch_analysis.get("verdict"):
            summary = ch_analysis["verdict"]
        elif ai_chapter_summaries_map.get(ch):
            summary = ai_chapter_summaries_map[ch]
        else:
            entries = chapters.get(ch, [])
            chars_in_ch = [c for c in characters if chapter_matches(ch, c.get("chapter"))]
            summary = f"{len(entries)} docs · {len(chars_in_ch)} characters"
        outline_rows += f'<div class="outline-row" data-action="show-chapter" data-idx="{sorted_chapters.index(ch)}" style="cursor:pointer"><div class="outline-ch" style="color:{color}">{sin_name}</div><div class="outline-summary">{summary}</div></div>'

    main_character_name = ""
    main_character_blurb = "No clear main character could be inferred from current data."
    if char_index_map:
        def main_char_score(name):
            meta = char_index_map.get(name, {})
            roles = meta.get("roles") or set()
            chs = meta.get("chapters") or set()
            rels = relationship_index.get(name) or []
            score = 0
            if "Ally" in roles:
                score += 100
            if "Boss" in roles:
                score -= 20
            score += len(chs) * 8
            score += len(rels) * 3
            if (character_analyses or {}).get(name):
                score += 10
            return score

        main_character_name = max(sorted(char_index_map.keys()), key=main_char_score)
        mc_meta = char_index_map.get(main_character_name, {})
        mc_roles = ", ".join(sorted(mc_meta.get("roles") or [])) or "Unknown"
        mc_chapters = ", ".join(sorted(mc_meta.get("chapters") or [])) or "Unassigned"
        mc_analysis = (character_analyses or {}).get(main_character_name, {})
        mc_blurb_parts = [mc_analysis.get("verdict", ""), mc_analysis.get("emotional_core", ""), mc_analysis.get("arc_analysis", "")]
        mc_blurb = " ".join(p for p in mc_blurb_parts if p).strip() or "No deep Nova character analysis generated yet."
        main_character_blurb = f"Role: {mc_roles} Â· Chapters: {mc_chapters}\n\n{mc_blurb}"

    up = unnamed_protagonist_analysis or {}
    main_character_name = up.get("name") or "You (Unnamed Protagonist)"
    main_character_blurb = up.get("analysis") or "No dedicated unnamed protagonist analysis was generated."
    if up.get("strength") or up.get("risk") or up.get("immediate_fix"):
        strength = up.get("strength", "N/A")
        risk = up.get("risk", "N/A")
        fix = up.get("immediate_fix", "N/A")
        main_character_blurb += f"\n\nStrongest Element: {strength}\nMain Risk: {risk}\nImmediate Fix: {fix}"

    def first_sentence(text):
        if not text:
            return ""
        parts = [p.strip() for p in text.replace("\n", " ").split(".") if p.strip()]
        return (parts[0] + ".") if parts else text.strip()

    top_theme_line = first_sentence(data.get("story_outline", "")) or first_sentence(data.get("narrative_overview", ""))
    sin_cycle = ", ".join(ch.split(" - ")[-1] if " - " in ch else ch for ch in sorted_chapters) or "Not enough chapter data"
    theme_blurb = f"{top_theme_line}\n\nSin Arc Coverage: {sin_cycle}"

    page_content_json = json.dumps(page_content_map, ensure_ascii=False)
    chapter_overview_json = json.dumps(chapter_overview, ensure_ascii=False)
    chapter_names_json = json.dumps(sorted_chapters, ensure_ascii=False)
    chapter_index_json = json.dumps({ch: str(i) for i, ch in enumerate(sorted_chapters)}, ensure_ascii=False)
    relationship_index_json = json.dumps(relationship_index, ensure_ascii=False)
    character_meta_json = json.dumps({
        name: {
            "roles": sorted(list(info.get("roles") or [])),
            "chapters": sorted(list(info.get("chapters") or [])),
            "statuses": sorted(list(info.get("statuses") or []))
        } for name, info in char_index_map.items()
    }, ensure_ascii=False)
    character_analyses_json = json.dumps(character_analyses or {}, ensure_ascii=False)
    chapter_analyses_json = json.dumps(chapter_analyses or {}, ensure_ascii=False)
    timeline_json = json.dumps(timeline_data or {}, ensure_ascii=False)
    timeline_note = ((timeline_data or {}).get("timeline_note") or "").replace("<", "&lt;").replace(">", "&gt;")
    num_chars = len(char_index_map)
    num_gaps = len(data.get("gaps") or [])
    num_chapters = len(sorted_chapters)
    num_conns = len(story_connections)
    story_outline = (data.get("story_outline") or "").replace("<", "&lt;").replace(">", "&gt;")
    narrative_overview = (data.get("narrative_overview") or "").replace("<", "&lt;").replace(">", "&gt;")
    main_character_name_html = (main_character_name or "No Main Character Found").replace("<", "&lt;").replace(">", "&gt;")
    main_character_blurb_html = (main_character_blurb or "").replace("<", "&lt;").replace(">", "&gt;")
    theme_blurb_html = (theme_blurb or "").replace("<", "&lt;").replace(">", "&gt;")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>7 Sins — Story Intelligence Board</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

:root {{
  --bg: #070709;
  --navbar: #0a0a0dee;
  --surface: #0f0f13;
  --surface2: #141418;
  --surface3: #1a1a1f;
  --border: #1e1e24;
  --border2: #2a2a32;
  --text: #e2e2e8;
  --muted: #6b6b78;
  --dim: #3a3a44;
  --accent: #c9a84c;
  --accent2: #e8c96d;
}}

body {{
  background: var(--bg);
  color: var(--text);
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  font-size: 13px;
  line-height: 1.5;
  min-height: 100vh;
  overflow-x: hidden;
}}

/* ── NAVBAR ── */
.navbar {{
  position: fixed;
  top: 0; left: 0; right: 0;
  z-index: 100;
  background: var(--navbar);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 0;
  height: 54px;
  padding: 0 24px;
}}

.navbar-brand {{
  display: flex;
  flex-direction: column;
  justify-content: center;
  margin-right: 28px;
  flex-shrink: 0;
}}

.brand-eyebrow {{
  font-size: 8px;
  letter-spacing: .2em;
  text-transform: uppercase;
  color: var(--muted);
  line-height: 1;
}}

.brand-title {{
  font-size: 17px;
  font-weight: 700;
  letter-spacing: -.02em;
  color: var(--text);
  line-height: 1.2;
}}

.brand-title em {{
  color: var(--accent);
  font-style: normal;
}}

.navbar-nav {{
  display: flex;
  align-items: center;
  gap: 2px;
  flex: 1;
  overflow-x: auto;
  scrollbar-width: none;
}}

.navbar-nav::-webkit-scrollbar {{ display: none; }}

.nav-item {{
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  cursor: pointer;
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
  border-radius: 6px;
  border-bottom: 2px solid transparent;
  transition: all .12s;
  user-select: none;
  white-space: nowrap;
  flex-shrink: 0;
}}

.nav-item:hover {{ color: var(--text); background: var(--surface2); }}

.nav-item.active {{
  color: var(--accent);
  background: var(--surface2);
  border-bottom-color: var(--accent);
}}

.nav-icon {{
  width: 14px;
  height: 14px;
  opacity: .7;
  flex-shrink: 0;
}}

.nav-item.active .nav-icon {{ opacity: 1; }}

.navbar-search {{
  position: relative;
  margin-left: 16px;
  flex-shrink: 0;
}}

.search-input {{
  width: 180px;
  padding: 5px 10px;
  background: var(--surface2);
  border: 1px solid var(--border2);
  border-radius: 6px;
  color: var(--text);
  font-size: 12px;
  outline: none;
  transition: border-color .15s;
}}

.search-input:focus {{ border-color: var(--accent); width: 240px; }}
.search-input {{ transition: width .2s, border-color .15s; }}
.search-input::placeholder {{ color: var(--muted); }}

.search-dropdown {{
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  width: 280px;
  background: var(--surface);
  border: 1px solid var(--border2);
  border-radius: 8px;
  max-height: 320px;
  overflow-y: auto;
  z-index: 200;
  display: none;
  box-shadow: 0 8px 32px #00000066;
}}

.search-item {{
  padding: 8px 12px;
  cursor: pointer;
  border-bottom: 1px solid var(--border);
}}

.search-item:last-child {{ border-bottom: none; }}
.search-item:hover {{ background: var(--surface2); }}
.search-item-label {{ color: var(--text); font-size: 12px; }}
.search-item-meta {{ color: var(--muted); font-size: 10px; margin-top: 1px; }}

.navbar-stats {{
  display: flex;
  gap: 16px;
  margin-left: 20px;
  flex-shrink: 0;
  border-left: 1px solid var(--border);
  padding-left: 20px;
}}

.stat-pill {{
  text-align: center;
  line-height: 1;
}}

.stat-pill-val {{
  font-size: 15px;
  font-weight: 700;
  color: var(--accent);
}}

.stat-pill-label {{
  font-size: 8px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .1em;
  margin-top: 2px;
}}

/* ── MAIN / SCROLL LAYOUT ── */
.main {{
  padding-top: 54px;
  max-width: 1100px;
  margin: 0 auto;
}}

.scroll-section {{
  padding: 48px 40px 0;
  scroll-margin-top: 70px;
}}

.scroll-section:last-child {{
  padding-bottom: 80px;
}}

.section-title {{
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -.02em;
  margin-bottom: 4px;
}}

.section-divider {{
  border: none;
  border-top: 1px solid var(--border);
  margin-bottom: 28px;
}}

.page-sub {{
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 24px;
}}

/* ── OVERVIEW BOX ── */
.overview-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 22px 26px;
  margin-bottom: 28px;
}}

.overview-card-label {{
  font-size: 9px;
  letter-spacing: .18em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 10px;
}}

.overview-card-text {{
  font-size: 14px;
  line-height: 1.75;
  color: var(--text);
  white-space: pre-wrap;
}}

/* ── CORE FOCUS ── */
.core-focus-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}}

.core-focus-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 20px;
}}

.core-focus-title {{
  font-size: 15px;
  font-weight: 700;
  margin-bottom: 8px;
}}

.core-focus-text {{
  font-size: 12px;
  color: var(--muted);
  line-height: 1.7;
  white-space: pre-wrap;
}}

/* ── OUTLINE ROWS ── */
.outline-rows {{ display: flex; flex-direction: column; gap: 0; }}

.outline-row {{
  display: grid;
  grid-template-columns: 160px 1fr;
  gap: 16px;
  padding: 14px 0;
  border-bottom: 1px solid var(--border);
  align-items: start;
}}

.outline-row:last-child {{ border-bottom: none; }}

.outline-ch {{
  font-size: 12px;
  font-weight: 600;
  color: var(--accent);
  padding-top: 1px;
}}

.outline-summary {{
  font-size: 12px;
  color: var(--muted);
  line-height: 1.6;
}}

/* ── CHAPTER GRID ── */
.chapter-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 16px;
  margin-top: 20px;
}}

.ch-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  transition: border-color .15s;
}}

.ch-card:hover {{ border-color: var(--border2); }}
.ch-card.focused {{ border-color: var(--ch-color) !important; box-shadow: 0 0 0 1px var(--ch-color)20; }}

.ch-card-header {{
  padding: 14px 16px 10px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 10px;
  border-top: 3px solid var(--ch-color);
}}

.ch-sin-badge {{
  font-size: 9px;
  letter-spacing: .14em;
  text-transform: uppercase;
  padding: 2px 8px;
  border-radius: 20px;
  flex-shrink: 0;
}}

.ch-title {{
  font-size: 13px;
  font-weight: 600;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}

.ch-meta {{
  font-size: 10px;
  color: var(--muted);
  flex-shrink: 0;
}}

.ch-chars {{
  padding: 10px 16px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  border-bottom: 1px solid var(--border);
  min-height: 42px;
  align-items: center;
}}

.char-pill {{
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 9px 3px 6px;
  background: var(--surface2);
  border: 1px solid var(--border2);
  border-radius: 20px;
  font-size: 11px;
  color: var(--text);
  cursor: pointer;
  transition: all .12s;
}}

.char-pill:hover {{ background: var(--surface3); border-color: var(--accent); color: var(--accent); }}

.pill-dot {{
  width: 6px; height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}}

.arc-dot {{
  width: 5px; height: 5px;
  border-radius: 50%;
  flex-shrink: 0;
  margin-left: 2px;
}}

.no-data {{
  font-size: 11px;
  color: var(--dim);
  font-style: italic;
}}

.ch-docs {{
  padding: 6px 0 4px;
}}

.doc-row {{
  display: flex;
  align-items: center;
  padding: 6px 16px;
  cursor: pointer;
  gap: 8px;
  transition: background .1s;
}}

.doc-row:hover {{ background: var(--surface2); }}
.doc-row:hover .doc-row-name {{ color: var(--accent); }}

.doc-row-name {{
  flex: 1;
  font-size: 11px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}

.doc-has-content {{ color: var(--accent); font-size: 8px; flex-shrink: 0; }}

.doc-status {{
  font-size: 10px;
  color: var(--dim);
  flex-shrink: 0;
}}

/* Chapter focus panel */
.chapter-focus-panel {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 20px;
  margin-bottom: 20px;
  min-height: 60px;
  transition: all .15s;
}}

.cfp-hint {{ color: var(--dim); font-size: 12px; font-style: italic; }}
.cfp-sin {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; margin-bottom: 4px; }}
.cfp-summary {{ font-size: 13px; line-height: 1.6; color: var(--muted); }}
.cfp-chars {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }}

/* ── CHARACTERS ── */
.char-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
  margin-top: 8px;
}}

.char-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  cursor: pointer;
  transition: all .12s;
}}

.char-card:hover {{ border-color: var(--accent); transform: translateY(-1px); }}

.char-card-top {{
  padding: 14px 14px 10px;
  border-top: 3px solid transparent;
}}

.char-card-name {{
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 2px;
}}

.char-card-role {{
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: .1em;
  font-weight: 600;
}}

.char-card-bottom {{
  padding: 8px 14px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}}

.char-card-chapter {{
  font-size: 11px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}

.char-arc-pill {{
  font-size: 9px;
  padding: 2px 8px;
  border-radius: 20px;
  white-space: nowrap;
  flex-shrink: 0;
  text-transform: uppercase;
  letter-spacing: .08em;
  font-weight: 600;
}}

/* ── PLOTLINES ── */
.pl-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 14px;
  margin-top: 8px;
}}

.pl-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  cursor: pointer;
  transition: border-color .12s;
}}

.pl-card:hover {{ border-color: var(--border2); }}

.pl-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 14px 10px;
  border-bottom: 1px solid var(--border);
}}

.pl-title {{ font-size: 13px; font-weight: 600; }}
.pl-count {{ font-size: 11px; color: var(--muted); }}
.pl-beats {{ padding: 8px 14px 10px; display: flex; flex-direction: column; gap: 5px; }}

.pl-beat {{
  display: flex;
  gap: 8px;
  font-size: 11px;
  color: var(--muted);
}}

.pl-cat {{
  font-weight: 600;
  color: var(--text);
  flex-shrink: 0;
  min-width: 80px;
}}

.pl-names {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

/* ── MASTER TIMELINE ── */
.tl-legend {{
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 20px;
  padding: 12px 16px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
}}

.tl-legend-item, .tl-legend-era {{
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--muted);
}}

.tl-type-badge {{
  font-size: 8px;
  font-weight: 700;
  letter-spacing: .1em;
  padding: 2px 6px;
  border-radius: 4px;
  text-transform: uppercase;
}}

.tl-main    {{ background: #3b82f620; color: #60a5fa; border: 1px solid #3b82f640; }}
.tl-sideplot {{ background: #a855f720; color: #c084fc; border: 1px solid #a855f740; }}
.tl-trauma  {{ background: #ef444420; color: #f87171; border: 1px solid #ef444440; }}
.tl-flashback {{ background: #f59e0b20; color: #fbbf24; border: 1px solid #f59e0b40; }}
.tl-offscreen {{ background: #6b728020; color: #9ca3af; border: 1px solid #6b728040; }}

.tl-era-dot {{
  width: 8px; height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}}
.tl-era-past {{ background: var(--dim); }}
.tl-era-present {{ background: var(--accent); }}

.tl-note {{
  font-size: 12px;
  color: var(--muted);
  line-height: 1.7;
  padding: 14px 18px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 0 8px 8px 0;
  margin-bottom: 24px;
  font-style: italic;
}}

.tl-master {{
  position: relative;
  padding-left: 32px;
}}

.tl-master::before {{
  content: '';
  position: absolute;
  left: 11px;
  top: 0; bottom: 0;
  width: 2px;
  background: linear-gradient(to bottom, var(--dim), var(--border2), var(--dim));
}}

.tl-era-divider {{
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 28px 0 20px -32px;
  padding-left: 32px;
}}

.tl-era-label {{
  font-size: 9px;
  font-weight: 700;
  letter-spacing: .22em;
  text-transform: uppercase;
  color: var(--dim);
  white-space: nowrap;
}}

.tl-era-line {{
  flex: 1;
  height: 1px;
  background: var(--border);
}}

.tl-event {{
  position: relative;
  margin-bottom: 16px;
  padding: 14px 16px 14px 20px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  cursor: pointer;
  transition: border-color .15s, background .15s;
}}

.tl-event:hover {{ background: var(--surface2); border-color: var(--border2); }}

.tl-event.tl-ev-high, .tl-event.tl-ev-critical {{
  border-left-width: 3px;
}}

.tl-event::before {{
  content: '';
  position: absolute;
  left: -27px;
  top: 18px;
  width: 10px; height: 10px;
  border-radius: 50%;
  border: 2px solid var(--bg);
  z-index: 1;
}}

.tl-event.tl-ev-main::before    {{ background: #60a5fa; }}
.tl-event.tl-ev-sideplot::before {{ background: #c084fc; }}
.tl-event.tl-ev-trauma::before  {{ background: #f87171; }}
.tl-event.tl-ev-flashback::before {{ background: #fbbf24; }}
.tl-event.tl-ev-offscreen::before {{ background: var(--dim); }}

.tl-event-header {{
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}}

.tl-event-title {{
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  flex: 1;
  min-width: 0;
}}

.tl-event-badges {{
  display: flex;
  gap: 5px;
  flex-shrink: 0;
  flex-wrap: wrap;
}}

.tl-chapter-badge {{
  font-size: 9px;
  padding: 2px 7px;
  border-radius: 4px;
  font-weight: 600;
  letter-spacing: .05em;
  background: var(--surface3);
  border: 1px solid var(--border2);
  color: var(--muted);
  white-space: nowrap;
}}

.tl-event-desc {{
  font-size: 12px;
  color: var(--muted);
  line-height: 1.6;
  margin-bottom: 8px;
}}

.tl-event-chars {{
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}}

.tl-char-tag {{
  font-size: 10px;
  padding: 2px 8px;
  background: var(--surface2);
  border: 1px solid var(--border2);
  border-radius: 20px;
  color: var(--muted);
  cursor: pointer;
  transition: all .12s;
}}

.tl-char-tag:hover {{ border-color: var(--accent); color: var(--accent); }}

/* weight-based left border colors */
.tl-ev-critical {{ border-left-color: #ef4444; }}
.tl-ev-high     {{ border-left-color: var(--accent); }}

/* keep old pl/tl CSS for any fallback usage */

/* ── GAPS & RECS ── */
.gaps-list, .recs-list {{ display: flex; flex-direction: column; gap: 12px; margin-top: 8px; }}

.gap-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 20px;
}}

.gap-top {{
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}}

.gap-sev {{
  font-size: 9px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .14em;
}}

.gap-title {{
  font-size: 14px;
  font-weight: 600;
}}

.gap-desc {{
  font-size: 12px;
  color: var(--muted);
  line-height: 1.65;
}}

.rec-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 20px;
  display: flex;
  gap: 18px;
}}

.rec-num {{
  font-size: 24px;
  font-weight: 800;
  color: var(--accent);
  line-height: 1;
  flex-shrink: 0;
  min-width: 36px;
}}

.rec-title {{
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 4px;
}}

.rec-detail {{
  font-size: 12px;
  color: var(--muted);
  line-height: 1.65;
}}

/* ── RELATIONSHIPS ── */
.rel-explorer {{
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  gap: 16px;
  margin-top: 8px;
}}

.rel-char-list {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 8px;
  max-height: 70vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}}

.rel-char-item {{
  text-align: left;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 8px;
  padding: 10px 11px;
  color: var(--text);
  cursor: pointer;
  transition: border-color .12s, background .12s;
}}

.rel-char-item:hover {{ border-color: var(--border2); background: var(--surface2); }}
.rel-char-item.active {{ border-color: var(--accent); background: var(--surface2); }}

.rel-char-name {{ font-size: 13px; font-weight: 600; margin-bottom: 3px; }}
.rel-char-meta {{ font-size: 11px; color: var(--muted); line-height: 1.45; }}

.rel-info {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 18px;
  min-height: 320px;
}}

.rel-info-empty {{
  color: var(--dim);
  font-style: italic;
  font-size: 12px;
  padding-top: 8px;
}}

.rel-info-head {{
  margin-bottom: 14px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
}}

.rel-info-name {{ font-size: 18px; font-weight: 700; margin-bottom: 4px; }}
.rel-info-meta {{ font-size: 12px; color: var(--muted); line-height: 1.6; }}

.rel-reasoning {{
  background: var(--surface2);
  border: 1px solid var(--border2);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 14px;
}}

.rel-reasoning-label {{
  font-size: 9px;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: var(--dim);
  margin-bottom: 5px;
}}

.rel-reasoning-text {{
  font-size: 12px;
  color: var(--muted);
  line-height: 1.65;
  white-space: pre-wrap;
}}

.rel-links {{
  display: flex;
  flex-direction: column;
  gap: 9px;
}}

.rel-link-card {{
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
}}

.rel-link-top {{
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}}

.rel-dir {{
  font-size: 9px;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--dim);
}}

.rel-other {{
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
  cursor: pointer;
}}

.rel-other:hover {{ color: var(--accent2); }}

.rel-link-body {{
  font-size: 12px;
  color: var(--muted);
  line-height: 1.6;
  white-space: pre-wrap;
}}

/* ── MODALS ── */
.modal-backdrop {{
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,.75);
  backdrop-filter: blur(4px);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  pointer-events: none;
  transition: opacity .2s;
}}

.modal-backdrop.open {{
  opacity: 1;
  pointer-events: all;
}}

.modal {{
  background: var(--surface);
  border: 1px solid var(--border2);
  border-radius: 14px;
  padding: 28px;
  max-width: 520px;
  width: calc(100% - 40px);
  max-height: 85vh;
  overflow-y: auto;
  position: relative;
}}

.modal-wide {{
  max-width: 720px;
}}

.modal-close {{
  position: absolute;
  top: 16px; right: 16px;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--muted);
  width: 28px; height: 28px;
  cursor: pointer;
  font-size: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all .12s;
}}

.modal-close:hover {{ color: var(--text); border-color: var(--border2); }}

.modal-tag {{
  font-size: 9px;
  letter-spacing: .18em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 6px;
}}

.modal-name {{
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -.02em;
  margin-bottom: 4px;
}}

.modal-sub {{
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 16px;
}}

.modal-verdict {{
  font-size: 15px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--text);
  padding: 14px 18px;
  background: var(--surface2);
  border: 1px solid var(--border2);
  border-radius: 8px;
  margin-bottom: 4px;
}}

.modal-grid-2 {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 4px;
}}

@media (max-width: 980px) {{
  .rel-explorer {{ grid-template-columns: 1fr; }}
  .rel-char-list {{ max-height: 320px; }}
  .core-focus-grid {{ grid-template-columns: 1fr; }}
}}

@media (max-width: 600px) {{
  .modal-grid-2 {{ grid-template-columns: 1fr; }}
}}

.modal-section {{
  margin-bottom: 14px;
}}

.modal-section-label {{
  font-size: 9px;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: var(--dim);
  margin-bottom: 5px;
}}

.modal-section-value {{
  font-size: 12px;
  line-height: 1.65;
  color: var(--muted);
  white-space: pre-wrap;
}}

.modal-warning-card {{
  background: #ef444410;
  border: 1px solid #ef444428;
  border-radius: 8px;
  padding: 12px 14px;
}}

.modal-accent-card {{
  background: var(--accent)10;
  border: 1px solid var(--accent)28;
  border-radius: 8px;
  padding: 12px 14px;
}}

.arc-badge {{
  display: inline-block;
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .1em;
  text-transform: uppercase;
}}

.nova-spinner {{
  width: 32px; height: 32px;
  border: 3px solid var(--border2);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin .8s linear infinite;
  margin: 0 auto;
}}

@keyframes spin {{ to {{ transform: rotate(360deg); }} }}

.ch-card {{ cursor: pointer; }}
.ch-card:hover {{ border-color: var(--ch-color) !important; }}

/* ── LEGEND ── */
.legend {{
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  margin-bottom: 20px;
}}

.legend-item {{
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--muted);
}}

.legend-dot {{
  width: 8px; height: 8px;
  border-radius: 50%;
}}

.section-label {{
  font-size: 9px;
  letter-spacing: .16em;
  text-transform: uppercase;
  color: var(--dim);
  margin: 24px 0 12px;
}}

</style>
</head>
<body>

<nav class="navbar">
  <div class="navbar-brand">
    <div class="brand-eyebrow">Studio Agent</div>
    <div class="brand-title">7 <em>Sins</em></div>
  </div>

  <div class="navbar-nav">
    <div class="nav-item active" data-section="outline">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4h12M2 8h8M2 12h10"/></svg>
      Outline
    </div>
    <div class="nav-item" data-section="focus">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="5"/><path d="M8 5v3l2 2"/></svg>
      Core Focus
    </div>
    <div class="nav-item" data-section="board">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/></svg>
      Overview
    </div>
    <div class="nav-item" data-section="plotlines">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 8h3l2-5 3 10 2-5h2"/></svg>
      Plotlines
    </div>
    <div class="nav-item" data-section="relationships">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="4" cy="8" r="2"/><circle cx="12" cy="4" r="2"/><circle cx="12" cy="12" r="2"/><path d="M6 8h2l2-3M6 8h2l2 3"/></svg>
      Relationships
    </div>
    <div class="nav-item" data-section="missing">
      <svg class="nav-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 2v8M8 12v2"/><circle cx="8" cy="8" r="6"/></svg>
      Missing Pieces
    </div>
  </div>

  <div class="navbar-search">
    <input id="search-input" class="search-input" type="text" placeholder="Search…" autocomplete="off" oninput="doSearch(this.value)" />
    <div id="search-dropdown" class="search-dropdown"></div>
  </div>

  <div class="navbar-stats">
    <div class="stat-pill">
      <div class="stat-pill-val">{num_chapters}</div>
      <div class="stat-pill-label">Chapters</div>
    </div>
    <div class="stat-pill">
      <div class="stat-pill-val">{num_chars}</div>
      <div class="stat-pill-label">Characters</div>
    </div>
    <div class="stat-pill">
      <div class="stat-pill-val">{num_conns}</div>
      <div class="stat-pill-label">Connections</div>
    </div>
    <div class="stat-pill">
      <div class="stat-pill-val">{num_gaps}</div>
      <div class="stat-pill-label">Gaps</div>
    </div>
  </div>
</nav>

<main class="main">

  <section id="section-outline" class="scroll-section">
    <div class="section-title">Story Outline</div>
    <div class="page-sub">High-level narrative arc and chapter breakdown · {timestamp}</div>
    <div class="overview-card">
      <div class="overview-card-label">Narrative Overview</div>
      <div class="overview-card-text">{narrative_overview}</div>
    </div>
    <div class="overview-card">
      <div class="overview-card-label">Full Story Outline</div>
      <div class="overview-card-text">{story_outline}</div>
    </div>
    <div class="section-label">Chapters at a Glance</div>
    <div class="outline-rows">{outline_rows or '<div style="color:var(--dim);font-style:italic;font-size:12px">No chapter summaries generated.</div>'}</div>
  </section>

  <section id="section-focus" class="scroll-section">
    <div class="section-title">Main Character &amp; Story Theme</div>
    <div class="page-sub">Focused read on protagonist strength and thematic clarity</div>
    <div class="core-focus-grid">
      <div class="core-focus-card">
        <div class="overview-card-label">Main Character</div>
        <div class="core-focus-title">{main_character_name_html}</div>
        <div class="core-focus-text">{main_character_blurb_html}</div>
      </div>
      <div class="core-focus-card">
        <div class="overview-card-label">Story Theme</div>
        <div class="core-focus-title">Thematic Direction</div>
        <div class="core-focus-text">{theme_blurb_html}</div>
      </div>
    </div>
  </section>

  <section id="section-board" class="scroll-section">
    <div class="section-title">Chapter Overview</div>
    <div class="page-sub">Click a chapter card to explore details</div>

    <div class="chapter-focus-panel" id="focus-panel">
      <div class="cfp-hint">Click a chapter to see its summary and characters here</div>
    </div>

    <div class="legend">
      <div class="legend-item"><div class="legend-dot" style="background:#4ade80"></div>Developed</div>
      <div class="legend-item"><div class="legend-dot" style="background:#facc15"></div>Partially Developed</div>
      <div class="legend-item"><div class="legend-dot" style="background:#f87171"></div>Undeveloped</div>
      <div class="legend-item" style="margin-left:8px"><span style="color:var(--muted)">● = has page content</span></div>
    </div>

    <div class="chapter-grid">{chapter_cards_html}</div>
  </section>

  <section id="section-plotlines" class="scroll-section">
    <div class="section-title">Master Timeline</div>
    <div class="page-sub">All events — main story, sideplots &amp; past traumas — ordered chronologically</div>

    <div class="tl-legend">
      <div class="tl-legend-item"><span class="tl-type-badge tl-main">MAIN</span> Main story</div>
      <div class="tl-legend-item"><span class="tl-type-badge tl-sideplot">SIDE</span> Sideplot</div>
      <div class="tl-legend-item"><span class="tl-type-badge tl-trauma">TRAUMA</span> Past trauma</div>
      <div class="tl-legend-item"><span class="tl-type-badge tl-flashback">FLASH</span> Flashback</div>
      <div class="tl-legend-item"><span class="tl-type-badge tl-offscreen">OFF</span> Offscreen</div>
      <div class="tl-legend-era"><span class="tl-era-dot tl-era-past"></span>Past</div>
      <div class="tl-legend-era"><span class="tl-era-dot tl-era-present"></span>Present</div>
    </div>

    {f'<div class="tl-note">{timeline_note}</div>' if timeline_note else ''}

    <div class="tl-master" id="tl-master">
      <div style="color:var(--dim);font-style:italic;padding:20px 0" id="tl-loading">Loading timeline…</div>
    </div>
  </section>

  <section id="section-relationships" class="scroll-section">
    <div class="section-title">Character Relationships</div>
    <div class="page-sub">Every character with all mapped relationship links and reasoning</div>
    <div class="rel-explorer">
      <div class="rel-char-list" id="rel-char-list">{relationship_character_list_html or '<div style="color:var(--dim);font-style:italic;padding:8px">No characters mapped yet.</div>'}</div>
      <div class="rel-info" id="rel-info-box">
        <div class="rel-info-empty">Select a character to view their full relationship web and reasoning.</div>
      </div>
    </div>
  </section>

  <section id="section-missing" class="scroll-section">
    <div class="section-title">Missing &amp; Fragile Threads</div>
    <div class="page-sub">Brutal honest review from your Atlus/Sega narrative director</div>
    <div class="section-label">Gaps</div>
    <div class="gaps-list">{gaps_html or '<div style="color:var(--dim);font-style:italic">No gaps identified.</div>'}</div>
    <div class="section-label">Director Recommendations</div>
    <div class="recs-list">{recs_html or '<div style="color:var(--dim);font-style:italic">No recommendations yet.</div>'}</div>
  </section>

</main>

<div class="modal-backdrop" id="char-modal" data-action="close-modal" data-modal="char-modal">
  <div class="modal modal-wide">
    <button class="modal-close" data-action="close-modal" data-modal="char-modal">&#215;</button>
    <div class="modal-tag" id="cm-role"></div>
    <div class="modal-name" id="cm-name"></div>
    <div class="modal-sub" id="cm-sub"></div>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:20px;flex-wrap:wrap">
      <span class="arc-badge" id="cm-arc"></span>
    </div>

    <div id="cm-nova-loading" style="display:none;text-align:center;padding:24px 0">
      <div class="nova-spinner"></div>
      <div style="color:var(--muted);font-size:12px;margin-top:10px">Nova is thinking…</div>
    </div>
    <div id="cm-no-analysis" style="display:none;color:var(--dim);font-style:italic;font-size:12px;padding:8px 0">No Nova analysis generated for this character.</div>

    <div id="cm-nova-body">
      <div id="cm-verdict-section" class="modal-section" style="display:none">
        <div class="modal-verdict" id="cm-verdict"></div>
      </div>

      <div class="modal-grid-2">
        <div id="cm-emotional-section" class="modal-section" style="display:none">
          <div class="modal-section-label">Emotional Core</div>
          <div class="modal-section-value" id="cm-emotional"></div>
        </div>
        <div id="cm-sin-section" class="modal-section" style="display:none">
          <div class="modal-section-label">Sin Embodiment</div>
          <div class="modal-section-value" id="cm-sin-emb"></div>
        </div>
        <div id="cm-arc-section" class="modal-section" style="display:none">
          <div class="modal-section-label">Arc Analysis</div>
          <div class="modal-section-value" id="cm-arc-analysis"></div>
        </div>
        <div id="cm-player-section" class="modal-section" style="display:none">
          <div class="modal-section-label">Player Connection</div>
          <div class="modal-section-value" id="cm-player"></div>
        </div>
        <div id="cm-pivotal-section" class="modal-section" style="display:none">
          <div class="modal-section-label">Pivotal Moment</div>
          <div class="modal-section-value" id="cm-pivotal"></div>
        </div>
        <div id="cm-rels-section" class="modal-section" style="display:none">
          <div class="modal-section-label">Relationship Web</div>
          <div class="modal-section-value" id="cm-rels"></div>
        </div>
      </div>

      <div id="cm-weakness-section" class="modal-section modal-warning-card" style="display:none">
        <div class="modal-section-label">⚠ Critical Weakness</div>
        <div class="modal-section-value" id="cm-weakness"></div>
      </div>
      <div id="cm-director-section" class="modal-section modal-accent-card" style="display:none">
        <div class="modal-section-label">🎬 Director's Note</div>
        <div class="modal-section-value" id="cm-director"></div>
      </div>
    </div>

    <!-- Fallback sections (shown when no nova analysis) -->
    <div id="cm-backstory-section" class="modal-section" style="display:none">
      <div class="modal-section-label">Backstory</div>
      <div class="modal-section-value" id="cm-backstory"></div>
    </div>
    <div id="cm-note-section" class="modal-section">
      <div class="modal-section-label">Narrative Potential</div>
      <div class="modal-section-value" id="cm-note"></div>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="chapter-modal" data-action="close-modal" data-modal="chapter-modal">
  <div class="modal modal-wide">
    <button class="modal-close" data-action="close-modal" data-modal="chapter-modal">&#215;</button>
    <div class="modal-tag" id="chm-sin"></div>
    <div class="modal-name" id="chm-name"></div>
    <div class="modal-sub" id="chm-sub"></div>

    <div id="chm-nova-loading" style="display:none;text-align:center;padding:24px 0">
      <div class="nova-spinner"></div>
      <div style="color:var(--muted);font-size:12px;margin-top:10px">Nova is thinking…</div>
    </div>
    <div id="chm-no-analysis" style="display:none;color:var(--dim);font-style:italic;font-size:12px;padding:8px 0">No Nova analysis generated for this chapter.</div>

    <div id="chm-nova-body">
      <div id="chm-verdict-section" class="modal-section" style="display:none">
        <div class="modal-verdict" id="chm-verdict"></div>
      </div>
      <div class="modal-grid-2">
        <div id="chm-sin-section" class="modal-section" style="display:none">
          <div class="modal-section-label">Sin as Narrative Device</div>
          <div class="modal-section-value" id="chm-sin-theme"></div>
        </div>
        <div id="chm-peak-section" class="modal-section" style="display:none">
          <div class="modal-section-label">Emotional Peak</div>
          <div class="modal-section-value" id="chm-peak"></div>
        </div>
        <div id="chm-pacing-section" class="modal-section" style="display:none">
          <div class="modal-section-label">Pacing &amp; Structure</div>
          <div class="modal-section-value" id="chm-pacing"></div>
        </div>
        <div id="chm-chars-section" class="modal-section" style="display:none">
          <div class="modal-section-label">Character Dynamics</div>
          <div class="modal-section-value" id="chm-chars-dyn"></div>
        </div>
        <div id="chm-strength-section" class="modal-section" style="display:none">
          <div class="modal-section-label">✓ Strongest Element</div>
          <div class="modal-section-value" id="chm-strength"></div>
        </div>
        <div id="chm-gap-section" class="modal-section" style="display:none">
          <div class="modal-section-label">✗ Critical Gap</div>
          <div class="modal-section-value" id="chm-gap"></div>
        </div>
      </div>
      <div id="chm-director-section" class="modal-section modal-accent-card" style="display:none">
        <div class="modal-section-label">🎬 Director's Note</div>
        <div class="modal-section-value" id="chm-director"></div>
      </div>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="page-modal" data-action="close-modal" data-modal="page-modal">
  <div class="modal">
    <button class="modal-close" data-action="close-modal" data-modal="page-modal">&#215;</button>
    <div class="modal-tag">Page Content</div>
    <div class="modal-name" id="pm-name"></div>
    <div class="modal-section" style="margin-top:16px">
      <div class="modal-section-value" id="pm-content"></div>
    </div>
  </div>
</div>

<script>
var characters = {characters_json};
var pageContent = {page_content_json};
var chapterOverview = {chapter_overview_json};
var chapterNames = {chapter_names_json};
var chapterIndexMap = {chapter_index_json};
var relationshipIndex = {relationship_index_json};
var characterMeta = {character_meta_json};
var characterAnalyses = {character_analyses_json};
var chapterAnalyses = {chapter_analyses_json};
var timelineData = {timeline_json};

// ── Navigation ──
var sections = ['outline', 'focus', 'board', 'plotlines', 'relationships', 'missing'];

function scrollToSection(name) {{
  var sec = document.getElementById('section-' + name);
  if (sec) sec.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  document.querySelectorAll('.nav-item').forEach(function(n) {{
    n.classList.toggle('active', n.dataset.section === name);
  }});
}}

window.addEventListener('scroll', function() {{
  var scrollY = window.scrollY + 80;
  var active = 'outline';
  sections.forEach(function(name) {{
    var sec = document.getElementById('section-' + name);
    if (sec && sec.offsetTop <= scrollY) active = name;
  }});
  document.querySelectorAll('.nav-item').forEach(function(n) {{
    n.classList.toggle('active', n.dataset.section === active);
  }});
}});

// ── Modals ──
function closeModal(id) {{
  var el = document.getElementById(id);
  if (el) el.classList.remove('open');
}}

function openModal(id) {{
  var el = document.getElementById(id);
  if (el) el.classList.add('open');
}}

// ── Chapter focus panel ──
function selectChapter(idx) {{
  var info = chapterOverview[String(idx)];
  if (!info) return;
  scrollToSection('board');
  var panel = document.getElementById('focus-panel');
  var pillsHtml = '';
  if (info.chars && info.chars.length) {{
    pillsHtml = '<div class="cfp-chars">';
    info.chars.forEach(function(name) {{
      pillsHtml += '<button class="char-pill" data-action="show-char" data-name="' + name.replace(/"/g, '&quot;') + '"><span class="pill-dot" style="background:var(--accent)"></span>' + name + '</button>';
    }});
    pillsHtml += '</div>';
  }}
  panel.innerHTML = '<div class="cfp-sin" style="color:' + (info.color || 'var(--accent)') + '">' + (info.sin || '') + '</div>'
    + '<div class="cfp-summary">' + (info.summary || '') + '</div>' + pillsHtml;
  document.querySelectorAll('.ch-card').forEach(function(c) {{ c.classList.remove('focused'); }});
  var card = document.getElementById('chapter-' + idx);
  if (card) card.classList.add('focused');
}}

// ── Show character detail ──
function showCharDetail(name) {{
  var related = characters.filter(function(c) {{ return c.name === name; }});
  if (!related.length) return;
  var char = related[0];
  var arcColors = {{ 'Developed': '#4ade80', 'Partially Developed': '#facc15', 'Undeveloped': '#f87171' }};
  var col = arcColors[char.arc_status] || '#94a3b8';

  document.getElementById('cm-role').textContent = (char.role || '').toUpperCase();
  document.getElementById('cm-name').textContent = char.name || '';
  var chaps = Array.from(new Set(related.map(function(c) {{ return c.chapter; }}).filter(Boolean)));
  document.getElementById('cm-sub').textContent = [(char.sin || ''), chaps.length ? 'Chapter: ' + chaps.join(', ') : ''].filter(Boolean).join(' · ');
  var arcEl = document.getElementById('cm-arc');
  arcEl.textContent = char.arc_status || 'Unknown';
  arcEl.style.background = col + '22';
  arcEl.style.color = col;
  arcEl.style.border = '1px solid ' + col + '44';

  ['cm-verdict-section','cm-emotional-section','cm-sin-section','cm-arc-section',
   'cm-player-section','cm-pivotal-section','cm-rels-section','cm-weakness-section',
   'cm-director-section','cm-nova-loading','cm-no-analysis','cm-backstory-section',
   'cm-note-section'].forEach(function(id) {{
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  }});

  var analysis = characterAnalyses[name];
  if (analysis) {{
    function setField(sectionId, fieldId, value) {{
      if (value) {{
        document.getElementById(sectionId).style.display = 'block';
        document.getElementById(fieldId).textContent = value;
      }}
    }}
    setField('cm-verdict-section', 'cm-verdict', analysis.verdict);
    setField('cm-emotional-section', 'cm-emotional', analysis.emotional_core);
    setField('cm-sin-section', 'cm-sin-emb', analysis.sin_embodiment);
    setField('cm-arc-section', 'cm-arc-analysis', analysis.arc_analysis);
    setField('cm-player-section', 'cm-player', analysis.player_connection);
    setField('cm-pivotal-section', 'cm-pivotal', analysis.pivotal_moment);
    setField('cm-rels-section', 'cm-rels', analysis.relationship_web);
    setField('cm-weakness-section', 'cm-weakness', analysis.weakness);
    setField('cm-director-section', 'cm-director', analysis.director_note);
  }} else {{
    document.getElementById('cm-no-analysis').style.display = 'block';
    document.getElementById('cm-note-section').style.display = 'block';
    var backstory = related.map(function(c) {{ return c.backstory_summary || ''; }}).filter(Boolean).join(' ').trim();
    if (backstory) {{
      document.getElementById('cm-backstory-section').style.display = 'block';
      document.getElementById('cm-backstory').textContent = backstory;
    }}
    var note = related.map(function(c) {{ return c.emotional_note || ''; }}).filter(Boolean).join(' ').trim();
    document.getElementById('cm-note').textContent = note || 'No analysis available.';
  }}
  openModal('char-modal');
}}

// ── Show chapter detail ──
function showChapterDetail(idx) {{
  var info = chapterOverview[String(idx)];
  if (!info) return;
  var name = chapterNames[idx] || ('Chapter ' + idx);
  document.getElementById('chm-sin').textContent = (info.sin || '').toUpperCase();
  document.getElementById('chm-name').textContent = name;
  document.getElementById('chm-sub').textContent = (info.chars && info.chars.length)
    ? info.chars.slice(0, 5).join(' · ') : 'No characters mapped';

  ['chm-verdict-section','chm-sin-section','chm-peak-section','chm-pacing-section',
   'chm-chars-section','chm-strength-section','chm-gap-section','chm-director-section',
   'chm-nova-loading','chm-no-analysis'].forEach(function(id) {{
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  }});

  var analysis = chapterAnalyses[name];
  if (analysis) {{
    function setCh(sectionId, fieldId, value) {{
      if (value) {{
        document.getElementById(sectionId).style.display = 'block';
        document.getElementById(fieldId).textContent = value;
      }}
    }}
    setCh('chm-verdict-section', 'chm-verdict', analysis.verdict);
    setCh('chm-sin-section', 'chm-sin-theme', analysis.sin_theme);
    setCh('chm-peak-section', 'chm-peak', analysis.emotional_peak);
    setCh('chm-pacing-section', 'chm-pacing', analysis.pacing);
    setCh('chm-chars-section', 'chm-chars-dyn', analysis.character_dynamics);
    setCh('chm-strength-section', 'chm-strength', analysis.strongest_element);
    setCh('chm-gap-section', 'chm-gap', analysis.critical_gap);
    setCh('chm-director-section', 'chm-director', analysis.director_note);
  }} else {{
    document.getElementById('chm-no-analysis').style.display = 'block';
    if (info.summary) {{
      document.getElementById('chm-verdict-section').style.display = 'block';
      document.getElementById('chm-verdict').textContent = info.summary;
    }}
  }}
  openModal('chapter-modal');
  document.querySelectorAll('.ch-card').forEach(function(c) {{ c.classList.remove('focused'); }});
  var card = document.getElementById('chapter-' + idx);
  if (card) card.classList.add('focused');
}}

// ── Show page content ──
function showPageContent(name) {{
  var content = pageContent[name];
  document.getElementById('pm-name').textContent = name;
  document.getElementById('pm-content').textContent = (content && content.trim()) ? content : 'No content written in this page yet.';
  openModal('page-modal');
}}

// ── Relationship explorer ──
function showRelationshipCharacter(name) {{
  var infoBox = document.getElementById('rel-info-box');
  if (!infoBox) return;

  document.querySelectorAll('.rel-char-item').forEach(function(btn) {{
    btn.classList.toggle('active', btn.dataset.name === name);
  }});

  var meta = characterMeta[name] || {{}};
  var roles = (meta.roles && meta.roles.length) ? meta.roles.join(', ') : 'Unknown';
  var chapters = (meta.chapters && meta.chapters.length) ? meta.chapters.join(', ') : 'Unassigned';
  var statuses = (meta.statuses && meta.statuses.length) ? meta.statuses.join(', ') : 'Unknown';
  var rels = relationshipIndex[name] || [];
  var analysis = characterAnalyses[name] || {{}};
  var reasoning = analysis.relationship_web || 'No relationship reasoning generated yet for this character.';

  var linksHtml = '';
  if (!rels.length) {{
    linksHtml = '<div class="rel-info-empty">No direct links mapped for this character.</div>';
  }} else {{
    linksHtml = '<div class="rel-links">' + rels.map(function(rel) {{
      var directionLabel = rel.direction === 'outgoing' ? 'OUTGOING TO' : 'INCOMING FROM';
      var other = rel.other || 'Unknown';
      var detail = rel.reasoning || rel.relationship || 'No relationship details available.';
      var safeOther = other.replace(/"/g, '&quot;');
      return '<div class="rel-link-card">'
        + '<div class="rel-link-top"><span class="rel-dir">' + directionLabel + '</span>'
        + '<span class="rel-other" data-action="show-char" data-name="' + safeOther + '">' + other + '</span></div>'
        + '<div class="rel-link-body">' + detail + '</div>'
        + '</div>';
    }}).join('') + '</div>';
  }}

  infoBox.innerHTML = ''
    + '<div class="rel-info-head">'
    + '<div class="rel-info-name">' + name + '</div>'
    + '<div class="rel-info-meta">Role: ' + roles + '<br/>Chapter: ' + chapters + '<br/>Arc Status: ' + statuses + '</div>'
    + '</div>'
    + '<div class="rel-reasoning"><div class="rel-reasoning-label">Narrative Reasoning</div><div class="rel-reasoning-text">' + reasoning + '</div></div>'
    + linksHtml;
}}

// ── Search ──
function doSearch(raw) {{
  var q = (raw || '').trim().toLowerCase();
  var dd = document.getElementById('search-dropdown');
  if (!q) {{ dd.style.display = 'none'; dd.innerHTML = ''; return; }}
  var results = [], seen = {{}};
  function push(type, label, meta, key) {{
    var k = type + ':' + label;
    if (seen[k]) return;
    seen[k] = true;
    results.push({{ type: type, label: label, meta: meta, key: key }});
  }}
  characters.forEach(function(c) {{
    if (((c.name||'')+' '+(c.role||'')+' '+(c.chapter||'')).toLowerCase().indexOf(q) !== -1)
      push('character', c.name||'', (c.role||'')+(c.chapter?' · '+c.chapter:''), c.name||'');
  }});
  chapterNames.forEach(function(ch) {{
    if (ch.toLowerCase().indexOf(q) !== -1)
      push('chapter', ch, 'Chapter', chapterIndexMap[ch]||'0');
  }});
  Object.keys(pageContent).forEach(function(name) {{
    if (name.toLowerCase().indexOf(q) !== -1)
      push('page', name, 'Doc', name);
  }});
  results = results.slice(0, 18);
  if (!results.length) {{ dd.style.display = 'none'; return; }}
  dd.innerHTML = results.map(function(r) {{
    var safeKey = r.key.replace(/"/g, '&quot;');
    return '<div class="search-item" data-action="search-pick" data-type="' + r.type + '" data-key="' + safeKey + '">'
      + '<div class="search-item-label">' + r.label + '</div>'
      + '<div class="search-item-meta">' + r.meta + '</div></div>';
  }}).join('');
  dd.style.display = 'block';
}}

// ── Render Master Timeline ──
(function renderTimeline() {{
  var container = document.getElementById('tl-master');
  if (!container) return;
  var events = (timelineData && timelineData.timeline) || [];
  if (!events.length) {{
    container.innerHTML = '<div style="color:var(--dim);font-style:italic;padding:20px 0">No timeline generated — re-run to produce timeline analysis.</div>';
    return;
  }}

  var typeClass = {{ 'main': 'tl-ev-main', 'sideplot': 'tl-ev-sideplot', 'trauma': 'tl-ev-trauma', 'flashback': 'tl-ev-flashback', 'offscreen': 'tl-ev-offscreen' }};
  var typeLabel = {{ 'main': 'MAIN', 'sideplot': 'SIDE', 'trauma': 'TRAUMA', 'flashback': 'FLASH', 'offscreen': 'OFF' }};
  var typeBadge = {{ 'main': 'tl-main', 'sideplot': 'tl-sideplot', 'trauma': 'tl-trauma', 'flashback': 'tl-flashback', 'offscreen': 'tl-offscreen' }};
  var weightClass = {{ 'critical': 'tl-ev-critical', 'high': 'tl-ev-high', 'medium': '', 'low': '' }};

  var html = '';
  var lastEra = null;

  events.forEach(function(ev) {{
    var era = ev.era || 'present';
    if (era !== lastEra) {{
      var eraLabel = era === 'past' ? '◀ BEFORE THE STORY' : '▶ PRESENT — MAIN STORY';
      var eraColor = era === 'past' ? 'var(--dim)' : 'var(--accent)';
      html += '<div class="tl-era-divider"><span class="tl-era-label" style="color:' + eraColor + '">' + eraLabel + '</span><div class="tl-era-line"></div></div>';
      lastEra = era;
    }}

    var tc = typeClass[ev.type] || 'tl-ev-main';
    var wc = weightClass[ev.emotional_weight] || '';
    var tb = typeBadge[ev.type] || 'tl-main';
    var tl = typeLabel[ev.type] || 'MAIN';

    var charsHtml = '';
    if (ev.characters && ev.characters.length) {{
      charsHtml = '<div class="tl-event-chars">';
      ev.characters.forEach(function(name) {{
        charsHtml += '<span class="tl-char-tag" data-action="show-char" data-name="' + name.replace(/"/g, '&quot;') + '">' + name + '</span>';
      }});
      charsHtml += '</div>';
    }}

    var chapterSafe = (ev.chapter || '').replace(/"/g, '&quot;');
    var chapterIdx = chapterIndexMap[ev.chapter];
    var chapterAttr = (chapterIdx !== undefined) ? ' data-action="show-chapter" data-idx="' + chapterIdx + '"' : '';

    html += '<div class="tl-event ' + tc + ' ' + wc + '"' + chapterAttr + '>'
      + '<div class="tl-event-header">'
      + '<div class="tl-event-title">' + (ev.title || '') + '</div>'
      + '<div class="tl-event-badges">'
      + '<span class="tl-type-badge ' + tb + '">' + tl + '</span>'
      + (ev.chapter ? '<span class="tl-chapter-badge">' + ev.chapter + '</span>' : '')
      + '</div></div>'
      + '<div class="tl-event-desc">' + (ev.description || '') + '</div>'
      + charsHtml
      + '</div>';
  }});

  container.innerHTML = html;
  document.getElementById('tl-loading') && document.getElementById('tl-loading').remove();
}})();

(function initRelationshipExplorer() {{
  var first = document.querySelector('.rel-char-item');
  if (first) showRelationshipCharacter(first.dataset.name);
}})();

// ── Single delegated click handler ──
document.addEventListener('click', function(e) {{
  var el = e.target.closest('[data-action]');
  var dd = document.getElementById('search-dropdown');
  var inp = document.getElementById('search-input');

  // Close search dropdown on outside click
  if (inp && dd && !inp.contains(e.target) && !dd.contains(e.target)) {{
    dd.style.display = 'none';
  }}

  if (!el) return;
  var action = el.dataset.action;

  if (action === 'show-char') {{
    e.stopPropagation();
    showCharDetail(el.dataset.name);
  }} else if (action === 'show-chapter') {{
    showChapterDetail(parseInt(el.dataset.idx, 10));
  }} else if (action === 'show-page') {{
    e.stopPropagation();
    showPageContent(el.dataset.name);
  }} else if (action === 'select-chapter') {{
    selectChapter(parseInt(el.dataset.idx, 10));
  }} else if (action === 'rel-char-select') {{
    showRelationshipCharacter(el.dataset.name);
  }} else if (action === 'close-modal') {{
    // Only close if clicking the backdrop itself (not content inside) or the close button
    var isBackdrop = el.classList.contains('modal-backdrop');
    var isCloseBtn = el.classList.contains('modal-close');
    if (isBackdrop && e.target !== el) return;
    closeModal(el.dataset.modal);
  }} else if (action === 'search-pick') {{
    dd.style.display = 'none';
    inp.value = '';
    var type = el.dataset.type, key = el.dataset.key;
    if (type === 'character') showCharDetail(key);
    else if (type === 'chapter') {{ selectChapter(parseInt(key, 10)); }}
    else if (type === 'page') showPageContent(key);
  }} else if (el.dataset.section) {{
    scrollToSection(el.dataset.section);
  }}
}});

document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') {{
    document.querySelectorAll('.modal-backdrop.open').forEach(function(m) {{ m.classList.remove('open'); }});
    document.getElementById('search-dropdown').style.display = 'none';
  }}
}});
</script>
</body>
</html>"""
    return html


def run():
    print("\n7 SINS - STUDIO AGENT")
    print("=" * 50)

    for name in os.listdir("."):
        if name.startswith("storyboard_") and name.endswith(".html"):
            try:
                os.remove(name)
            except OSError:
                pass

    print("Reading Story Planner...")
    story_entries = get_story_entries()
    print(f"Total: {len(story_entries)} entries.")

    print("\nBuilding story map...")
    story_text, chapters = build_story_prompt(story_entries)
    overview_brief = build_overview_brief(story_entries)

    print("Sending to Amazon Nova for story analysis...")
    data = ask_nova_story(story_text, overview_brief)
    print("Running second-pass character recovery...")
    roster_recovery = ask_nova_character_roster(overview_brief, story_text, data.get("characters") or [])
    if roster_recovery:
        extra_chars = [c for c in (roster_recovery.get("additional_characters") or []) if isinstance(c, dict) and c.get("name")]
        if extra_chars:
            data["characters"] = (data.get("characters") or []) + extra_chars
            print(f"  Added {len(extra_chars)} extra characters from second pass.")
        extra_aliases = [a for a in (roster_recovery.get("character_aliases") or []) if isinstance(a, dict)]
        if extra_aliases:
            data["character_aliases"] = (data.get("character_aliases") or []) + extra_aliases

    # Build page content map for character analysis
    page_summaries_map = {ps.get("page"): ps.get("summary", "") for ps in (data.get("page_summaries") or []) if ps.get("page")}
    page_content_map = {}
    for e in story_entries:
        n = e.get("name")
        if n:
            page_content_map[n] = (page_summaries_map.get(n) or e.get("content") or "").strip()

    # Build character list (same logic as generate_html)
    sin_names = {"wrath", "pride", "envy", "greed", "lust", "gluttony", "sloth"}
    chapter_words = {"overview", "unassigned", "chapter", "prologue", "epilogue"}

    def is_invalid_character_name(name):
        if not name or not name.strip():
            return True
        n = name.lower().strip()
        if n in sin_names:
            return True
        for sin in sin_names:
            if n == sin or n.endswith("- " + sin) or n.startswith(sin + " "):
                return True
        for word in chapter_words:
            if n == word or n.startswith(word + " ") or n.endswith(" " + word):
                return True
        if len(name.strip()) <= 2:
            return True
        if not any(c.isalpha() for c in name):
            return True
        return False

    characters_list = [
        c for c in (data.get("characters") or [])
        if isinstance(c, dict) and not is_invalid_character_name(c.get("name", ""))
    ]
    existing_keys = {(c.get("name", "").lower(), c.get("chapter", "").lower()) for c in characters_list}
    for e in story_entries:
        cats = e.get("category") or []
        doc_name = e.get("name") or ""
        cats_lower = [c.lower() for c in cats]
        is_char = any(kw in c for c in cats_lower for kw in ("character", "boss", "ally", "npc", "design", "idol", "villain", "hero", "protagonist", "antagonist"))
        if not is_char or not doc_name:
            continue
        base_name = doc_name.split(":", 1)[0].split(" - ", 1)[0].strip()
        if is_invalid_character_name(base_name):
            continue
        ch = e.get("chapter") or "Unassigned"
        if (base_name.lower(), ch.lower()) in existing_keys:
            continue
        role = "NPC"
        if any("boss" in c for c in cats_lower): role = "Boss"
        elif any("ally" in c or "hero" in c or "protagonist" in c for c in cats_lower): role = "Ally"
        characters_list.append({"name": base_name, "role": role, "chapter": ch, "sin": "", "emotional_note": "", "backstory_summary": (e.get("content") or "")[:500], "arc_status": "Undeveloped"})
        existing_keys.add((base_name.lower(), ch.lower()))

    raw_connections = data.get("story_connections") or []
    ai_alias_map = _build_ai_alias_map(data.get("character_aliases") or [])
    alias_seed_names = [c.get("name", "") for c in characters_list]
    for conn in raw_connections:
        alias_seed_names.append(conn.get("from", ""))
        alias_seed_names.append(conn.get("to", ""))
    alias_map = _build_name_alias_map(alias_seed_names)
    alias_map.update(ai_alias_map)

    for c in characters_list:
        c["name"] = _canonicalize_name(c.get("name", ""), alias_map)

    canonical_connections = []
    for conn in raw_connections:
        from_name = _canonicalize_name(conn.get("from", ""), alias_map)
        to_name = _canonicalize_name(conn.get("to", ""), alias_map)
        rel_text = (conn.get("relationship") or "").strip()
        if not from_name and not to_name and not rel_text:
            continue
        canonical_connections.append({"from": from_name, "to": to_name, "relationship": rel_text})
    data["story_connections"] = canonical_connections

    print("Generating unnamed protagonist analysis from all pages...")
    unnamed_protagonist_analysis = ask_nova_unnamed_protagonist(
        overview_brief,
        story_text,
        characters_list,
        canonical_connections
    )

    # Group characters by name
    char_groups = {}
    for c in characters_list:
        char_groups.setdefault(c["name"], []).append(c)

    story_snippet = story_text

    print(f"\nGenerating Nova character analyses for {len(char_groups)} characters...")
    character_analyses = {}
    for i, (char_name, char_data_list) in enumerate(sorted(char_groups.items()), 1):
        print(f"  [{i}/{len(char_groups)}] Analysing: {char_name}")
        result = ask_nova_character(char_name, char_data_list, page_content_map, story_snippet)
        if result:
            character_analyses[char_name] = result

    sorted_chapters = sorted(ch for ch in chapters.keys() if ch.lower() not in ("overview", "unassigned"))
    print(f"\nGenerating Nova chapter analyses for {len(sorted_chapters)} chapters...")
    chapter_analyses = {}
    for i, ch in enumerate(sorted_chapters, 1):
        print(f"  [{i}/{len(sorted_chapters)}] Analysing: {ch}")
        entries = chapters[ch]
        chars_in_ch = [c for c in characters_list if (c.get("chapter") or "").lower() == ch.lower() or ch.lower() in (c.get("chapter") or "").lower()]
        result = ask_nova_chapter(ch, entries, chars_in_ch, story_snippet)
        if result:
            chapter_analyses[ch] = result

    print("\nGenerating Nova master timeline...")
    timeline_data = ask_nova_timeline(story_text, story_entries)
    if timeline_data:
        evt_count = len(timeline_data.get("timeline") or [])
        print(f"  {evt_count} timeline events generated.")
    else:
        print("  Timeline generation failed — will show empty timeline.")

    print("\nGenerating interactive storyboard...")
    html = generate_html(
        data,
        story_entries,
        chapters,
        character_analyses,
        chapter_analyses,
        timeline_data,
        unnamed_protagonist_analysis
    )

    filename = "index.html"

    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nStoryboard saved to: {filename}")
    print("Opening in browser...")
    webbrowser.open(f"file:///{os.path.abspath(filename)}")
    print("\nDone.")


if __name__ == "__main__":
    run()
