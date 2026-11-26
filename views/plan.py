"""Weekly plan: what to study on each day of the week, why, and how the plan moved since the
week before. The next week is generated from the latest weakness scores."""

from functools import partial

import streamlit as st

from core import calendar_export
from piq import charts, formatter, services, study_library
from piq.config import STUDY_TIME, TIMEZONE, WEAK_THRESHOLD
from piq.ui import empty_state, page_header, require_profile, run_agent, show_last_run


def _days_done(sid: str, week_no: int, saved: set[int]) -> set[int]:
    """Tick the days of the week you finished; a change is saved at once."""
    picked = st.pills(
        "Days done",
        list(range(1, 8)),
        selection_mode="multi",
        default=sorted(saved),
        format_func=lambda d: f"Day {d}",
        key=f"plan_done_{sid}_{week_no}",  # per student: a switch must not carry ticks over
        help="Home and the Dashboard count the days you mark here.",
        wrap=True,
    )
    if set(picked) != saved:
        return services.set_days_done(sid, week_no, picked)
    return saved


PAGE = 8  # books and repositories shown before "show all"


def _library(role: str, weakness: dict[str, float]) -> None:
    """Books for the role's track (weak topics first) and curated GitHub repositories for the
    role: the same library and ranking as the web plan page (piq/study_library.py)."""
    weak = sorted(t for t, w in weakness.items() if w > WEAK_THRESHOLD)
    with st.container(border=True):
        st.subheader("Recommended Books & Deep Reading", anchor=False)
        st.caption(
            "Textbooks and handbooks for your track, with a free or official link wherever one "
            "exists. "
            + (f"Books for your weak topics ({', '.join(weak)}) come first." if weak else "")
        )
        left, right = st.columns([3, 2])
        query = left.text_input(
            "Search books", placeholder="Title, author or topic", key="lib_book_q"
        )
        level = right.segmented_control(
            "Difficulty", list(study_library.LEVELS), key="lib_book_level"
        )
        books = study_library.books_for_student(role, weak, query, level)
        if not books:
            empty_state("No books match. Clear the search or choose another level.")
        show_all = len(books) > PAGE and st.toggle(
            f"Show all {len(books)} books", key="lib_book_all"
        )
        for book, fills in books if show_all else books[:PAGE]:
            st.markdown(formatter.book_md(book, fills))
        st.caption(f"Links last checked {study_library.library()['checked']}.")

    with st.container(border=True):
        st.subheader("Nexus Curated Roadmaps & Field Guides", anchor=False)
        st.caption(
            f"Free, open-source GitHub repositories that fit {role}. Hours are rough planning "
            "estimates."
        )
        cats = {c["id"]: c["name"] for c in study_library.library()["repo_categories"]}
        left, right = st.columns([3, 2])
        query = left.text_input(
            "Search repositories", placeholder="Name, topic or skill", key="lib_repo_q"
        )
        category = right.selectbox(
            "Category",
            [None, *cats],
            format_func=lambda c: "All categories" if c is None else cats[c],
            key="lib_repo_cat",
        )
        repos = study_library.repos_for_role(role, query, category)
        if not repos:
            empty_state(
                "No repositories match yet. GitHub study guides lean towards software, so some "
                "tracks have fewer; try another category or clear the search."
            )
        for repo in repos[:PAGE]:
            st.markdown(formatter.repo_md(repo))
        if len(repos) > PAGE:
            with st.expander(f"{len(repos) - PAGE} more repositories"):
                for repo in repos[PAGE:]:
                    st.markdown(formatter.repo_md(repo))


def _export(plan: services.WeeklyPlan) -> None:
    """The shown week as a calendar file or a table, to keep outside the app. The files are
    built only when a button is clicked, not on every render."""
    with st.expander("Export this week", icon=":material/download:"):
        first = st.date_input(
            "First day", value=formatter.next_monday(formatter.local_today()), format="DD/MM/YYYY"
        )
        at = st.time_input("Study time each day", value=STUDY_TIME, step=1800)
        st.caption(
            f"Times are {TIMEZONE} time. Each day becomes one event, as long as that day's minutes."
        )
        name = f"placementiq-week-{plan.week_no}"
        with st.container(horizontal=True):
            st.download_button(
                "Calendar (.ics)",
                partial(formatter.plan_ics, plan, first, at),
                file_name=f"{name}.ics",
                mime="text/calendar",
                icon=":material/event:",
                on_click="ignore",
            )
            st.download_button(
                "Table (.csv)",
                partial(formatter.plan_csv, plan),
                file_name=f"{name}.csv",
                mime="text/csv",
                icon=":material/table_view:",
                on_click="ignore",
            )


