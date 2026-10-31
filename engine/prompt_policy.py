"""Prompt boundaries and style decisions shared by the two resolvers."""
from __future__ import annotations

import re
import unicodedata

_WEIGHT = re.compile(r"-?\d+(?:\.\d+)?\s*::")
_WEIGHT_GROUP = re.compile(r"-?\d+(?:\.\d+)?\s*::[\s\S]*?::")
_SPAN = re.compile(
    r"artist:[A-Za-z0-9_./&~+'()\-]+|"
    r"[\w'.:\-]+_\([^()]+\)|"
    r"[\w]+(?:_[\w]+)+|\d+(?:girls?|boys?)|[A-Za-z][A-Za-z0-9'.\-]*"
)
_UNICODE_TAG = re.compile(r"(?=[\w'.:-]*[^\x00-\x7f])[\w'.:-]+")


def needs_translation(text: str) -> bool:
    value = str(text or "")
    latin_positions = []
    for index, char in enumerate(value):
        if ord(char) > 127 and char.isalpha():
            # Accented Latin spelling is common in explicit character tags.
            if "LATIN" not in unicodedata.name(char, ""):
                return True
            latin_positions.append(index)
    if not latin_positions:
        return False
    structured = []
    structured.extend((match.start(), match.end()) for match in _WEIGHT_GROUP.finditer(value))
    structured.extend((match.start(), match.end()) for match in re.finditer(r"[\w'.:-]+\s*\([^()]+\)", value))
    if all(any(start <= index < end for start, end in structured) for index in latin_positions):
        return False
    # Exempt conventional weighted tags and parenthesized character/work names.
    residue = _WEIGHT_GROUP.sub(" ", value)
    residue = re.sub(r"[\w'.:-]+\s*\([^()]+\)", " ", residue)
    # Multiword accented text is natural language, not a single tagged name.
    return len(re.findall(r"[^\W_][\w'-]*", residue, re.UNICODE)) > 1


def split_tags(text: str) -> list[str]:
    pieces, stack = [], []
    start = index = weight = 0
    while index < len(text):
        opening = _WEIGHT.match(text, index)
        if opening:
            weight += 1
            index = opening.end()
            continue
        if weight and text.startswith("::", index):
            weight -= 1
            index += 2
            continue
        ch = text[index]
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}" and stack:
            stack.pop()
        if ch in ",，" and not stack and not weight:
            if text[start:index].strip():
                pieces.append(text[start:index].strip())
            start = index + 1
        index += 1
    if text[start:].strip():
        pieces.append(text[start:].strip())
    return pieces


def merge_tags(*texts: str) -> str:
    result, seen = [], set()
    for text in texts:
        for piece in split_tags(str(text or "")):
            key = piece.lower()
            if key not in seen:
                seen.add(key)
                result.append(piece)
    return ", ".join(result)


def _group_spans(text: str) -> list[tuple[int, int]]:
    """Find complete emphasis groups without losing nested numeric weights."""
    stack, spans = [], []
    index = 0
    pairs = {"}": "{", "]": "["}
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        opening = _WEIGHT.match(text, index)
        if opening:
            stack.append(("::", index))
            index = opening.end()
            continue
        if text.startswith("::", index) and stack and stack[-1][0] == "::":
            _, start = stack.pop()
            spans.append((start, index + 2))
            index += 2
            continue
        char = text[index]
        if char in "{[":
            stack.append((char, index))
        elif char in pairs and stack and stack[-1][0] == pairs[char]:
            _, start = stack.pop()
            spans.append((start, index + 1))
        index += 1
    return spans


def protected_spans(text: str) -> list[str]:
    """Keep explicit syntax without guessing how to translate adjacent language."""
    result = []
    for piece in split_tags(text):
        if not needs_translation(piece):
            result.append(piece)
            continue
        spans = [(start, end) for start, end in _group_spans(piece)
                 if not needs_translation(piece[start:end])]
        if any(ord(ch) > 127 and ch.isalpha() and "LATIN" not in unicodedata.name(ch, "")
               for ch in piece):
            for match in _UNICODE_TAG.finditer(piece):
                if not needs_translation(match.group()):
                    spans.append((match.start(), match.end()))
        for match in _SPAN.finditer(piece):
            if needs_translation(match.group()):
                continue
            spans.append((match.start(), match.end()))
        # Preserve the largest exact syntax span, never its unweighted duplicate.
        spans = sorted(set(spans), key=lambda span: (span[0], -span[1]))
        covered = -1
        for start, end in spans:
            if end <= covered:
                continue
            covered = end
            if re.search(r"(?:不要|不是|不许|排除|避免|别画|without|not|no)\s*$",
                         piece[:start], re.I):
                continue
            result.append(piece[start:end])
    return list(dict.fromkeys(result))


def explicit_style(text: str, source: str = "") -> bool:
    # A model can identify an uncommon style, but must quote the user's words.
    if source and source in text:
        return True
    return bool(re.search(
        r"artist:|\b(?:3d|2d|realistic|photorealistic|watercolor|oil_painting|pixel_art)\b|"
        r"画风|风格|写实|厚涂|水彩|油画|像素|水墨|素描|三维|三渲二|赛璐璐",
        text, re.I))


def _atom_key(text: str) -> str:
    return re.sub(r"[ _]+", " ", text.strip().casefold())


