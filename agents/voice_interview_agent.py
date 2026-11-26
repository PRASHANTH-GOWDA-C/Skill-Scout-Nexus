"""Voice Interview Agent for Skill Scout Nexus (Step 5 Mock Voice Interview Architecture).

Responsibilities:
- Converts interview questions into realistic spoken audio using ElevenLabs TTS API
  (`ELEVENLABS_API_KEY`) or Gemini TTS (`GEMINI_API_KEYS`), else a local or browser voice.
- Provides fallback to browser SpeechSynthesis / data URI synthetic wavs if API key is not present.
- Generates realistic role-specific behavioral, technical, and situational scenario questions
  tailored across ALL 12 career domains and 6 streams (Engineering: CS, ECE, Mech, Civil, EEE,
  Biotech; Healthcare; Commerce; Law; Management; Central & State Government).
- Supports `exclude_questions` to guarantee questions never repeat within a session.
"""

from __future__ import annotations

import base64
import logging
import random
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from core import speech_router

log = logging.getLogger("agents.voice_interview_agent")
Category = Literal["technical", "behavioral", "situational"]
CATEGORIES: tuple[Category, ...] = ("technical", "behavioral", "situational")


class VoiceInterviewQuestion(BaseModel):
    category: Category
    stream: str = Field(
        description="Target discipline / stream (Engineering, Healthcare, Commerce, Law, Management, Government)"
    )
    role: str = Field(description="Specific target role")
    question_text: str = Field(min_length=15, description="Full spoken interview question")
    intent: str = Field(description="What competencies the interviewer is evaluating")
    scenario_context: str = Field(default="", description="Industry situational setup or backdrop")
    audio_base64: str | None = Field(
        default=None, description="MP3 audio base64 payload if synthesized"
    )
    tts_source: str = Field(
        default="browser_fallback", description="elevenlabs | gemini | pyttsx3 | browser_fallback"
    )


def _sc(category: Category, scenario: str, question: str, intent: str) -> dict[str, str]:
    return {"category": category, "scenario": scenario, "question": question, "intent": intent}


