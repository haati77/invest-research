"""Streamlit-Dashboard.  Start:  streamlit run src/invest_research/dashboard/app.py"""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy.orm import Session

from invest_research import queries
from invest_research.config import load_settings
from invest_research.logsetup import tail
from invest_research.usage import Budget, usage_summary
from invest_research.db import get_engine, init_db
from invest_research.engine import RATIOS
from invest_research.profiles import CORE_RATIOS, ABSOLUTE, diff_profiles, render_markdown
from invest_research.peers import add_percentiles, overall_score, snapshot

st.set_page_config(page_title="Investment-Forschung", page_icon="📈", layout="wide")

# Validierte Kategorienfarben (dataviz-Palette, Slots 1-3), je Modus eigene Stufen.
PALETTE = {"light": ["#2a78d6", "#eb6834", "#1baf7a"], "dark": ["#3987e5", "#d95926", "#199e70"]}
SURFACE = {"light": "#fcfcfb", "dark": "#1a1a19"}  # Ring um Punkte in Hintergrundfarbe
MARGIN_LABELS = {"gross_margin": "Bruttomarge", "operating_margin": "Operative Marge",
                 "net_margin": "Nettomarge"}  # feste Reihenfolge -> feste Farben je Serie


def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


@st.cache_resource
def get_db_engine():
    engine = get_engine(load_settings())
    init_db(engine)
    return engine


def fmt_pct(x, signed: bool = False) -> str:
    return "-" if x is None or pd.isna(x) else f"{x * 100:{'+' if signed else ''}.1f}%"


def fmt_cap(x) -> str:
    if x is None or pd.isna(x):
        return "-"
    return f"{x / 1e12:.2f} Bio. $" if x >= 1e12 else f"{x / 1e9:.1f} Mrd. $"


def fmt_x(x) -> str:
    return "-" if x is None or pd.isna(x) else f"{x:.1f}x"


def empty_state() -> None:
    st.info("Noch keine Daten. Im Projektordner ausfuehren:\n\n"
            "`python -m invest_research update`")


# ---------------------------------------------------------------- Uebersicht
def page_overview(session: Session) -> None:
    st.header("Watchlist")
    ov = queries.overview(session)
    if ov.empty:
        return empty_state()
    sectors = sorted(ov["sector"].dropna().unique())
    chosen = st.multiselect("Branche", sectors, default=sectors)
    ov = ov[ov["sector"].isin(chosen)]
    table = pd.DataFrame({
        "Name": ov["name"].str.title().str.slice(0, 22), "Kurs ($)": ov["price"],
        "1 Mon.": ov["chg_1m"] * 100, "Seit Jahresbeg.": ov["chg_ytd"] * 100,
        "1 Jahr": ov["chg_1y"] * 100, "Verlauf 1J": ov["spark"],
        "52W-Lage": ov["pos_52w"] * 100, "Marktkap.": ov["market_cap"].map(fmt_cap),
        "KGV": ov["pe"], "KUV": ov["ps"], "Kurs/FCF": ov["p_fcf"],
    })
    table.index.name = "Ticker"
    pct = lambda label: st.column_config.NumberColumn(label, format="%+.1f%%")  # noqa: E731
    st.dataframe(
        table, width="stretch", height="content",
        column_config={
            "Kurs ($)": st.column_config.NumberColumn(format="%.2f"),
            "1 Mon.": pct("1 Mon."), "Seit Jahresbeg.": pct("Seit Jahresbeg."), "1 Jahr": pct("1 Jahr"),
            "Verlauf 1J": st.column_config.LineChartColumn("Verlauf 1J"),
            "52W-Lage": st.column_config.ProgressColumn(
                "52W-Lage", help="0 % = Jahrestief, 100 % = Jahreshoch",
                min_value=0, max_value=100, format="%.0f%%"),
            "KGV": st.column_config.NumberColumn(format="%.1f"),
            "KUV": st.column_config.NumberColumn(format="%.1f"),
            "Kurs/FCF": st.column_config.NumberColumn(format="%.1f"),
        })
    st.caption("KGV, KUV und Kurs/FCF nutzen die letzten 12 Monate (TTM). '-' = nicht berechenbar "
               "(z. B. Verlust, fehlende Daten); es wird nichts geschaetzt.")
    unseen = queries.events(session, only_unseen=True)
    if not unseen.empty:
        st.warning(f"{len(unseen)} ungelesene Alerts, siehe Seite 'Alerts'.")