def remove_cross_owned_features(data: dict, trace: list, fixed: list[str], text: str) -> dict:
    """Drop reference-backed physical tags placed on another role during outfit swaps."""
    if not re.search(r"换装|服装交换|交换服装|交换衣服|换衣|clothes[_ ]swap|clothing[_ ]swap|outfit swap|costume swap", text, re.I):
        return data
    # An explicit hair/body swap is a separate user instruction, not outfit transfer.
    if re.search(r"换发型|交换发型|发型互换|换发色|交换发色|发色互换|互换头发|交换头发|swap hair|hair swap|swap hairstyle|hairstyle swap|change hair color", text, re.I):
        return data
    roles = data.get("multi_role_list")
    if not isinstance(roles, list) or len(roles) < 2:
        return data

    descriptions = {}
    for event in trace:
        for candidate in event.get("candidates", []):
            if candidate.get("source") != "local_description":
                continue
            atoms = split_tags(str(candidate.get("desc", "")))
            identity = str(candidate.get("matched_tag") or (atoms[0] if atoms else "")).casefold()
            if identity:
                descriptions[identity] = {
                    _atom_key(atom) for atom in atoms[1:]
                    if re.search(r"(?:hair|ponytail|eyes?|skin|face|horns?|tails?|ears?)", atom, re.I)
                }
    if not descriptions:
        return data

    role_ids = []
    for role in roles:
        names = {atom.casefold() for atom in split_tags(str(role.get("prompt", "")))}
        role_ids.append(names & descriptions.keys())
    known_owners = set().union(*role_ids)
    protected = {_atom_key(atom) for span in fixed for atom in split_tags(span)}
    cleaned = []
    changed = False
    for index, role in enumerate(roles):
        owned = set().union(*(descriptions.get(name, set()) for name in role_ids[index]))
        foreign = set().union(*(features for owner, features in descriptions.items() if owner in known_owners
                                and owner not in role_ids[index]))
        atoms = split_tags(str(role.get("prompt", "")))
        kept = [atom for atom in atoms if not (
            _atom_key(atom) in foreign and _atom_key(atom) not in owned
            and _atom_key(atom) not in protected
        )]
        changed |= len(kept) != len(atoms)
        cleaned.append({**role, "prompt": ", ".join(kept)})
    return {**data, "multi_role_list": cleaned} if changed else data


def _unweighted(text: str) -> str:
    for _ in range(16):
        group = re.fullmatch(r"-?\d+(?:\.\d+)?\s*::(.*?)::", text)
        if group:
            text = group[1].strip()
        elif len(text) > 1 and (text[0], text[-1]) in {("{", "}"), ("[", "]")}:
            inner = text[1:-1].strip()
            # Only unwrap a group covering the entire atom, not adjacent groups.
            depth = 0
            for char in text[:-1]:
                depth += (char == text[0]) - (char == text[-1])
                if depth == 0:
                    return text
            text = inner
        else:
            break
    return text


def _artist_keys(text: str) -> set[str]:
    atoms = [_unweighted(atom) for atom in split_tags(_unweighted(text))]
    if atoms and all(re.fullmatch(r"artist:[A-Za-z0-9_./&~+'()\-]+", atom, re.I) for atom in atoms):
        return {_atom_key(atom[7:]) for atom in atoms}
    return set()


def reconcile_generated_tags(data: dict, fixed: list[str]) -> dict:
    """Remove only provable model duplicates, preserving explicit user syntax."""
    roles = data.get("multi_role_list", [])
    if (not isinstance(data.get("tag"), str) or not isinstance(data.get("artist", ""), str)
            or not isinstance(roles, list) or any(not isinstance(r, dict) for r in roles)):
        return data
    result = {**data, "multi_role_list": [dict(r) for r in roles]}
    originals, artist_keys = [], set()
    for span in fixed:
        keys = _artist_keys(span)
        if keys:
            originals.append(span)
            artist_keys.update(keys)
    if originals:
        def clean(value, *, artist_field=False):
            if not isinstance(value, str):
                return value
            kept = []
            for atom in split_tags(value):
                # An independently specified bare name remains the user's tag.
                if atom in fixed and atom not in originals:
                    kept.append(atom)
                    continue
                keys = _artist_keys(atom)
                bare = _atom_key(_unweighted(atom))
                if atom in originals or (keys and keys <= artist_keys) or (artist_field and bare in artist_keys):
                    continue
                kept.append(atom)
            return ", ".join(kept)
        result["tag"] = clean(result["tag"])
        result["artist"] = merge_tags(*originals, clean(result.get("artist", ""), artist_field=True))

    if len(roles) > 1:
        positive = [{_atom_key(p) for p in split_tags(str(r.get("prompt", "")))} for r in roles]
        negative = [{_atom_key(p) for p in split_tags(str(r.get("uc", "")))} for r in roles]
        protected = {_atom_key(s) for s in fixed}
        kept = []
        for atom in split_tags(result["tag"]):
            key = _atom_key(atom)
            conflict = any(key in pos and key in neg for i, pos in enumerate(positive)
                           for j, neg in enumerate(negative) if i != j)
            # Keep weighted/grouped atoms and anything explicitly provided by the user.
            if key in protected or re.search(r"::|[{}\[\]]", atom) or not conflict:
                kept.append(atom)
        # Do not turn a valid result into a rejected empty scene prompt.
        if kept:
            result["tag"] = ", ".join(kept)
    if not result["tag"].strip():
        result["tag"] = data["tag"]
    return result