# --- Curated Multi-Domain Scenario Banks (all 12 domains, multiple per category) ---
DOMAIN_BANK_BY_ID: dict[str, list[dict[str, str]]] = {
    "cs": [
        _sc(
            "technical",
            "High-throughput Distributed Architecture",
            "Imagine our e-commerce checkout service experiences a sudden 10x traffic spike during a flash sale, causing cascading database connection timeouts. How would you redesign the order ingestion pipeline and rate limiting to prevent database saturation while guaranteeing zero lost transactions?",
            "Evaluates distributed systems architecture, asynchronous queues, caching, and graceful degradation.",
        ),
        _sc(
            "technical",
            "Database Indexing & Query Optimization",
            "A reporting query joining orders and users suddenly slows from 40 milliseconds to 8 seconds as table rows cross 50 million. Walk me through how you inspect the query execution plan, design composite indexes, and avoid write amplification.",
            "Assesses SQL indexing internals, B-tree selectivity, and database performance profiling.",
        ),
        _sc(
            "situational",
            "Live Production Outage Debugging",
            "A critical microservice deployed 30 minutes ago is intermittently returning HTTP 500 errors for 5% of active users, with elevated CPU and garbage collection pauses. Walk me step-by-step through how you isolate whether this is a memory leak, a deadlock, or a downstream dependency failure.",
            "Assesses systematic root-cause analysis, telemetry interpretation, and incident triage.",
        ),
        _sc(
            "situational",
            "API Security & Rate Abuse Incident",
            "You notice abnormal bursts of authentication requests hitting a public REST endpoint from rotating IP addresses, driving up latency for legitimate users. How do you mitigate the abuse immediately and harden the API long term?",
            "Tests practical API security, token bucket rate limiting, and defensive engineering.",
        ),
        _sc(
            "behavioral",
            "Technical Disagreement on Architecture",
            "Tell me about a time you strongly disagreed with a teammate's architectural or library choice on a software project. How did you validate your counter-proposal with evidence, and how did you align the team?",
            "Evaluates technical persuasion, maturity, and collaborative consensus building.",
        ),
        _sc(
            "behavioral",
            "Learning from a Production or Project Mistake",
            "Describe a bug or design flaw you introduced that broke a build, test suite, or deployment. How did you recover quickly and what safeguard did you put in place afterwards?",
            "Evaluates ownership, debugging discipline, and blameless engineering growth.",
        ),
    ],
    "ece": [
        _sc(
            "technical",
            "Embedded Interrupts & RTOS Timing",
            "In an embedded microcontroller firmware handling both high-frequency sensor DMA interrupts and a communication task over SPI, you observe missed deadlines and jitter. How do you partition ISRs versus RTOS tasks and prevent priority inversion?",
            "Evaluates real-time embedded architecture, interrupt latency, and RTOS synchronization.",
        ),
        _sc(
            "technical",
            "ASIC/FPGA Timing Closure",
            "During static timing analysis of an FPGA pipeline, you encounter a setup-time violation on a critical combinatorial path. What architectural and physical synthesis techniques would you apply to meet clock closure?",
            "Tests digital VLSI design, pipelining, retiming, and timing constraints.",
        ),
        _sc(
            "situational",
            "Hardware-Software Board Bring-Up Fault",
            "A newly fabricated PCB fails to enumerate its I2C peripheral during initial bring-up. Walk me through your systematic bench debugging process using a multimeter, logic analyzer, and oscilloscope.",
            "Assesses hands-on hardware bring-up, signal integrity checks, and fault isolation.",
        ),
        _sc(
            "behavioral",
            "Cross-Team Hardware-Firmware Trade-off",
            "Tell me about a lab or project situation where hardware constraints forced you to redesign your firmware or signal processing algorithm. How did you adapt under deadline pressure?",
            "Evaluates hardware-software co-design adaptability and teamwork.",
        ),
    ],
    "mech": [
        _sc(
            "technical",
            "Thermal Management & Heat Transfer",
            "An EV battery pack experiences localized thermal hotspots exceeding safe operating limits during fast charging. How would you analyze the heat rejection path and redesign the cooling manifold for uniform temperature distribution?",
            "Evaluates conduction/convection fundamentals, thermal CFD thinking, and EV design.",
        ),
        _sc(
            "technical",
            "GD&T and Tolerance Stack-Up",
            "A precision shaft-and-bearing housing assembly passes individual part inspections yet jams intermittently on the assembly line. How do you run a tolerance stack-up analysis and apply GD&T datums to eliminate interference?",
            "Tests practical mechanical tolerancing, fits, and manufacturability.",
        ),
        _sc(
            "situational",
            "Shop-Floor Defect Spike (Six Sigma)",
            "Defect rates on a CNC machining cell jump from 0.4% to 4.2% overnight without a drawing change. Walk me through your DMAIC root-cause investigation on the shop floor.",
            "Assesses statistical process control, Six Sigma triage, and manufacturing rigor.",
        ),
        _sc(
            "behavioral",
            "Safety & Design Review Pushback",
            "Describe a time you spotted a structural, ergonomic, or safety concern in a mechanical prototype that others wanted to ship quickly. How did you justify the fix?",
            "Evaluates engineering ethics, safety ownership, and evidence-based persuasion.",
        ),
    ],
    "civil": [
        _sc(
            "technical",
            "RCC Structural Design & Detailing",
            "When designing a multi-storey reinforced concrete frame in a high seismic zone, why do we enforce strong-column weak-beam hierarchy and ductile confining reinforcement at beam-column joints?",
            "Evaluates structural mechanics, seismic codes (IS 456 / IS 13920), and detailing judgment.",
        ),
        _sc(
            "technical",
            "Foundation Settlement & Soil Bearing",
            "Site borehole logs reveal soft compressible clay down to 8 meters beneath a proposed commercial building. Compare shallow raft foundations versus pile foundations for controlling differential settlement.",
            "Tests geotechnical engineering judgment and soil-structure interaction.",
        ),
        _sc(
            "situational",
            "Construction Site Delay & Quality Control",
            "Ready-mix concrete arriving at site fails the target slump test and ambient temperatures are high, while a critical slab pour is scheduled. What steps do you take to protect structural quality without halting site logistics recklessly?",
            "Assesses field QA/QC authority, CPM scheduling awareness, and site safety.",
        ),
        _sc(
            "behavioral",
            "Coordinating Contractors Under Tight Timelines",
            "Tell me about a project or site coordination challenge where conflicting schedules between trades threatened a milestone. How did you align everyone safely?",
            "Evaluates project coordination, communication, and conflict resolution.",
        ),
    ],
    "eee": [
        _sc(
            "technical",
            "Power Electronics & Inverter Harmonics",
            "A 3-phase solar inverter feeding an industrial microgrid exhibits elevated Total Harmonic Distortion and overheating in the DC-link capacitors. How do you diagnose switching modulation, filter resonance, and thermal ripple?",
            "Evaluates power converter topologies, PWM harmonics, and filter design.",
        ),
        _sc(
            "technical",
            "Power System Protection Coordination",
            "In a radial distribution feeder with distributed rooftop generation, an upstream breaker is nuisance-tripping before downstream relays clear faults. How do you re-coordinate relay pickup curves and directional protection?",
            "Tests fault analysis, relay grading, and grid stability.",
        ),
        _sc(
            "situational",
            "PLC & Motor Drive Fault on Plant Line",
            "A variable frequency drive controlling a conveyor motor trips repeatedly on overcurrent only under loaded start-up. Walk me through how you isolate mechanical binding, V/f ramp settings, and motor insulation faults.",
            "Assesses industrial automation troubleshooting and electrical safety.",
        ),
        _sc(
            "behavioral",
            "Lockout-Tagout & Electrical Safety Leadership",
            "Describe a time you insisted on strict electrical testing or safety isolation before energizing a circuit, even when others felt confident. What happened?",
            "Evaluates safety culture, discipline, and accountability.",
        ),
    ],
    "biotech": [
        _sc(
            "technical",
            "Bioprocess Scale-Up & Fermentation",
            "When scaling a recombinant protein fermentation from a 2-liter benchtop bioreactor to a 500-liter pilot vessel, product yield drops by 35%. How do oxygen transfer rate (kLa), shear stress, and nutrient feeding explain this, and how do you correct it?",
            "Evaluates bioprocess engineering, mass transfer, and scale-up optimization.",
        ),
        _sc(
            "technical",
            "PCR / Assay Controls & Specificity",
            "Your qPCR assay shows amplification in the no-template negative control across multiple replicates. Walk me through how you trace primer-dimers versus reagent contamination and validate assay specificity.",
            "Tests molecular biology experimental rigor and troubleshooting.",
        ),
        _sc(
            "situational",
            "GMP Deviation & Out-of-Specification Batch",
            "During downstream chromatography purification, an in-process bioburden check exceeds the GMP alert limit. How do you document the deviation, quarantine material, and conduct root-cause CAPA?",
            "Assesses GMP compliance, quality assurance, and regulatory documentation.",
        ),
        _sc(
            "behavioral",
            "Troubleshooting Failed Wet-Lab or Dry-Lab Experiments",
            "Tell me about a time an experiment or bioinformatics pipeline produced unexpected negative or noisy results. How did you systematically verify controls and turn it into a reliable protocol?",
            "Evaluates scientific persistence, reproducibility, and collaboration.",
        ),
    ],
    "health": [
        _sc(
            "situational",
            "Acute Patient Triage & Differential Diagnosis",
            "A 58-year-old patient arrives at triage with acute chest tightness, mild diaphoresis, and borderline blood pressure. Walk me through your immediate stabilization priorities, targeted history, and diagnostic workup to rule out life-threatening causes.",
            "Evaluates clinical prioritization, red-flag recognition, and structured triage.",
        ),
        _sc(
            "technical",
            "Pharmacology & Drug Interaction Safety",
            "An elderly patient with chronic kidney disease and atrial fibrillation is prescribed an NSAID alongside an anticoagulant and an ACE inhibitor. Identify the major pharmacological risks and how you would counsel or adjust therapy.",
            "Tests clinical pharmacology, renal dosing awareness, and medication safety.",
        ),
        _sc(
            "technical",
            "Epidemiological Outbreak Investigation",
            "A district primary health center reports a sudden cluster of acute gastroenteritis cases across three nearby wards within 48 hours. Outline the steps you would take to verify the outbreak, trace the source, and contain spread.",
            "Assesses public health surveillance, outbreak containment, and field coordination.",
        ),
        _sc(
            "behavioral",
            "Empathetic Patient & Family Communication",
            "Describe how you would explain a complex diagnosis or treatment plan with uncertain outcomes to an anxious family while ensuring informed consent and emotional support.",
            "Evaluates clinical empathy, clarity, and ethical communication.",
        ),
    ],
    "commerce": [
        _sc(
            "situational",
            "Financial Audit & Internal Controls",
            "During a statutory year-end audit of a manufacturing client, you notice a material mismatch between vendor ledger balances and physical inventory stock registers, with missing purchase order reconciliations in Q3. How do you design your audit procedure to test for revenue leakage or fictitious entries?",
            "Assesses analytical auditing procedures, internal controls (ICFR), and fraud skepticism.",
        ),
        _sc(
            "technical",
            "GST Filing & Input Tax Credit Dispute",
            "A client receives a notice alleging a discrepancy between GSTR-2B and GSTR-3B regarding Input Tax Credit claimed on capital goods and blocked credits under Section 17(5). How would you reconcile vendor invoices and draft the structured reply?",
            "Tests practical grasp of GST provisions, ITC reconciliation, and tax dispute drafting.",
        ),
        _sc(
            "technical",
            "DCF Valuation & Working Capital Sensitivity",
            "While valuing a fast-growing retail company using a Discounted Cash Flow model, you notice rising revenue alongside negative operating cash flow driven by ballooning receivables. How do you adjust your valuation assumptions and flag credit risk?",
            "Evaluates financial statement linkage, working capital analysis, and valuation rigor.",
        ),
        _sc(
            "behavioral",
            "Financial Reporting Under Tight Deadlines",
            "Describe a scenario where you had to compile variance analysis or quarterly projections with incomplete ledger entries right before a review deadline. How did you protect accuracy and audit compliance?",
            "Tests integrity under pressure, prioritization, and precision.",
        ),
    ],
    "law": [
        _sc(
            "situational",
            "Contractual Breach & Force Majeure Interpretation",
            "Your corporate client entered into a multi-crore supply contract containing a force majeure clause. Due to sudden regulatory export tariffs and supply interruptions, the vendor unilaterally halted shipments and claimed frustration under Section 56 of the Indian Contract Act. How do you advise the client on damages versus arbitration?",
            "Evaluates contract law mastery, commercial risk assessment, and dispute strategy.",
        ),
        _sc(
            "technical",
            "Constitutional Doctrine & Proportionality",
            "In a challenge before the High Court against a sweeping state digital surveillance order, how would you structure arguments applying the proportionality test laid down in the Justice K.S. Puttaswamy judgment under Article 21?",
            "Evaluates constitutional jurisprudence, structured legal reasoning, and oral advocacy.",
        ),
        _sc(
            "technical",
            "M&A Due Diligence & Companies Act Compliance",
            "During legal due diligence on a target private limited company, you uncover unfiled ROC charges and an unsigned related-party board resolution. How do you assess transaction risk and draft protective representations and warranties?",
            "Tests corporate governance, Companies Act compliance, and transactional drafting.",
        ),
        _sc(
            "behavioral",
            "Ethical Client Representation",
            "How would you handle a situation where a commercial client pressures you to omit an inconvenient non-compliance disclosure from a due diligence report to close a deal quickly?",
            "Tests professional ethics, regulatory rigor, and client counsel fortitude.",
        ),
    ],
    "corp": [
        _sc(
            "situational",
            "HR Grievance & Attrition Resolution",
            "Two high-performing team leads from engineering and product have an escalating conflict over project ownership, affecting team morale and retention. How do you run an impartial grievance process, preserve psychological safety, and institute lasting alignment?",
            "Assesses HR mediation, organizational behavior, and structured people leadership.",
        ),
        _sc(
            "technical",
            "Marketing ROI & CAC Breakdown Case Study",
            "Your B2B SaaS company's Customer Acquisition Cost rose 40% over two quarters while Customer Lifetime Value stayed flat, squeezing unit economics. Walk me through your funnel audit across acquisition channels, conversion velocity, and retention.",
            "Tests unit economics literacy, growth funnel diagnostics, and analytical rigor.",
        ),
        _sc(
            "technical",
            "Supply Chain & Working Capital Optimization",
            "A multi-region distributor faces frequent stockouts on top-20 SKUs while holding excess slow-moving inventory in regional warehouses. How would you redesign safety stock, ABC segmentation, and reorder points?",
            "Evaluates operations management, forecasting, and inventory economics.",
        ),
        _sc(
            "behavioral",
            "Managing Resistant Stakeholders",
            "Describe an instance where a cross-functional initiative fell behind because a key stakeholder resisted the new process. How did you align incentives and deliver results without burning bridges?",
            "Evaluates executive communication, influence, and accountable execution.",
        ),
    ],
    "govt_central": [
        _sc(
            "situational",
            "Administrative & Ethical Dilemma (Public Service)",
            "As a district sub-divisional officer, you discover that a flagship welfare subsidy list includes ineligible beneficiaries backed by influential local leaders, while genuinely needy families were excluded. How do you conduct an impartial verification while maintaining administrative stability?",
            "Evaluates public service integrity, administrative tact, and constitutional fairness.",
        ),
        _sc(
            "technical",
            "Constitutional Polity & Federal Governance",
            "Analyze the constitutional balance between central fiscal coordination and state administrative autonomy in implementing nationwide welfare schemes, citing Finance Commission mechanisms.",
            "Tests grasp of Indian Constitution, federalism, and public policy mechanics.",
        ),
        _sc(
            "technical",
            "Macroeconomy & Fiscal Policy",
            "Explain how elevated food inflation alongside slowing private capital expenditure complicates coordination between RBI's monetary policy stance and the Union Budget's fiscal deficit path.",
            "Evaluates macroeconomic clarity, policy trade-offs, and balanced analysis.",
        ),
        _sc(
            "behavioral",
            "Motivation for Public Administration",
            "What personal experience shaped your decision to pursue central civil or government service, and how do you handle sustained scrutiny and ambiguous policy trade-offs calmly?",
            "Assesses emotional maturity, balanced perspective, and public service ethos.",
        ),
    ],
    "govt_state": [
        _sc(
            "situational",
            "District Field Crisis & Disaster Response",
            "Unseasonal flash floods cut off road access to six rural gram panchayats overnight, disrupting power and drinking water supply. Walk me through your first 12-hour coordination plan across revenue, police, health, and panchayat teams.",
            "Evaluates crisis command, inter-departmental coordination, and field pragmatism.",
        ),
        _sc(
            "technical",
            "Local Self-Governance & Rural Schemes",
            "Why do social audits under rural employment and housing schemes often uncover implementation gaps, and how can district administration strengthen Gram Sabha oversight and digital transparency?",
            "Tests knowledge of 73rd Amendment governance, social audit, and scheme delivery.",
        ),
        _sc(
            "technical",
            "Public Order & Legal Procedure",
            "Explain the statutory safeguards and de-escalation principles a magistrate or executive officer must balance when managing an unannounced public protest near critical civic infrastructure.",
            "Evaluates administrative law awareness, proportionality, and civic poise.",
        ),
        _sc(
            "behavioral",
            "Empathy & Citizen Service Delivery",
            "Tell me about a time you resolved a practical problem for a community or team with very limited budget and authority. What did it teach you about last-mile execution?",
            "Assesses resourcefulness, citizen empathy, and grounded execution.",
        ),
    ],
}

