"""Home & Dashboard: unified overview combining today's focus, study streak,
analytics charts, quiz/interview/plan history, calendar export, and critic telemetry."""

import streamlit as st

from core import calendar_export
from piq import charts, formatter, services, ui
from piq.ui import Stat, empty_state, stat_row

HOW_IT_WORKS = (
    ("upload_file", "Profile", "Upload your resume. Your skills are matched to a target role."),
    ("calendar_month", "Plan", "Get seven days of study, most of it on your weakest topics."),
    ("quiz", "Quiz", "Take five-question quizzes. A critic checks every question first."),
    ("autorenew", "Adapt", "Your scores reshape next week's plan, and the loop goes on."),
)


def _go(page: str, **presets: str) -> None:
    """Open another page, preselecting its widgets through session state."""
    st.session_state.update(presets)
    st.switch_page(page)


student = ui.current_student()
if student is None:
    ui.page_header(
        "Placement prep that adapts to you",
        "Skill Scout Nexus reads your resume, plans a week of study, quizzes you and rewrites next "
        "week's plan from your scores. A critic checks every plan, quiz and interview question "
        "before you see it.",
    )
    with st.container(horizontal=True):
        build_resume = st.button(
            "Build ATS resume from scratch",
            type="primary",
            icon=":material/badge:",
            key="home_build_resume",
        )
        start = st.button("Start with your resume", icon=":material/upload_file:", key="start")
        sample = st.button("Try it with a sample resume", icon=":material/description:")
    if build_resume:
        _go("views/resume_builder.py")
    if start:
        _go("views/resume_builder.py")
    if sample:
        _go("views/onboard.py", onboard_sample=services.default_sample())
    st.subheader("How it works", anchor=False)
    for column, (n, (icon, title, text)) in zip(
        st.columns(4, gap="medium"), enumerate(HOW_IT_WORKS, 1), strict=True
    ):
        column.markdown(f":primary[:material/{icon}:] **{n}. {title}**  \n{text}")

    st.divider()
    st.subheader("Explore Career Pathways & Disciplines", anchor=False)
    st.caption(
        "Open to every stream: Engineering, Commerce, Law, Management, Healthcare and Government."
    )
    from piq import resume_builder

    domains = resume_builder.list_domains()
    for row_start in range(0, len(domains), 2):
        row_domains = domains[row_start : row_start + 2]
        cols = st.columns(2, gap="medium")
        for col, d in zip(cols, row_domains, strict=False):
            with col, st.container(border=True):
                st.markdown(f":primary[:material/{d.get('icon', 'work')}:] **{d['name']}**")
                st.caption(d.get("description", ""))
                role_names = [r["name"] for r in d.get("roles", [])]
                if role_names:
                    st.write(", ".join(f"`{r}`" for r in role_names[:3]))
                if st.button(
                    f"Build Resume: {d['name']}", key=f"btn_dom_{d['id']}", icon=":material/badge:"
                ):
                    _go("views/resume_builder.py")
    st.stop()

sid = student["id"]
profile = services.current_profile(sid)
if profile is None:
    ui.page_header(
        f"Welcome, {formatter.md_escape(student['name'])}",
        "Two ways to begin your placement journey.",
    )
    ui.no_profile_card("home_start")
    st.stop()

try:
    with st.skeleton(height=420):
        overview = services.home(sid, profile)
except services.ServiceError as e:
    if ui.empty_state(
        "error", "Your overview did not load", formatter.md_escape(str(e)), "Try again"
    ):
        st.rerun()
    st.stop()

domain = services.domain_of_role(profile.target_role)
ui.page_header(
    f"Hello, {formatter.md_escape(profile.name)}",
    f"Unified Home & Dashboard for {formatter.md_escape(profile.target_role)} "
    f"({formatter.md_escape(domain.name)}): today's focus, skill gaps, analytics, and history.",
)

if (snap := st.session_state.get("snapshot")) and (snap.roadmap_week or snap.quizzes):
    st.info(formatter.welcome_back_md(snap), icon=":material/history:")

ui.org_progress_card(sid)  # an organization's student: their stage against its pass marks

# Load full dashboard telemetry alongside Home overview
try:
    d = services.dashboard(sid, profile)
except services.ServiceError:
    d = None