# Choosing a week, ticking days, the table view and the export inputs rerun only this
# section, not the page (no queries, no header, no sidebar).
@st.fragment
def _plans(sid: str, ov: services.PlanOverview) -> None:
    plan_col, side_col = st.columns([2, 1], gap="medium")
    previous = None
    with plan_col:
        if not ov.plans:
            empty_state(
                "calendar_month",
                "No plan yet",
                "Generate week 1 with the button above. The planner turns your weakness scores "
                "into seven days of practice, most of it on your weakest topics.",
            )
        else:
            weeks = [p.week_no for p in ov.plans]
            week = st.pills(  # chips wrap onto more rows on a phone, however many weeks
                "Week",
                weeks,
                format_func=lambda w: f"Week {w}",
                default=weeks[-1],
                required=True,
                label_visibility="collapsed",
                wrap=True,
            )
            shown = next(p for p in ov.plans if p.week_no == week)
            previous = next((p for p in ov.plans if p.week_no == shown.week_no - 1), None)
            with st.container(border=True):
                w_top1, w_top2 = st.columns([3, 2], vertical_alignment="center")
                with w_top1:
                    st.subheader(f"Week {shown.week_no}", anchor=False)
                    st.caption(formatter.plan_summary(shown))
                with w_top2:
                    st.download_button(
                        "📥 Download 7-Day Schedule (.ics)",
                        data=partial(calendar_export.generate_plan_ics, shown),  # on click
                        file_name=f"study-schedule-week-{shown.week_no}.ics",
                        mime="text/calendar",
                        type="primary",
                        icon=":material/calendar_month:",
                        help="Export all 7 days as all-day events for Google Calendar / Outlook",
                        key=f"plan_shown_ics_{shown.week_no}",
                    )
                st.info(
                    f"**Why this plan.** {formatter.md_escape(shown.rationale)}",
                    icon=":material/lightbulb:",
                )
                done = _days_done(sid, shown.week_no, ov.done.get(shown.week_no, set()))
                ov.done[shown.week_no] = done  # a fragment rerun keeps its first arguments

                st.markdown("##### 📅 Daily Tasks & Schedule")
                for d in sorted(shown.days, key=lambda x: x.day):
                    day_is_done = d.day in done
                    badge_icon = "✅" if day_is_done else "📖"
                    task_title = f"[Skill Scout Nexus] Day {d.day}: {d.topic}"
                    task_desc = (
                        f"Day {d.day} Study Plan - Week {shown.week_no}\n"
                        f"Topic: {d.topic} ({d.duration_min} min)\n"
                        f"Practice Goal: {d.practice_goal}\n"
                        f"Subtopics: {', '.join(d.subtopics)}"
                    )
                    gcal_url = calendar_export.build_gcal_url(
                        title=task_title,
                        details=task_desc,
                        duration_min=d.duration_min,
                    )
                    with st.container(border=True):
                        d_col1, d_col2 = st.columns([4, 1], vertical_alignment="center")
                        with d_col1:
                            sub_text = " · ".join(d.subtopics)
                            status_tag = " :green[(Completed)]" if day_is_done else ""
                            st.markdown(
                                f"**{badge_icon} Day {d.day}: {d.topic}** "
                                f"({d.duration_min} min){status_tag}  \n"
                                f":gray[Subtopics: {formatter.md_escape(sub_text)}]  \n"
                                f"🎯 *Goal: {formatter.md_escape(d.practice_goal)}*"
                            )
                        with d_col2:
                            st.link_button(
                                "📅 Add to GCal",
                                url=gcal_url,
                                help=f"Add Day {d.day} task to Google Calendar in a new tab",
                                key=f"gcal_btn_w{shown.week_no}_d{d.day}",
                            )

                with st.container(border=True):
                    st.subheader("📝 Export to Google Keep / Tasks", anchor=False)
                    st.caption(
                        "Copy your 7-day checklist formatted with task boxes `[ ]` "
                        "ready to paste directly into Google Keep, Apple Notes, Notion, or Tasks."
                    )
                    checklist_md = calendar_export.format_tasks_checklist(shown, done_days=done)
                    st.code(checklist_md, language="markdown")
                    st.html(
                        calendar_export.copy_clipboard_button_html(
                            checklist_md, f"copy_btn_w{shown.week_no}"
                        )
                    )

                if st.toggle("Table view", help="The same seven days as a table."):
                    st.dataframe(formatter.plan_table(shown), width="stretch")
                else:
                    st.html(formatter.plan_days_html(shown, done))
                _export(shown)

    with side_col:
        # What changed in the week shown, then the weakness the next week is planned from.
        if previous:
            before, after = previous.minutes_by_topic(), shown.minutes_by_topic()
            pair = (previous.week_no, shown.week_no)
            with st.container(border=True):
                st.subheader(
                    formatter.minutes_shift_headline(before, after, previous.week_no),
                    anchor=False,
                )
                st.vega_lite_chart(
                    spec=charts.minutes_shift_spec(before, after, pair),
                    width="stretch",
                    alt=formatter.minutes_shift_alt(before, after, pair),
                )
                st.caption(
                    f"Minutes per topic. Hollow grey dot: week {pair[0]}. Filled dot, in the "
                    f"topic's colour: week {pair[1]}."
                )
        with st.container(border=True):
            st.subheader(formatter.weakness_headline(ov.weakness), anchor=False)
            st.vega_lite_chart(
                spec=charts.weakness_spec(ov.weakness),
                width="stretch",
                alt=formatter.weakness_alt(ov.weakness),
            )
            st.caption(f"{formatter.weakness_caption()} {formatter.weakness_source()}")