# Stream-level fallback mapping (keeps backward compatibility for tests calling stream names directly)
DOMAIN_SCENARIOS: dict[str, list[dict[str, str]]] = {
    "Engineering": DOMAIN_BANK_BY_ID["cs"],
    "Commerce": DOMAIN_BANK_BY_ID["commerce"],
    "Law": DOMAIN_BANK_BY_ID["law"],
    "Management": DOMAIN_BANK_BY_ID["corp"],
    "Healthcare": DOMAIN_BANK_BY_ID["health"],
    "Government": DOMAIN_BANK_BY_ID["govt_central"],
}

STREAMS = tuple(DOMAIN_SCENARIOS)  # the streams with their own scenario bank


def bank_stream(stream: str) -> str:
    """The scenario bank for a catalog stream."""
    return stream if stream in DOMAIN_SCENARIOS else "Engineering"


def synthesize_speech_elevenlabs(text: str, api_key: str | None = None) -> tuple[bytes | None, str]:
    """(audio bytes, the voice that spoke: "elevenlabs" | "gemini" | "pyttsx3") or
    (None, "browser_fallback"). core/speech_router.py does the work: ElevenLabs, Gemini TTS
    or the local voice, with circuit breakers, and no exception ever escapes.
    `api_key=""` asks for the browser voice outright."""
    if api_key == "":
        return None, "browser_fallback"
    speech = speech_router.synthesize(text)
    return (speech.audio, speech.source) if speech.audio else (None, "browser_fallback")