if d is not None:
    correct, answered = d.answered
    stat_row(
        [
            Stat("Quizzes taken", str(d.quizzes_taken)),
            Stat(
                "Questions answered",
                str(answered),
                f"{correct / answered:.0%} right" if answered else None,
                delta_color="off",
                delta_arrow="off",
            ),
            Stat("Interview sessions", str(len(d.sessions))),
            Stat(
                "Study streak",
                formatter.days(overview.streak.current),
                help=f"Days in a row with a quiz or an interview. Longest: "
                f"{formatter.days(overview.streak.longest)}.",
            ),
        ]
    )

action = overview.next_action
main, side = st.columns([2, 1], gap="medium")
with main:
    with st.container(border=True):
        st.markdown(f":primary[:material/{action.icon}:] **Today's focus**")
        st.subheader(action.label, anchor=False)
        st.markdown(formatter.md_escape(action.reason))
        if action.topic:
            ui.topic_chip(action.topic)
        if st.button(action.cta, type="primary", icon=f":material/{action.icon}:", key="next"):
            _go(action.page, **({"quiz_topic": action.topic} if action.topic else {}))
    st.subheader(formatter.weakness_headline(overview.weakness), anchor=False)
    st.vega_lite_chart(
        spec=charts.weakness_spec(overview.weakness),
        width="stretch",
        alt=formatter.weakness_alt(overview.weakness),
    )
    st.caption(
        f"{formatter.weakness_caption()} Your role's topics in {formatter.md_escape(domain.name)}, "
        "plus any other topic of it you have quizzed."
    )

with side:
    with st.container(border=True):
        st.markdown(f"**ATS Resume & Skills:** `{profile.target_role}`")
        total = len(profile.matched_skills) + len(profile.missing_skills)
        st.caption(f"{len(profile.matched_skills)} of {total} required skills are on your resume.")
        st.markdown(formatter.streak_md(overview.streak.current, overview.streak.longest))
        if overview.week_done:
            st.markdown(formatter.days_done_md(*overview.week_done))
        if st.button(
            "Open Resume & Skills Hub", icon=":material/badge:", key="home_update_resume"
        ):
            _go("views/resume_builder.py")
    with st.container(border=True):
        st.markdown("**Getting started**")
        st.markdown(
            "  \n".join(formatter.checklist_line(i.label, i.done) for i in overview.checklist)
        )
    with st.container(border=True):
        st.markdown("**Recent activity**")
        if not overview.recent:
            st.caption("Nothing yet. Your plans, quizzes and interviews will appear here.")
        for item in overview.recent:
            when = formatter.relative_day(item.at)
            st.markdown(f"{formatter.md_escape(item.text)}  \n:small[:gray[{when}]]")