page_header(
    "Study Plan & Roadmap",
    "Unified 7-day study roadmap, weakness-driven weekly plan, calendar export, and curated "
    "books & repositories. Plans generated in either tab stay synchronized everywhere.",
)
sid, profile = require_profile()

tab_weekly, tab_roadmap, tab_library = st.tabs(
    [
        ":material/calendar_month: Weekly Study Plan",
        ":material/route: 7-Day Skill-Gap Roadmap",
        ":material/menu_book: Books & Field Guides",
    ]
)

ov = services.plan_overview(sid, profile)

with tab_weekly:
    col_top_gen, col_top_ics, col_top_email = st.columns([1, 1, 1], vertical_alignment="center")
    with col_top_gen:
        if st.button(
            f"Generate week {ov.next_week} plan", type="primary", icon=":material/autorenew:"
        ) and run_agent("Planner agent", lambda ev: services.generate_next_plan(sid, ev)):
            st.toast(
                f"Week {ov.next_week} plan generated and sent to your email with the .ics calendar file!",
                icon=":material/mail:",
            )
            st.rerun()

    with col_top_ics:
        if ov.plans:
            latest = ov.plans[-1]
            st.download_button(
                "📥 Download 7-Day Schedule (.ics)",
                data=partial(calendar_export.generate_plan_ics, latest),  # built on click
                file_name=f"study-schedule-week-{latest.week_no}.ics",
                mime="text/calendar",
                type="primary",
                icon=":material/event:",
                help="Universal RFC-5545 .ics calendar file for iOS, Android, Outlook, Mac, and Web",
                key="plan_top_primary_ics",
            )
    with col_top_email:
        if ov.plans and st.button(
            "📧 Email Plan + .ics",
            icon=":material/forward_to_inbox:",
            help="Send all 7 days and universal .ics calendar attachment to your email",
            key="plan_resend_email_btn",
        ):
            ch = services.email_weekly_plan(sid, ov.plans[-1])
            if ch:
                st.toast("Study plan and .ics file sent to your email!", icon=":material/mark_email_read:")
            else:
                st.warning("No email address on file for this account.", icon=":material/warning:")

    show_last_run("Planner agent")
    _plans(sid, ov)