def get_stream_from_role_or_domain(role: str, domain_id: str = "") -> str:
    """Normalizes role/domain into one of the flagship interview streams."""
    r_lower = role.lower()
    d_lower = domain_id.lower()

    if any(k in r_lower or k in d_lower for k in ["law", "legal", "advocate", "counsel"]):
        return "Law"
    if any(
        k in r_lower or k in d_lower for k in ["commerce", "accountant", "audit", "tax", "finance"]
    ):
        return "Commerce"
    if any(
        k in r_lower or k in d_lower
        for k in ["health", "medic", "doctor", "nurse", "pharma", "clinic", "hospital"]
    ):
        return "Healthcare"
    if any(
        k in r_lower or k in d_lower
        for k in ["govt", "government", "upsc", "psc", "civil service", "ssc", "ias", "ips"]
    ):
        return "Government"
    if any(
        k in r_lower or k in d_lower for k in ["management", "hr", "marketing", "business", "corp"]
    ):
        return "Management"
    return "Engineering"


def _resolve_scenarios(target_role: str, stream_name: str, domain_id: str = "") -> list[dict[str, str]]:
    """Resolve the richest scenario list matching the student's domain_id, target_role, or stream."""
    if domain_id and domain_id in DOMAIN_BANK_BY_ID:
        return DOMAIN_BANK_BY_ID[domain_id]
    try:
        from piq import syllabus

        dom = syllabus.domain_of_role(target_role)
        if dom.id in DOMAIN_BANK_BY_ID:
            return DOMAIN_BANK_BY_ID[dom.id]
    except Exception:
        pass
    return DOMAIN_SCENARIOS.get(stream_name, DOMAIN_BANK_BY_ID["cs"])


