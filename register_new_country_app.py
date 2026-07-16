"""register_new_country_app.py — Streamlit front-end for register_new_country.py.

Lets an operator register a brand new supported country (dispatch dropdown +
Lovable manifest + GCS folder-slug map + the guard test) from a browser
instead of the command line, while writing through the exact same
``build_registration_plan`` this repo's CLI script uses — so the preview
shown here and the files actually written can never drift apart.

This still only runs locally (not deployed): it edits source files on disk
and re-runs pytest via subprocess, both of which require a real checkout, the
same constraint every other local-only app in this repo (e.g.
country_visibility_app.py) already has.

Usage:
    streamlit run register_new_country_app.py
"""

from __future__ import annotations

from register_new_country import build_registration_plan, run_affected_tests, REPO_ROOT


def main() -> None:  # pragma: no cover - exercised only under `streamlit run`
    import streamlit as st

    st.set_page_config(page_title="Nieuw land registreren", page_icon="🆕", layout="centered")
    st.title("🆕 Nieuw land registreren")
    st.caption(
        "Voegt een land toe aan de dispatch-dropdown, de Lovable-manifest-lijst "
        "en de GCS-mapnaam-lookup — de drie plekken die in sync moeten blijven "
        "(zie `register_new_country.py`'s docstring voor waarom dit bewust geen "
        "*live* self-service actie is: het is een reviewbare bestandswijziging, "
        "geen productie-toggle). Land toevoegen aan de dispatch-dropdown "
        "betekent dat verrijkingsruns het voortaan als land kunnen selecteren — "
        "controleer dus of de juiste taal-/lokalisatie-aannames daar kloppen "
        "voordat je een land hier registreert."
    )

    label = st.text_input("Landnaam", placeholder="bijv. Luxembourg").strip()
    enabled = st.checkbox(
        "Direct zichtbaar in de Lovable Company Hub",
        value=False,
        help="Standaard uit — net als elk ander nieuw land tot nu toe: pas "
             "aanzetten (via de 'Landen zichtbaarheid'-app) zodra de eerste "
             "echte run voor dit land geverifieerd is.",
    )

    if not label:
        st.info("Vul een landnaam in om een voorbeeld te zien.")
        return

    plan = build_registration_plan(label, enabled=enabled)

    if plan["error"]:
        st.error(plan["error"])
        return
    if plan["already_exists"]:
        st.warning(f"**{plan['label']}** is al een geregistreerd land — niets te doen.")
        return

    st.write(f"**Slug:** `{plan['slug']}`  ·  "
             f"**Lovable-zichtbaarheid:** {'direct aan' if enabled else 'uit (tot handmatig aangezet)'}")
    st.write("**Bestanden die aangepast worden:**")
    for path in plan["new_contents"]:
        st.write(f"- `{path.relative_to(REPO_ROOT)}`")

    with st.expander("Voorbeeld van de wijzigingen"):
        for path, content in plan["new_contents"].items():
            st.caption(str(path.relative_to(REPO_ROOT)))
            original = path.read_text(encoding="utf-8")
            for old_line, new_line in zip(original.splitlines(), content.splitlines()):
                if old_line != new_line:
                    st.code(f"- {old_line}\n+ {new_line}", language="diff")

    st.warning(
        "Dit schrijft direct naar bestanden in je lokale checkout (geen git "
        "commit/push) en draait daarna de bijbehorende tests. Commit en "
        "review de wijziging zelf net als bij elke andere code-aanpassing."
    )

    if st.button(f"✅ {plan['label']} registreren", type="primary"):
        for path, content in plan["new_contents"].items():
            path.write_text(content, encoding="utf-8")
        st.success(
            f"{plan['label']} geregistreerd. GCS-map `{plan['slug']}/current/` "
            "hoeft niet vooraf aangemaakt te worden -- die ontstaat automatisch "
            "bij de eerste echte export voor dit land."
        )

        with st.spinner("Tests draaien..."):
            proc = run_affected_tests()
        if proc.returncode == 0:
            st.success("Alle bijbehorende tests slagen.")
        else:
            st.error("Tests zijn gefaald na deze wijziging -- bestanden zijn wel "
                      "weggeschreven (niets is automatisch teruggedraaid).")
        st.code(proc.stdout or proc.stderr, language="text")


if __name__ == "__main__":
    main()
