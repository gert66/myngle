"""Deterministic Dutch (NL) content localization for the Lovable JSON export demo.

Every caller-facing text field this module touches is produced upstream by a
small, fixed set of English sentence templates (see
``lead_caller_app_fields_builder.py`` and ``lead_app_summary_builder.py``).
Rather than patching English fragments inside those strings — which produces
mixed-language output such as "The hoofdkantoor evidence source..." — each
function here matches the *whole* known template structurally and rebuilds
the complete sentence from a Dutch template, reusing only the variable slots
(company name, country, parent company, ...) that were already in the data.

Text that doesn't match any known template (custom notes, external source
snippets/quotes, free-form evidence text) is returned unchanged in English:
this is a small demo, never a guessed/free translation, and never an AI call.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Known display-label translations.
# ---------------------------------------------------------------------------

# visible_icp_signal_scores[].label / SIGNAL_DISPLAY_LABELS values.
LABEL_TRANSLATIONS_NL: dict[str, str] = {
    "Foreign ownership or group structure":
        "Buitenlands hoofdkantoor of groepsstructuur",
    "International business context": "Internationale bedrijfscontext",
    "L&D or onboarding signal": "L&D- of onboarding-signaal",
    "Possible onboarding need": "Mogelijke onboardingbehoefte",
    "Explicit learning and development signal":
        "Expliciet learning & development-signaal",
    "Employer branding or employee satisfaction":
        "Employer branding of medewerkerstevredenheid",
    "Multicultural or international workforce":
        "Multicultureel of internationaal personeelsbestand",
    "Rapid growth signal": "Signaal van snelle groei",
    "Merger or acquisition signal": "Fusie- of overnamesignaal",
}

# lead_app_summary_builder._SIGNAL_LABELS values, for evidence_summary_app lines.
SIGNAL_SUMMARY_LABEL_TRANSLATIONS_NL: dict[str, str] = {
    "International profile": "Internationaal profiel",
    "Onboarding / training need": "Onboarding-/trainingsbehoefte",
    "Company size / complexity": "Bedrijfsomvang / complexiteit",
    "ICP keyword match": "ICP-trefwoordovereenkomst",
    "Employer branding": "Employer branding",
}

_CONFIDENCE_NL = {"High": "Hoge", "Medium": "Gemiddelde", "Low": "Lage"}


def translate_known_label(label) -> object:
    """Translate a known display label; unknown/blank labels pass through."""
    if not label:
        return label
    return LABEL_TRANSLATIONS_NL.get(label, label)


# ---------------------------------------------------------------------------
# Small variable-slot renderers shared across templates.
# ---------------------------------------------------------------------------

def _country_adj_nl(country_adj: str) -> str:
    """'{country}-based' -> 'in {country} gevestigde'; 'local' -> 'lokale'."""
    if country_adj == "local":
        return "lokale"
    m = re.fullmatch(r"(.+)-based", country_adj)
    if m:
        return f"in {m.group(1)} gevestigde"
    return country_adj


def _team_phrase_nl(team_phrase: str) -> str:
    """'the {country} team' -> 'het {country}-team'; 'the local team' -> 'het lokale team'."""
    if team_phrase == "the local team":
        return "het lokale team"
    m = re.fullmatch(r"the (.+) team", team_phrase)
    if m:
        return f"het {m.group(1)}-team"
    return team_phrase


def _where_nl(where: str) -> str:
    """'locally' -> 'lokaal'; 'in {country}' -> 'in {country}'."""
    if where == "locally":
        return "lokaal"
    m = re.fullmatch(r"in (.+)", where)
    if m:
        return f"in {m.group(1)}"
    return where


def _context_nl(context: str) -> str:
    """Foreign-HQ context phrase, e.g. 'a foreign parent or HQ context in {x}'."""
    if context == "a foreign parent or HQ context":
        return "een buitenlandse moeder- of hoofdkantoorcontext"
    m = re.fullmatch(r"a foreign parent or HQ context in (.+)", context)
    if m:
        return f"een buitenlandse moeder- of hoofdkantoorcontext in {m.group(1)}"
    return context


def _country_or_fallback_nl(country: str) -> str:
    if country == "the input country":
        return "het invoerland"
    return country


# ---------------------------------------------------------------------------
# why_relevant_app (lead_caller_app_fields_builder._why_relevant_app)
# ---------------------------------------------------------------------------

_WHY_RELEVANT_RULES: list[tuple[re.Pattern, "callable"]] = [
    (re.compile(
        r"^(?P<company>.+?) is relevant because it combines a foreign-parent or "
        r"international group signal with evidence of international operations, "
        r"onboarding, training, or company complexity\. That makes it a practical "
        r"target for a first conversation about language, communication, or "
        r"training support for (?P<country_adj>.+?) teams\.$"),
     lambda g: (
        f"{g['company']} is relevant omdat het een signaal van een buitenlandse "
        "moeder- of hoofdkantoororganisatie combineert met bewijs van "
        "internationale activiteiten, onboarding, training of bedrijfscomplexiteit. "
        "Dat maakt het een praktisch aanknopingspunt voor een eerste gesprek over "
        f"taal, communicatie of trainingsondersteuning voor "
        f"{_country_adj_nl(g['country_adj'])} teams.")),
    (re.compile(
        r"^(?P<company>.+?) is relevant because it shows a foreign-parent or HQ "
        r"context outside (?P<country>.+?)\. That alone is a practical reason to "
        r"open a conversation about how the local team stays aligned with the "
        r"wider group\.$"),
     lambda g: (
        f"{g['company']} is relevant omdat het een buitenlandse moeder- of "
        f"hoofdkantoorcontext buiten {_country_or_fallback_nl(g['country'])} laat "
        "zien. Dat alleen al is een praktische reden om een gesprek te openen over "
        "hoe het lokale team afgestemd blijft met de bredere groep.")),
    (re.compile(
        r"^(?P<company>.+?) is relevant because it shows evidence of both "
        r"international operations and onboarding or training needs, which "
        r"together suggest a practical opening for a conversation about team "
        r"support and communication\.$"),
     lambda g: (
        f"{g['company']} is relevant omdat het bewijs toont van zowel "
        "internationale activiteiten als onboarding- of trainingsbehoeften, wat "
        "samen een praktisch aanknopingspunt biedt voor een gesprek over "
        "teamondersteuning en communicatie.")),
    (re.compile(
        r"^(?P<company>.+?) is relevant because the available evidence matches "
        r"keywords associated with international teams, training, or language "
        r"support needs\.$"),
     lambda g: (
        f"{g['company']} is relevant omdat het beschikbare bewijs overeenkomt met "
        "trefwoorden die geassocieerd worden met internationale teams, training "
        "of taalondersteuningsbehoeften.")),
    (re.compile(
        r"^(?P<company>.+?) is relevant based on the calculated commercial fit "
        r"score, even though no single strong qualitative signal stands out "
        r"yet\.$"),
     lambda g: (
        f"{g['company']} is relevant op basis van de berekende commerciële "
        "fit-score, ook al springt er nog geen enkel sterk kwalitatief signaal "
        "uit.")),
]


def localize_why_relevant_app(text):
    if not text:
        return text
    for regex, render in _WHY_RELEVANT_RULES:
        m = regex.fullmatch(text)
        if m:
            return render(m.groupdict())
    return text


# ---------------------------------------------------------------------------
# caller_angle_app (lead_caller_app_fields_builder._caller_angle_app)
# ---------------------------------------------------------------------------

_CALLER_ANGLE_FIXED_NL: dict[str, str] = {
    "Explore whether onboarding, training, or learning needs are handled "
    "centrally or locally, and who owns that decision today.":
        "Verken of onboarding-, training- of leerbehoeften centraal of lokaal "
        "worden geregeld, en wie deze beslissing vandaag de dag neemt.",
    "Ask how they currently support international teams, sales, service, or "
    "language-related learning.":
        "Vraag hoe zij momenteel internationale teams, sales, service of "
        "taalgerelateerd leren ondersteunen.",
    "Use a light discovery angle: ask a few open questions to validate whether "
    "international training or communication needs exist before proposing "
    "anything specific.":
        "Gebruik een lichte verkenningsaanpak: stel een paar open vragen om te "
        "toetsen of er internationale training- of communicatiebehoeften "
        "bestaan voordat je iets specifieks voorstelt.",
}

_CALLER_ANGLE_FOREIGN_HQ_RE = re.compile(
    r"Open around how (?P<team_phrase>.+?) stays aligned with international "
    r"business expectations, especially in customer-facing, sales, service, "
    r"onboarding, or internal communication roles\.")


def _render_caller_angle_foreign_hq(team_phrase: str) -> str:
    return (
        f"Open het gesprek met hoe {_team_phrase_nl(team_phrase)} aansluiting "
        "houdt bij internationale zakelijke verwachtingen, vooral in "
        "klantgerichte functies, sales, service, onboarding of interne "
        "communicatie.")


def localize_caller_angle_app(text):
    if not text:
        return text
    fixed = _CALLER_ANGLE_FIXED_NL.get(text)
    if fixed is not None:
        return fixed
    m = _CALLER_ANGLE_FOREIGN_HQ_RE.fullmatch(text)
    if m:
        return _render_caller_angle_foreign_hq(m.group("team_phrase"))
    return text


# ---------------------------------------------------------------------------
# call_starter_app (lead_caller_app_fields_builder._call_starter_app)
# ---------------------------------------------------------------------------

_CALL_STARTER_RULES: list[tuple[re.Pattern, "callable"]] = [
    (re.compile(
        r"^I saw that (?P<company>.+?) appears to operate (?P<where>.+?) within "
        r"a wider international group context\. I was wondering how you "
        r"currently support teams that need to work across local priorities "
        r"and international expectations\.$"),
     lambda g: (
        f"Ik zag dat {g['company']} {_where_nl(g['where'])} lijkt te opereren "
        "binnen een bredere internationale groepscontext. Ik vroeg me af hoe "
        "jullie momenteel teams ondersteunen die moeten schakelen tussen lokale "
        "prioriteiten en internationale verwachtingen.")),
    (re.compile(
        r"^I saw some signals around international operations and people "
        r"development at (?P<company>.+?), and wanted to ask how you support "
        r"teams across countries\.$"),
     lambda g: (
        f"Ik zag enkele signalen rond internationale activiteiten en "
        f"mensontwikkeling bij {g['company']}, en wilde vragen hoe jullie teams "
        "in verschillende landen ondersteunen.")),
    (re.compile(
        r"^I am reaching out to understand whether (?P<company>.+?) has "
        r"international training or language support needs\.$"),
     lambda g: (
        f"Ik neem contact op om te begrijpen of {g['company']} behoefte heeft "
        "aan internationale training of taalondersteuning.")),
]


def localize_call_starter_app(text):
    if not text:
        return text
    for regex, render in _CALL_STARTER_RULES:
        m = regex.fullmatch(text)
        if m:
            return render(m.groupdict())
    return text


# ---------------------------------------------------------------------------
# caution_app (lead_caller_app_fields_builder._caution_app) — a "; "-joined
# list of fixed-template items (no interpolated variables).
# ---------------------------------------------------------------------------

_CAUTION_ITEM_NL: dict[str, str] = {
    "Manual review recommended before outreach.":
        "Handmatige controle aanbevolen vóór contactopname.",
    "HQ interpretation reported an error.":
        "Bij de interpretatie van het hoofdkantoor is een fout gemeld.",
    "HQ confidence is low.":
        "De betrouwbaarheid van het hoofdkantoorsignaal is laag.",
    "Foreign HQ signal without a detected HQ country.":
        "Signaal van een buitenlands hoofdkantoor zonder gedetecteerd "
        "hoofdkantoorland.",
    "The HQ evidence source does not clearly match the lead's own domain; "
    "verify the HQ signal before relying on it.":
        "De bron van het hoofdkantoorbewijs komt niet duidelijk overeen met het "
        "domein van de lead zelf; controleer het signaal voordat je erop "
        "vertrouwt.",
    "The foreign-HQ signal was flagged for manual review before being treated "
    "as confirmed.":
        "Het signaal van een buitenlands hoofdkantoor is gemarkeerd voor "
        "handmatige controle voordat het als bevestigd wordt behandeld.",
    "No non-HQ evidence collected yet.":
        "Er is nog geen bewijs verzameld buiten het hoofdkantoorsignaal.",
    "Commercial score uses missing signal defaults.":
        "De commerciële score gebruikt standaardwaarden voor ontbrekende "
        "signalen.",
}


_CAUTION_ITEM_RE = re.compile(
    "|".join(re.escape(item)
             for item in sorted(_CAUTION_ITEM_NL, key=len, reverse=True)))


def localize_caution_app(text):
    """Replace each *whole* known caution item wherever it appears.

    Deliberately does not split on "; " first: one known item's own text
    ("...domain; verify the HQ signal...") contains that exact separator, so
    splitting on it would shred a single item into two unmatched fragments.
    Matching the complete known sentences directly avoids that.
    """
    if not text:
        return text
    return _CAUTION_ITEM_RE.sub(lambda m: _CAUTION_ITEM_NL[m.group(0)], text)


# ---------------------------------------------------------------------------
# what_is_hot_app / what_is_not_app items (lead_caller_app_fields_builder.
# _hot_items / _not_hot_items) — fixed-template list items, no variables.
# By the time these reach localization they are already split into a list
# (see export_lead_prioritizer_to_lovable_json.parse_array_field), so each
# item is translated independently.
# ---------------------------------------------------------------------------

_HOT_ITEM_NL: dict[str, str] = {
    "Foreign-parent context gives a clear reason to discuss cross-border "
    "communication and team alignment.":
        "Buitenlandse moedercontext geeft een duidelijke reden om "
        "grensoverschrijdende communicatie en teamafstemming te bespreken.",
    "Signals point to international operations and onboarding or training "
    "needs.":
        "Signalen wijzen op internationale activiteiten en onboarding- of "
        "trainingsbehoeften.",
    "Signals suggest international operations that may need cross-border "
    "communication support.":
        "Signalen wijzen op internationale activiteiten die mogelijk "
        "grensoverschrijdende communicatieondersteuning nodig hebben.",
    "The enrichment data indicates onboarding or training needs worth "
    "exploring.":
        "De verrijkte data wijst op onboarding- of trainingsbehoeften die het "
        "waard zijn om te verkennen.",
    "Company size or complexity suggests structured training coordination "
    "may be relevant.":
        "Bedrijfsomvang of -complexiteit suggereert dat gestructureerde "
        "trainingscoördinatie relevant kan zijn.",
    "Keyword evidence signals alignment with the target profile for language "
    "or training support.":
        "Trefwoordbewijs wijst op aansluiting bij het doelprofiel voor taal- "
        "of trainingsondersteuning.",
}

_NOT_HOT_ITEM_NL: dict[str, str] = {
    "The evidence does not yet show detailed supporting signals beyond the "
    "HQ check.":
        "Het bewijs toont nog geen gedetailleerde ondersteunende signalen "
        "naast de hoofdkantoorcheck.",
    "No structured non-HQ signals have been extracted yet.":
        "Er zijn nog geen gestructureerde niet-hoofdkantoorsignalen "
        "geëxtraheerd.",
    "The evidence does not yet show clear signs of international "
    "operations.":
        "Het bewijs toont nog geen duidelijke tekenen van internationale "
        "activiteiten.",
    "No onboarding or training need signal was found in the available "
    "evidence.":
        "Er is geen signaal voor onboarding- of trainingsbehoefte gevonden in "
        "het beschikbare bewijs.",
    "The evidence does not yet show company size or complexity signals.":
        "Het bewijs toont nog geen signalen over bedrijfsomvang of "
        "-complexiteit.",
    "No keyword evidence matching the target profile was found.":
        "Er is geen trefwoordbewijs gevonden dat aansluit bij het "
        "doelprofiel.",
    "A commercial fit score has not yet been calculated for this lead.":
        "Voor deze lead is nog geen commerciële fit-score berekend.",
    "Source evidence should be checked before outreach.":
        "Controleer de brondata voordat je contact opneemt.",
}


def localize_what_is_hot_item(item):
    if not item:
        return item
    return _HOT_ITEM_NL.get(item, item)


def localize_what_is_not_item(item):
    if not item:
        return item
    return _NOT_HOT_ITEM_NL.get(item, item)


# ---------------------------------------------------------------------------
# parent_hq_summary_app (lead_caller_app_fields_builder._parent_hq_summary)
# ---------------------------------------------------------------------------

_PARENT_HQ_SUMMARY_RULES: list[tuple[re.Pattern, "callable"]] = [
    (re.compile(
        r"^The enrichment data identifies (?P<parent>.+?) as the parent "
        r"company, with HQ context in (?P<location>.+?)\.$"),
     lambda g: (
        f"De verrijkte data identificeert {g['parent']} als het moederbedrijf, "
        f"met hoofdkantoorcontext in {g['location']}.")),
    (re.compile(
        r"^The enrichment data identifies (?P<parent>.+?) as the parent "
        r"company\.$"),
     lambda g: (
        f"De verrijkte data identificeert {g['parent']} als het "
        "moederbedrijf.")),
    (re.compile(
        r"^The enrichment data indicates a foreign parent/HQ context in "
        r"(?P<location>.+?)\.$"),
     lambda g: (
        "De verrijkte data wijst op een buitenlandse moeder-/"
        f"hoofdkantoorcontext in {g['location']}.")),
]


def localize_parent_hq_summary_app(text):
    if not text:
        return text
    for regex, render in _PARENT_HQ_SUMMARY_RULES:
        m = regex.fullmatch(text)
        if m:
            return render(m.groupdict())
    return text


# ---------------------------------------------------------------------------
# cold_caller_summary_app — a two-sentence concatenation:
#   f"{foreign_hq_sentence} {caller_angle_app}"   (foreign-HQ leads)
#   f"{why_relevant_app} {caller_angle_app}"      (otherwise)
# (lead_caller_app_fields_builder.build_caller_app_fields /
# _foreign_hq_sentence). Rebuilt by matching the known caller_angle_app
# suffix first, then the known prefix sentence, and rendering both in Dutch.
# ---------------------------------------------------------------------------

_FOREIGN_HQ_SENTENCE_RE = re.compile(
    r"^The company appears to be a (?P<country_adj>.+?) operation connected "
    r"to (?P<context>.+?)\. This creates a concrete reason to explore "
    r"cross-border communication, onboarding, and alignment with "
    r"international group expectations\.$")


def _render_foreign_hq_sentence(country_adj: str, context: str) -> str:
    return (
        f"Het bedrijf lijkt een {_country_adj_nl(country_adj)} activiteit te "
        f"zijn die verbonden is met {_context_nl(context)}. Dit vormt een "
        "concrete reden om grensoverschrijdende communicatie, onboarding en "
        "afstemming met internationale groepsverwachtingen te verkennen.")


def _render_cold_caller_prefix(prefix: str) -> "str | None":
    m = _FOREIGN_HQ_SENTENCE_RE.fullmatch(prefix)
    if m:
        return _render_foreign_hq_sentence(m.group("country_adj"), m.group("context"))
    for regex, render in _WHY_RELEVANT_RULES:
        m = regex.fullmatch(prefix)
        if m:
            return render(m.groupdict())
    return None


def localize_cold_caller_summary_app(text):
    if not text:
        return text

    for english_suffix, dutch_suffix in _CALLER_ANGLE_FIXED_NL.items():
        suffix = " " + english_suffix
        if text.endswith(suffix):
            prefix = text[: -len(suffix)]
            dutch_prefix = _render_cold_caller_prefix(prefix)
            if dutch_prefix is not None:
                return f"{dutch_prefix} {dutch_suffix}"

    m = _CALLER_ANGLE_FOREIGN_HQ_RE.search(text)
    if m and m.end() == len(text) and text[: m.start()].endswith(" "):
        prefix = text[: m.start() - 1]
        dutch_prefix = _render_cold_caller_prefix(prefix)
        if dutch_prefix is not None:
            dutch_suffix = _render_caller_angle_foreign_hq(m.group("team_phrase"))
            return f"{dutch_prefix} {dutch_suffix}"

    return text


# ---------------------------------------------------------------------------
# visible_icp_signal_scores[] foreign-HQ evidence text
# (export_lead_prioritizer_to_lovable_json.build_foreign_hq_evidence_text).
# Only ever applied to the app-generated foreign-HQ row's evidence — never to
# other signals' evidence_quote/reason, which may hold external source text.
# ---------------------------------------------------------------------------

_FOREIGN_HQ_EVIDENCE_RULES: list[tuple[re.Pattern, "callable"]] = [
    (re.compile(
        r"^Confirmed foreign parent: (?P<parent>.+?), HQ (?P<country>.+?) "
        r"\((?P<city>.+?)\)\.$"),
     lambda g: (
        f"Bevestigd buitenlands moederbedrijf: {g['parent']}, hoofdkantoor "
        f"{g['country']} ({g['city']}).")),
    (re.compile(
        r"^Confirmed foreign parent: (?P<parent>.+?), HQ (?P<country>.+?)\.$"),
     lambda g: (
        f"Bevestigd buitenlands moederbedrijf: {g['parent']}, hoofdkantoor "
        f"{g['country']}.")),
    (re.compile(
        r"^Confirmed foreign parent: (?P<parent>.+?) \((?P<city>.+?)\)\.$"),
     lambda g: (
        f"Bevestigd buitenlands moederbedrijf: {g['parent']} ({g['city']}).")),
    (re.compile(r"^Confirmed foreign parent: (?P<parent>.+?)\.$"),
     lambda g: f"Bevestigd buitenlands moederbedrijf: {g['parent']}."),
    (re.compile(r"^Foreign headquarters detected: (?P<country>.+?)\.$"),
     lambda g: f"Buitenlands hoofdkantoor gedetecteerd: {g['country']}."),
    (re.compile(r"^Foreign headquarters or group structure detected\.$"),
     lambda g: "Buitenlands hoofdkantoor of groepsstructuur gedetecteerd."),
]


def localize_foreign_hq_evidence_text(text):
    if not text:
        return text
    for regex, render in _FOREIGN_HQ_EVIDENCE_RULES:
        m = regex.fullmatch(text)
        if m:
            return render(m.groupdict())
    return text


# ---------------------------------------------------------------------------
# evidence_summary_app (lead_app_summary_builder.build_evidence_summary_app)
# — one line per present supported signal. The free-form ``signal_reason``
# tail is never translated (it can be arbitrary or technical text), so it is
# dropped from the Dutch line rather than left in English: label/score/
# confidence are structural and safe to rebuild; the reason is not.
# ---------------------------------------------------------------------------

_EVIDENCE_SCORE_CONFIDENCE_RE = re.compile(
    r"^score (?P<score>\d+(?:\.\d+)?)(?:, (?P<confidence>High|Medium|Low) "
    r"confidence)?")
_EVIDENCE_CONFIDENCE_ONLY_RE = re.compile(
    r"^(?P<confidence>High|Medium|Low) confidence")


def _parse_evidence_line(line: str):
    """Return (label, score, confidence) for a known signal label, else None."""
    if ":" not in line:
        return None
    label, _, rest = line.partition(":")
    label = label.strip()
    rest = rest.strip()
    if label not in SIGNAL_SUMMARY_LABEL_TRANSLATIONS_NL:
        return None
    if not rest:
        return label, None, None
    m = _EVIDENCE_SCORE_CONFIDENCE_RE.match(rest)
    if m:
        return label, m.group("score"), m.group("confidence")
    m = _EVIDENCE_CONFIDENCE_ONLY_RE.match(rest)
    if m:
        return label, None, m.group("confidence")
    # Bare reason text with no score/confidence parts — nothing structural
    # to rebuild; the label alone is still valid Dutch content.
    return label, None, None


def localize_evidence_summary_app(text):
    if not text:
        return text
    out_lines = []
    for line in str(text).split("\n"):
        parsed = _parse_evidence_line(line)
        if parsed is None:
            out_lines.append(line)
            continue
        label, score, confidence = parsed
        label_nl = SIGNAL_SUMMARY_LABEL_TRANSLATIONS_NL[label]
        parts = []
        if score:
            parts.append(f"score {score}")
        if confidence:
            parts.append(f"{_CONFIDENCE_NL.get(confidence, confidence)} betrouwbaarheid")
        if parts:
            out_lines.append(f"{label_nl}: " + ", ".join(parts) + ".")
        else:
            out_lines.append(f"{label_nl}.")
    return "\n".join(out_lines)