with tab_roadmap:
    from piq import journey as _j
    from piq.config import ROADMAP_DEFAULT_SKILLS
    from piq.services import URL_RE, ServiceError

    me = _j.student(sid)
    handed = st.session_state.get("roadmap_gaps")
    gaps, origin = (handed, "your resume scan") if handed is not None else _j.latest_gaps(sid)
    saved_roadmaps = _j.roadmaps(sid)
    next_rm_week = max(
        [*(r.week for r in saved_roadmaps), *(p.week_no for p in ov.plans)],
        default=0,
    ) + 1

    with st.container(border=True):
        rm_options = list(dict.fromkeys([*gaps, *_j.role_skills(me.target_role)]))
        rm_chosen = st.multiselect(
            "Skills to cover",
            rm_options,
            default=gaps[:ROADMAP_DEFAULT_SKILLS],
            help="Seven days fit up to seven skills well; more are shared across the days.",
            key="unified_rm_skills",
        )
        st.caption(
            f"Suggested from {origin} for {formatter.md_escape(me.target_role)}."
            if gaps
            else "No gaps found yet: pick the skills you want to strengthen."
        )
        rm_label = (
            "Generate my 7-day roadmap"
            if next_rm_week == 1
            else f"Generate week {next_rm_week} roadmap"
        )
        if st.button(rm_label, type="primary", icon=":material/route:", disabled=not rm_chosen, key="unified_rm_gen"):
            try:
                with st.spinner("The planner agent is writing your 7-day roadmap…"):
                    roadmap, saved_rm = _j.build_roadmap(sid, rm_chosen)
                st.session_state["roadmap_unsaved"] = None if saved_rm else roadmap
                st.session_state.pop("roadmap_gaps", None)
                st.toast(f"Week {roadmap.week} roadmap generated and synced to Weekly Plan!", icon=":material/task_alt:")
                st.rerun()
            except ServiceError as e:
                st.error(formatter.md_escape(str(e)), icon=":material/error:")

    unsaved_rm = st.session_state.get("roadmap_unsaved")
    all_rms = [*saved_roadmaps, *([unsaved_rm] if unsaved_rm else [])]
    if not all_rms:
        st.caption("No detailed skill-gap roadmap yet. Choose skills above to generate one.")
    else:
        rm_weeks = [r.week for r in all_rms]
        picked_rm_week = st.segmented_control(
            "Roadmap Week", rm_weeks, default=rm_weeks[-1], format_func=lambda w: f"Week {w}", key="unified_rm_week"
        )
        cur_rm = next(r for r in all_rms if r.week == (picked_rm_week or rm_weeks[-1]))
        rm_plan = cur_rm.plan
        total_rm = sum(len(d.micro_tasks) for d in rm_plan.days)
        st.subheader(
            f"Week {cur_rm.week}: {formatter.md_escape(', '.join(rm_plan.addressed_gaps[:4]))}",
            anchor=False,
        )
        st.markdown(formatter.md_escape(rm_plan.summary))
        rm_progress = st.empty()
        rm_done: set[str] = set()
        for day in rm_plan.days:
            with st.container(border=True):
                st.markdown(
                    f"**Day {day.day} · {formatter.md_escape(day.focus_skill)}** "
                    f":gray[· about {day.estimated_hours:g} h]"
                )
                st.markdown("\n".join(f"- {formatter.md_escape(o)}" for o in day.learning_objectives))
                links = [
                    f"[{formatter.md_escape(r.title)}]({r.url})"
                    if URL_RE.match(r.url)
                    else formatter.md_escape(r.title)
                    for r in day.resources
                ]
                if links:
                    st.markdown(":material/menu_book: " + " · ".join(links))
                for n, task in enumerate(day.micro_tasks):
                    tkey = _j.task_key(day.day, n)
                    if st.checkbox(
                        formatter.md_escape(task),
                        value=tkey in cur_rm.done,
                        key=f"unified_rm{cur_rm.week}:{tkey}",
                        disabled=cur_rm is unsaved_rm,
                    ):
                        rm_done.add(tkey)
        rm_progress.progress(
            len(rm_done) / total_rm if total_rm else 0,
            text=f"{len(rm_done)} of {total_rm} tasks done",
        )
        if cur_rm is not unsaved_rm and rm_done != set(cur_rm.done):
            if _j.set_tasks_done(sid, cur_rm.week, rm_done):
                st.toast("Roadmap & Weekly Plan progress synced!", icon=":material/check_circle:")
                st.rerun()

with tab_library:
    _library(profile.target_role, ov.weakness)