# --- Integrated Dashboard Sections ---
if d is not None:
    st.divider()
    today = formatter.local_today()
    recent_days = sum(1 for day in overview.streak.days if (today - day).days < 7 * 12)
    st.subheader(formatter.activity_headline(recent_days, overview.streak.longest), anchor=False)
    st.vega_lite_chart(
        spec=charts.activity_heatmap_spec(overview.streak.days, today),
        width="stretch",
        alt=f"Calendar of the last 12 weeks: {formatter.days(recent_days)} with study.",
    )
    st.caption("One square per day, a column per week, oldest on the left; a stronger teal is busier.")

    syl = d.syllabus
    if syl is not None:
        with st.container(border=True):
            st.subheader("Your syllabus", anchor=False)
            st.markdown(formatter.syllabus_line(syl.domain.name, syl.role))
            chosen = [t for t in syl.tracked if t not in syl.focus]
            columns = st.columns(3 if chosen else 2, gap="medium")
            groups = [("For your role", syl.focus)]
            if chosen:
                groups.append(("Chosen by you", chosen))
            groups.append(("Not tried yet", syl.others))
            for column, (title, topics) in zip(columns, groups, strict=True):
                with column:
                    st.markdown(f"**{title}**")
                    if topics:
                        st.html(formatter.topic_chips_html(topics))
                    else:
                        st.caption("None: you have tried every topic of your domain.")

    st.divider()
    mastery = d.mastery
    st.subheader(formatter.topic_scores_headline({m.topic: m.scores for m in mastery}), anchor=False)
    if not d.quiz_history:
        st.caption("No quizzes yet. Each topic's scores appear here after its first quiz.")
    else:
        for start in range(0, len(mastery), 3):
            row = mastery[start : start + 3]
            for column, m in zip(st.columns(3, gap="small"), row, strict=False):
                with column, st.container(border=True, gap="small"):
                    st.markdown(formatter.mastery_line(m.topic, m.weakness, m.scores))
                    if m.scores:
                        st.markdown(formatter.trend_md(m.trend))
                        st.vega_lite_chart(
                            spec=charts.score_spark_spec(m.topic, m.scores),
                            width="stretch",
                            alt=f"{m.topic} quiz scores: " + ", ".join(f"{s:.0f}%" for s in m.scores),
                        )

    col_left, col_right = st.columns(2, gap="large")
    with col_left:
        cells = [(r["topic"], r["difficulty"], r["correct"], r["total"]) for r in d.accuracy]
        st.subheader(formatter.accuracy_headline(cells), anchor=False)
        if not cells:
            st.caption("Each quiz adds its questions here, per topic and difficulty.")
        else:
            st.vega_lite_chart(
                spec=charts.accuracy_grid_spec(cells),
                width="stretch",
                alt="Share correct by topic and difficulty.",
            )
        st.subheader("Recent quizzes", anchor=False)
        if not d.runs:
            st.caption("Quizzes you take in Quiz Arena appear here with their score.")
        else:
            st.dataframe(formatter.runs_table(d.runs), hide_index=True, width="stretch")

    with col_right:
        if not d.sessions:
            st.subheader("No interview yet", anchor=False)
            st.caption("Each session's average score appears here after your first mock interview.")
        else:
            averages = [float(s["avg_overall"]) for s in d.sessions]
            st.subheader(formatter.interview_headline(averages), anchor=False)
            st.vega_lite_chart(
                spec=charts.interview_trend_spec(averages, formatter.session_labels(d.sessions)),
                width="stretch",
                alt="Interview averages out of 5.",
            )

    st.divider()
    if not d.plans:
        st.subheader("No study plan yet", anchor=False)
        st.caption("Open Study Plan & Roadmap to generate your 7-day study plan.")
    else:
        weeks = {p.week_no: p.minutes_by_topic() for p in d.plans}
        st.subheader(formatter.week_mix_headline(d.plans), anchor=False)
        st.vega_lite_chart(
            spec=charts.week_mix_spec(weeks), width="stretch", alt=formatter.plan_mix_alt(weeks)
        )
        week, done_days = d.plans_done[-1][0].week_no, len(d.plans_done[-1][1])
        st.markdown(formatter.days_done_md(week, done_days))
        curr_plan = d.plans[-1]
        done_set = d.plans_done[-1][1] if d.plans_done else set()
        with st.container(horizontal=True):
            st.download_button(
                "📥 Download Schedule (.ics)",
                data=calendar_export.generate_plan_ics(curr_plan),
                file_name=f"study-schedule-week-{curr_plan.week_no}.ics",
                mime="text/calendar",
                key="dash_plan_download_ics",
            )
            pending_days = [
                d_ for d_ in sorted(curr_plan.days, key=lambda x: x.day) if d_.day not in done_set
            ]
            next_d = pending_days[0] if pending_days else curr_plan.days[0]
            gcal_title = f"[Skill Scout Nexus] Day {next_d.day}: {next_d.topic}"
            gcal_desc = (
                f"Day {next_d.day} Study Plan - Week {curr_plan.week_no}\n"
                f"Topic: {next_d.topic} ({next_d.duration_min} min)\n"
                f"Practice Goal: {next_d.practice_goal}"
            )
            st.link_button(
                f"📅 Day {next_d.day} to GCal",
                url=calendar_export.build_gcal_url(
                    gcal_title, gcal_desc, duration_min=next_d.duration_min
                ),
            )

    with st.expander("Under the hood: agents and critic", icon=":material/smart_toy:"):
        if d.agent_stats:
            st.dataframe(
                formatter.agent_table(d.agent_stats),
                hide_index=True,
                width="stretch",
                column_config={
                    "Rejection rate": st.column_config.ProgressColumn(
                        "Rejection rate", min_value=0, max_value=100, format="%.0f%%"
                    ),
                },
            )
        else:
            st.caption("No agent calls yet.")
        if d.rejections:
            st.markdown("**Recent critic rejections**")
            st.dataframe(formatter.rejections_table(d.rejections), hide_index=True, width="stretch")