# ---------------------------------------------------------------- Peer-Vergleich
def page_peers(session: Session) -> None:
    st.header("Peer-Vergleich")
    c1, c2 = st.columns([1, 2])
    basis = c1.radio("Zeitraum", ["FY", "TTM"], horizontal=True,
                     format_func=lambda b: "Letztes Geschaeftsjahr" if b == "FY" else "Letzte 12 Monate")
    df = snapshot(session, basis)
    if df.empty:
        return empty_state()
    sectors = sorted(df["sector"].dropna().unique())
    chosen = c2.multiselect("Branche", sectors, default=sectors)
    df = add_percentiles(df)
    df["score"] = overall_score(df)
    df = df[df["sector"].isin(chosen)]

    metric = st.selectbox("Kennzahl", list(RATIOS), format_func=lambda k: RATIOS[k][0])
    label, higher_better, kind = RATIOS[metric]
    d = df[[metric, "sector"]].dropna().reset_index()
    if d.empty:
        st.info("Fuer diese Kennzahl liegen keine Werte vor.")
    else:
        d["wert"] = d[metric] * 100 if kind == "pct" else d[metric]
        unit = "%" if kind == "pct" else "x"
        base = alt.Chart(d).encode(
            y=alt.Y("ticker:N", sort=alt.EncodingSortField("wert", order="descending" if higher_better else "ascending"),
                    title=None),
            x=alt.X("wert:Q", title=f"{label} ({unit})"),
            tooltip=[alt.Tooltip("ticker", title="Ticker"), alt.Tooltip("sector", title="Branche"),
                     alt.Tooltip("wert:Q", title=label, format=".1f")])
        bars = base.mark_bar(color=PALETTE[theme_mode()][0], cornerRadiusTopRight=4,
                             cornerRadiusBottomRight=4, size=18)
        text = base.mark_text(align="left", dx=4).encode(text=alt.Text("wert:Q", format=".1f"))
        st.altair_chart((bars + text).properties(height=max(120, 34 * len(d))), width="stretch")
        st.caption(("Hoeher" if higher_better else "Niedriger") + " ist besser.")

    show = ["sector", "period_end", "revenue_growth", "gross_margin", "operating_margin",
            "net_margin", "fcf_margin", "roe", "debt_to_equity", "score"]
    out = df[show].copy()
    for col, (_, _, k) in RATIOS.items():
        if col in out and k == "pct":
            out[col] = out[col] * 100
    out["score"] = out["score"] * 100
    ov = queries.overview(session)
    if not ov.empty:
        out = out.join(ov[["pe", "ps", "p_fcf"]])
    out = out.rename(columns={c: RATIOS[c][0] for c in RATIOS if c in out} | {
        "sector": "Branche", "period_end": "Stichtag", "score": "Score", "pe": "KGV", "ps": "KUV",
        "p_fcf": "Kurs/FCF"})
    pct_cols = [RATIOS[c][0] for c, v in RATIOS.items() if v[2] == "pct"]
    st.dataframe(out.sort_values(["Branche", "Score"], ascending=[True, False]), width="stretch",
                 column_config={**{c: st.column_config.NumberColumn(format="%.1f%%") for c in pct_cols if c in out},
                                RATIOS["debt_to_equity"][0]: st.column_config.NumberColumn(format="%.2fx"),
                                "Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
                                "KGV": st.column_config.NumberColumn(format="%.1f"),
                                "KUV": st.column_config.NumberColumn(format="%.1f"),
                                "Kurs/FCF": st.column_config.NumberColumn(format="%.1f")})
    st.caption("Score = mittleres Perzentil innerhalb der Branche (0 = schlechtester, 100 = bester), "
               "nur grobe Orientierung bei wenigen Peers. Geschaeftsjahre enden unterschiedlich: Stichtag beachten.")


