"""
Generate hooks, titles, descriptions and hashtags.

Deliberately template-driven rather than LLM-driven: no API key, no cost,
no rate limit, and it runs in 2ms on a free cloud box. The hook templates
below are the ones that actually carry gaming clips — curiosity gap or
stakes, never a summary of what happens.
"""
import random
import re

HOOKS = [
    "Nobody noticed this in {place}",
    "He found this on day one",
    "This should not be possible in {game}",
    "Wait for it…",
    "{streamer} did not expect this",
    "The detail everyone missed",
    "This is why {game} took 12 years",
    "Day one and it's already broken",
    "I replayed this 10 times",
    "{streamer} found it first",
]

# When chat exploded, say so — "chat lost it" is the hook viewers trust.
CHAT_HOOKS = [
    "Chat lost it at this",
    "Chat went crazy for this",
    "{streamer}'s chat could not believe it",
    "The moment chat exploded",
]

TITLE_TEMPLATES = [
    "{hook} 😳 #Shorts",
    "{hook} | GTA 6 #Shorts",
    "GTA 6: {hook} #Shorts",
]

BASE_TAGS = [
    "gta6", "gta vi", "grand theft auto 6", "gta 6 gameplay",
    "vice city", "leonida", "jason and lucia", "gta 6 clips",
    "shorts", "gaming",
]

HANDLE = "@gtavicecityclipzz"

HASHTAGS = {
    "youtube": "#GTA6 #GrandTheftAuto6 #ViceCity #Shorts #Gaming",
    "tiktok": "#gta6 #gta6gameplay #vicecity #fyp #gaming #grandtheftauto6",
    "instagram": "#gta6 #grandtheftauto6 #vicecity #gaming #reels #gta6gameplay",
}


def clean(text):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    text = re.sub(r"[|•]+", "-", text)
    return text


def make_hook(clip, seed=None, templates=None):
    """Campaign sources pass their own neutral hooks (briefs ban fake context)."""
    rng = random.Random(seed or clip.get("clip_id"))
    if templates:
        return rng.choice(templates)
    pool = CHAT_HOOKS if (clip.get("chat_ratio") or 0) >= 6 else HOOKS
    t = rng.choice(pool)
    return t.format(
        place="Vice City",
        game="GTA 6",
        streamer=clip.get("streamer") or "He",
    )


def build(clip, platform, credit_line, hook=None, extra_tags=None):
    hook = hook or make_hook(clip)
    rng = random.Random(clip.get("clip_id"))

    title = rng.choice(TITLE_TEMPLATES).format(hook=hook)
    if platform != "youtube":
        title = title.replace(" #Shorts", "")

    # Credit is not optional — it is half of why streamers say yes, and it is
    # what makes the clip free marketing for them rather than theft.
    tags_line = HASHTAGS[platform]
    if extra_tags:                       # campaign-required tags go first
        have = tags_line.lower().split()
        tags_line = " ".join([t for t in extra_tags if t.lower() not in have]
                             + [tags_line])
    desc_parts = [hook, "", credit_line, f"Daily GTA 6 clips: follow {HANDLE}",
                  "", tags_line]
    description = "\n".join(desc_parts)

    tags = BASE_TAGS + [
        t for t in re.findall(r"[a-z0-9]{4,}", clean(clip.get("title", "")).lower())
    ][:30]

    return {
        "hook": hook,
        "title": title[:100],
        "description": description,
        "tags": tags,
        "caption": f"{hook}\n\n{credit_line}\n\n{tags_line}",
    }