def generate_voice_interview_question(
    target_role: str,
    stream: str | None = None,
    category: Category | None = None,
    include_audio: bool = True,
    elevenlabs_key: str | None = None,
    custom_question: str | None = None,
    custom_scenario: str | None = None,
    custom_intent: str | None = None,
    domain_id: str = "",
    exclude_questions: Sequence[str] = (),
) -> VoiceInterviewQuestion:
    """Generates a high-fidelity scenario question across all 12 domains and synthesizes audio."""
    stream_name = stream or get_stream_from_role_or_domain(target_role, domain_id=domain_id)

    if custom_question:
        q_text = custom_question
        selected_category = category or "technical"
        scenario_context = custom_scenario or "GitHub Project Architecture Viva"
        intent = (
            custom_intent
            or "Evaluates architectural depth, code quality, and engineering trade-offs "
            "of the candidate's GitHub project."
        )
    else:
        scenarios = _resolve_scenarios(target_role, stream_name, domain_id=domain_id)
        excluded = {" ".join(q.lower().split()) for q in exclude_questions if q}
        unseen = [s for s in scenarios if " ".join(s["question"].lower().split()) not in excluded]
        base_pool = unseen or scenarios

        if category:
            cat_pool = [s for s in base_pool if s["category"] == category]
            selected = random.choice(cat_pool) if cat_pool else random.choice(base_pool)
        else:
            selected = random.choice(base_pool)

        q_text = selected["question"]
        selected_category = selected["category"]
        scenario_context = selected["scenario"]
        intent = selected["intent"]

    audio_b64 = None
    tts_source = "browser_fallback"

    if include_audio:
        audio_bytes, tts_source = synthesize_speech_elevenlabs(q_text, api_key=elevenlabs_key)
        if audio_bytes:
            audio_b64 = base64.b64encode(audio_bytes).decode("ascii")

    return VoiceInterviewQuestion(
        category=selected_category,  # type: ignore
        stream=stream_name,
        role=target_role,
        question_text=q_text,
        intent=intent,
        scenario_context=scenario_context,
        audio_base64=audio_b64,
        tts_source=tts_source,
    )