# ---------------------------------------------------------------- Unternehmen
def price_chart(ph: pd.DataFrame, color: str, ring: str) -> alt.Chart:
    nearest = alt.selection_point(nearest=True, on="mouseover", fields=["date"], empty=False)
    base = alt.Chart(ph).encode(x=alt.X("date:T", title=None, axis=alt.Axis(format="%m/%Y", labelOverlap=True)),
                                y=alt.Y("close:Q", title="Schlusskurs ($)", scale=alt.Scale(zero=False)))
    line = base.mark_line(color=color, strokeWidth=2)
    hit = base.mark_point(opacity=0).add_params(nearest)
    dot = base.mark_circle(color=color, size=70, stroke=ring, strokeWidth=2).transform_filter(nearest)
    rule = alt.Chart(ph).mark_rule(color="gray", strokeDash=[3, 3]).encode(
        x="date:T", tooltip=[alt.Tooltip("date:T", title="Datum"), alt.Tooltip("close:Q", title="Kurs ($)", format=".2f")]
    ).transform_filter(nearest)
    return (line + hit + dot + rule).properties(height=320)


def page_company(session: Session) -> None:
    companies = queries.watchlist(session)
    if not companies:
        return empty_state()
    by_ticker = {c.ticker: c for c in companies}
    ticker = st.selectbox("Unternehmen", list(by_ticker),
                          format_func=lambda t: f"{t}  -  {by_ticker[t].name or ''}")
    c = by_ticker[ticker]
    colors = PALETTE[theme_mode()]
    st.header(f"{c.name or ticker} ({ticker})")
    st.caption(f"Branche: {c.sector or '-'}")

    ov = queries.overview(session)
    row = ov.loc[ticker] if ticker in ov.index else None
    if row is not None:
        k = st.columns(5)
        k[0].metric("Kurs", "-" if pd.isna(row["price"]) else f"{row['price']:.2f} $",
                    None if pd.isna(row["chg_1m"]) else f"{row['chg_1m'] * 100:+.1f}% (1 Monat)")
        k[1].metric("Marktkapitalisierung", fmt_cap(row["market_cap"]))
        k[2].metric("KGV (TTM)", fmt_x(row["pe"]))
        k[3].metric("KUV (TTM)", fmt_x(row["ps"]))
        k[4].metric("Kurs/FCF (TTM)", fmt_x(row["p_fcf"]))

    st.subheader("Kursverlauf")
    ph = queries.price_history(session, c.id)
    if ph.empty:
        st.info("Keine Kursdaten. `python -m invest_research prices` ausfuehren.")
    else:
        rng = st.segmented_control("Zeitraum", ["1M", "6M", "1J", "5J"], default="1J", key="rng")
        days = {"1M": 30, "6M": 183, "1J": 365, "5J": 1826}[rng or "1J"]
        cut = pd.Timestamp(ph["date"].max()) - pd.Timedelta(days=days)
        view = ph[pd.to_datetime(ph["date"]) >= cut]
        st.altair_chart(price_chart(view, colors[0], SURFACE[theme_mode()]), width="stretch")

    st.subheader("Geschaeftsjahre")
    fy = queries.annual_series(session, c.id, ["revenue", "net_income"] + list(MARGIN_LABELS))
    if fy.empty:
        st.info("Keine Kennzahlen. `python -m invest_research update` ausfuehren.")
    else:
        fy["Jahr"] = pd.to_datetime(fy["period_end"]).dt.year.astype(str)
        years = sorted(fy["Jahr"].unique())[-8:]
        fy = fy[fy["Jahr"].isin(years)]
        left, right = st.columns(2)
        for col, name, title in ((left, "revenue", "Umsatz (Mrd. $)"), (right, "net_income", "Nettogewinn (Mrd. $)")):
            d = fy[fy["name"] == name].assign(wert=lambda x: x["value"] / 1e9)
            if d.empty:
                col.info(f"{title}: keine Daten")
                continue
            ch = alt.Chart(d).mark_bar(color=colors[0], cornerRadiusTopLeft=4, cornerRadiusTopRight=4,
                                       size=26).encode(
                x=alt.X("Jahr:O", title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("wert:Q", title=title),
                tooltip=[alt.Tooltip("period_end:T", title="Stichtag"), alt.Tooltip("wert:Q", title=title, format=",.1f")])
            col.altair_chart(ch.properties(height=260), width="stretch")

        m = fy[fy["name"].isin(MARGIN_LABELS)].copy()
        m["Marge"] = m["name"].map(MARGIN_LABELS)
        if m.empty:
            st.info("Keine Margen verfuegbar.")
        else:
            m["wert"] = m["value"] * 100
            color = alt.Color("Marge:N", scale=alt.Scale(domain=list(MARGIN_LABELS.values()),
                                                         range=colors), legend=alt.Legend(title=None, orient="top"))
            base = alt.Chart(m).encode(x=alt.X("Jahr:O", title=None, axis=alt.Axis(labelAngle=0)),
                                       y=alt.Y("wert:Q", title="Marge (%)"), color=color)
            lines = base.mark_line(strokeWidth=2)
            pts = base.mark_circle(size=60, stroke=SURFACE[theme_mode()], strokeWidth=2).encode(
                tooltip=[alt.Tooltip("Marge:N"), alt.Tooltip("Jahr:O"), alt.Tooltip("wert:Q", title="%", format=".1f")])
            last = m[m["Jahr"] == m.groupby("Marge")["Jahr"].transform("max")]
            labels = alt.Chart(last).mark_text(align="left", dx=8, fontSize=12).encode(
                x="Jahr:O", y="wert:Q", text=alt.Text("wert:Q", format=".0f"), color=color)
            st.altair_chart((lines + pts + labels).properties(height=280), width="stretch")
        with st.expander("Daten als Tabelle"):
            wide = fy.pivot_table(index="name", columns="Jahr", values="value")
            st.dataframe(wide, width="stretch")

    st.subheader("Letzte Filings")
    fl = queries.recent_filings(session, c.id)
    st.dataframe(fl.rename(columns={"filed_at": "Eingereicht", "form_type": "Formular",
                                    "period_end": "Berichtsperiode", "url": "Link"}),
                 width="stretch", hide_index=True,
                 column_config={"Link": st.column_config.LinkColumn("Link", display_text="oeffnen")})
    render_analysis(session, ticker)


SECTION_TITLES = {"business": "Geschaeftsmodell", "risks": "Risiken", "risks_diff": "Neue Risiken ggue. Vorjahr",
                  "mdna": "Management-Analyse (MD&A)"}
KIND_LABELS = {"business": "Geschaeft", "segment": "Segment", "strategy": "Strategie", "risk": "Risiko",
               "driver": "Treiber", "outlook": "Ausblick", "other": "Sonstiges"}


def render_analysis(session: Session, ticker: str) -> None:
    st.subheader("KI-Analyse der Berichte")
    rows = queries.latest_analyses(session, ticker)
    if not rows:
        st.info(f"Noch keine Analyse. Im Terminal: `python -m invest_research analyze --ticker {ticker}`")
        return
    st.caption("Aussagen werden nur angezeigt, wenn ihr Zitat woertlich im Originaltext steht. "
               "Die Zusammenfassung ist eine KI-Formulierung und nicht zitatgeprueft. Keine Anlageberatung.")
    filings: dict[tuple, list[dict]] = {}
    for r in rows:
        filings.setdefault((r["form_type"], r["filed_at"], r["url"]), []).append(r)
    for i, ((form, filed, url), secs) in enumerate(filings.items()):
        with st.expander(f"{form} vom {filed}", expanded=(i == 0)):
            if url:
                st.markdown(f"[Originaldokument]({url})")
            tabs = st.tabs([SECTION_TITLES.get(r["section"], r["section"]) for r in secs])
            for tab, r in zip(tabs, secs):
                c = r["content"]
                with tab:
                    st.markdown(f"**KI-Zusammenfassung:** {c['summary']}")
                    if c.get("tone"):
                        suffix = "" if c.get("tone_verified") else " (Zitat nicht verifiziert)"
                        st.markdown(f"**Ton des Managements:** {c['tone']}{suffix}")
                    for it in sorted((x for x in c["items"] if x["verified"]), key=lambda x: -x["importance"]):
                        st.markdown(f"**{it['title']}** ({KIND_LABELS.get(it['kind'], it['kind'])})  \n"
                                    f"{it['statement']}")
                        st.caption(f"Originalzitat: \u201e{it['quote']}\u201c")
                    hidden = c["n_items"] - c["n_verified"]
                    note = f"{c['n_verified']} von {c['n_items']} Aussagen mit verifiziertem Zitat"
                    if hidden:
                        note += f"; {hidden} ohne verifiziertes Zitat ausgeblendet"
                    if c.get("truncated"):
                        note += "; Abschnitt wurde fuer die Analyse gekuerzt"
                    st.caption(note + f". Modell: {r['model']}.")


# ---------------------------------------------------------------- Alerts
KIND_NAMES = {"new_filing": "Neues Filing", "metric_change": "Kennzahlensprung",
              "price_move": "Kursereignis", "text_change": "Textaenderung"}
SEVERITY_NAMES = {3: "HOCH", 2: "MITTEL", 1: "niedrig"}


def page_alerts(session: Session) -> None:
    st.header("Alerts")
    ev = queries.events(session, limit=300)
    if ev.empty:
        st.info("Noch keine Alerts. Sie entstehen bei neuen Filings, Kennzahlenspruengen, Kursereignissen und "
                "Aenderungen der Risikofaktoren: `python -m invest_research watch`")
        return
    c1, c2, c3 = st.columns([2, 2, 1])
    tickers = c1.multiselect("Unternehmen", sorted(ev["ticker"].unique()), default=[], placeholder="Alle")
    kinds = c2.multiselect("Art", list(KIND_NAMES), default=list(KIND_NAMES), format_func=lambda k: KIND_NAMES[k])
    min_sev = c3.selectbox("Ab Wichtigkeit", [1, 2, 3], format_func=lambda n: SEVERITY_NAMES[n])
    only_new = st.toggle("Nur ungelesene", value=True)
    view = ev[ev["kind"].isin(kinds) & (ev["severity"] >= min_sev)]
    if tickers:
        view = view[view["ticker"].isin(tickers)]
    if only_new:
        view = view[~view["seen"]]
    if view.empty:
        st.success("Keine passenden Alerts.")
        return
    left, right = st.columns([3, 1])
    left.caption(f"{len(view)} Alert(s). Kennzahlen- und Kursmeldungen kommen aus dem Code; "
                 "Textaenderungen enthalten eine KI-Einordnung mit geprueften Zitaten.")
    if right.button("Angezeigte als gelesen markieren"):
        queries.mark_events_seen(session, [int(i) for i in view["id"]])
        st.rerun()
    for _, r in view.sort_values(["severity", "created_at"], ascending=[False, False]).iterrows():
        label = f"[{SEVERITY_NAMES[int(r['severity'])]}] {r['ticker']}  {r['title']}"
        with st.expander(label + ("" if not r["seen"] else "  (gelesen)")):
            st.caption(f"{KIND_NAMES.get(r['kind'], r['kind'])} | erkannt {r['created_at']:%Y-%m-%d %H:%M}")
            detail = r["detail"] if isinstance(r["detail"], str) else ""  # fehlender Text kommt als NaN
            if detail.startswith("http"):
                st.markdown(f"[Originaldokument]({detail})")
            elif detail:
                st.markdown(detail)


# ---------------------------------------------------------------- Profil
def page_profile(session: Session) -> None:
    st.header("Unternehmensprofil")
    companies = queries.watchlist(session)
    if not companies:
        return empty_state()
    by_ticker = {c.ticker: c for c in companies}
    ticker = st.selectbox("Unternehmen", list(by_ticker), key="prof_ticker",
                          format_func=lambda t: f"{t}  -  {by_ticker[t].name or ''}")
    versions = queries.profile_versions(session, ticker)
    if not versions:
        st.info(f"Noch kein Profil. Im Terminal: `python -m invest_research profile --ticker {ticker}`")
        return
    labels = [f"{v['created_at']:%Y-%m-%d %H:%M}" + (" (mit KI-Synthese)" if v["model"] else " (ohne KI-Synthese)")
              for v in versions]
    pick = st.selectbox("Version", range(len(versions)), format_func=lambda i: labels[i]) if len(versions) > 1 else 0
    c = versions[pick]["content"]
    sheet, syn = c["sheet"], c.get("synthesis")

    st.subheader(f"{c.get('name') or ticker} ({ticker})")
    st.caption(f"Branche: {c.get('sector') or '-'}  |  Stand: {c['generated_on']}")
    if syn:
        st.markdown(f"**{syn['headline']}**")
        st.caption("KI-Synthese: jede Aussage nennt ihre Belege; Zahlen wurden gegen die Belege geprueft. "
                   + (f"{syn['dropped']} Aussage(n) mangels Belegen verworfen. " if syn.get("dropped") else "")
                   + "Das ist keine Anlageberatung.")
    else:
        st.info("Dieses Profil enthaelt keine KI-Synthese (nur Kennzahlen und geprueften Berichtsaussagen).")

    rows = []
    for name in CORE_RATIOS + list(ABSOLUTE):
        label = RATIOS[name][0] if name in RATIOS else ABSOLUTE[name]
        cells = [sheet.get(f"F:{name}:{b}", "").rsplit(": ", 1)[-1] or "-" for b in ("FY", "TTM")]
        rows.append({"Kennzahl": label, "Letztes Geschaeftsjahr": cells[0], "Letzte 12 Monate": cells[1]})
    for key, label in (("V:pe", "KGV"), ("V:ps", "KUV"), ("V:p_fcf", "Kurs/FCF"), ("P:score", "Branchen-Score")):
        if key in sheet:
            rows.append({"Kennzahl": label, "Letztes Geschaeftsjahr": sheet[key].rsplit(": ", 1)[-1],
                         "Letzte 12 Monate": ""})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    if syn:
        cols = st.columns(3)
        for col, key, title in zip(cols, ("strengths", "weaknesses", "watch"), ("Staerken", "Schwaechen", "Beobachten")):
            with col:
                st.markdown(f"#### {title}")
                for p in syn.get(key, []):
                    st.markdown(f"- {p['statement']}")
                    with st.expander("Belege"):
                        for e in p["evidence"]:
                            st.caption(f"`{e}`  {sheet.get(e, '')}")
        if syn.get("open_questions"):
            st.markdown("#### Offene Fragen")
            for q in syn["open_questions"]:
                st.markdown(f"- {q}")
        if syn.get("dropped_points"):
            with st.expander(f"Verworfene KI-Aussagen ({len(syn['dropped_points'])})"):
                st.caption("Diese Aussagen haben die Beleg-Pruefung nicht bestanden und gelten nicht als Profilinhalt.")
                for d in syn["dropped_points"]:
                    st.markdown(f"- {d['statement']}  \n  _Grund: {d['reason']}_")

    for key, title in (("business", "Geschaeftsmodell"), ("risks", "Risiken laut Bericht"), ("mdna", "Management-Sicht")):
        sec = c.get(key)
        if not sec:
            continue
        with st.expander(f"{title} ({sec['form_type']} vom {sec['filed_at']})"):
            st.markdown(f"**KI-Zusammenfassung** (nicht zitatgeprueft): {sec['summary']}")
            for it in sorted(sec["items"], key=lambda i: -i["importance"])[:6]:
                st.markdown(f"**{it['title']}**  \n{it['statement']}")
                st.caption(f"Originalzitat: \u201e{it['quote']}\u201c")
    if pick + 1 < len(versions):
        prev = versions[pick + 1]
        d = diff_profiles(prev["content"], c)
        with st.expander(f"Aenderungen gegenueber der Vorversion vom {prev['created_at']:%Y-%m-%d %H:%M}"):
            if d["facts"]:
                st.dataframe(pd.DataFrame([{"Kennzahl": f["label"], "Vorher": f["old_text"], "Jetzt": f["new_text"]}
                                           for f in d["facts"]]), hide_index=True, width="stretch")
            else:
                st.caption("Keine Aenderung bei den Kennzahlen.")
            if d["added"] or d["removed"]:
                st.caption("KI-Formulierungen schwanken von Lauf zu Lauf: die Listen zeigen geaenderte "
                           "Formulierungen, nicht zwingend inhaltliche Aenderungen.")
            for t in d["added"]:
                st.markdown(f"+ {t}")
            for t in d["removed"]:
                st.markdown(f"- ~~{t}~~")
    if c.get("gaps"):
        with st.expander(f"Datenluecken ({len(c['gaps'])})"):
            for g in c["gaps"]:
                st.markdown(f"- {g}")
    if c.get("sources"):
        st.caption("Quellen: " + " | ".join(f"[{s['form_type']} {s['filed_at']}]({s['url']})" for s in c["sources"]))
    st.download_button("Profil als Markdown herunterladen", render_markdown(c, versions[pick]["created_at"]),
                       file_name=f"{ticker}_profil_{c['generated_on']}.md", mime="text/markdown")


# ---------------------------------------------------------------- System
def page_system(session: Session) -> None:
    st.header("System")
    settings = load_settings()
    st.subheader("Datenstand")
    ds = queries.data_status(session)
    if ds.empty:
        return empty_state()
    newest = ds["last_price"].dropna().max() if ds["last_price"].notna().any() else None
    if newest is None:
        st.warning("Keine Kursdaten vorhanden: `python -m invest_research prices`")
    elif (pd.Timestamp.today().normalize() - pd.Timestamp(newest)).days > 4:
        st.warning(f"Die Kurse sind aelter als 4 Tage (neuester: {newest}). "
                   "Aktualisieren: `python -m invest_research update`")
    ds = ds.rename(columns={"last_price": "Letzter Kurs", "last_report": "Letzter Bericht",
                            "last_profile": "Letztes Profil", "analyses": "KI-Analysen", "open_alerts": "Offene Alerts"})
    st.dataframe(ds, width="stretch")

    st.subheader("KI-Nutzung")
    u = usage_summary(session, Budget(settings.llm_max_calls_per_day, settings.llm_max_cost_per_month))
    st.progress(min(1.0, u["calls_today"] / max(1, u["max_calls_per_day"])),
                text=f"Heute {u['calls_today']} von {u['max_calls_per_day']} erlaubten KI-Aufrufen")
    cap = f" von {u['max_cost_per_month']:.2f} USD" if u["max_cost_per_month"] else ""
    st.caption(f"Dieser Monat: {u['cost_month']:.3f} USD Listenpreis{cap}. Bei einem Abo wird nichts berechnet, "
               "es zaehlt dein Nutzungskontingent. Limits stehen in der Datei `.env`.")
    if u["by_purpose"]:
        st.dataframe(pd.DataFrame(u["by_purpose"]).rename(columns={
            "purpose": "Zweck", "calls": "Aufrufe", "input_tokens": "Token rein", "output_tokens": "Token raus",
            "cost_usd": "USD (Liste)"}), hide_index=True, width="stretch")
    if u["failed_month"]:
        st.warning(f"{u['failed_month']} fehlgeschlagene KI-Aufrufe in diesem Monat. Letzte Fehler:")
        for e in u["recent_errors"]:
            st.caption(f"{e['at']:%Y-%m-%d %H:%M} {e['purpose']}: {e['error']}")

    st.subheader("Protokoll")
    log_text = tail(settings.log_path, 80)
    with st.expander("Letzte 80 Zeilen der Logdatei", expanded=False):
        st.code(log_text or "Noch kein Protokoll.", language="text")
    st.caption(f"Logdatei: {settings.log_path}")


PAGES = {"Uebersicht": page_overview, "Peer-Vergleich": page_peers, "Profil": page_profile,
         "Unternehmen": page_company, "Alerts": page_alerts, "System": page_system}


def main() -> None:
    st.title("Investment-Forschung")
    page = st.sidebar.radio("Seite", list(PAGES))
    st.sidebar.caption("Recherche-Werkzeug, keine Anlageberatung. Daten: SEC EDGAR und Yahoo Finance "
                       "(inoffiziell). Alle Angaben ohne Gewaehr.")
    with Session(get_db_engine()) as session:
        PAGES[page](session)


main()
