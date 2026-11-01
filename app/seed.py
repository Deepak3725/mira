"""Demo seed data: four fictional users, each with one personal twin.

All people, companies, and contact details here are fictional and exist only
for the hackathon demo. Run once (idempotent)::

    python -m app.seed
"""

from __future__ import annotations

from pathlib import Path

from .db import (
    create_twin,
    create_user,
    get_twin_by_user,
    get_user_by_email,
    init_db,
)

SEED_USERS: list[dict] = [
    {
        "name": "Deepak",
        "role": "job_seeker",
        "email": "deepak.demo@example.com",
        "password": "mira-deepak-demo",
        "twin": {
            "display_name": "Dee",
            "persona": (
                "Friendly backend developer with 3 years of experience. "
                "Represents Deepak while he looks for his next full-time role."
            ),
            "tone": "warm, concise, professional",
            "avatar_seed": "deepak-companion-01",
            "authorized_fields": [
                "headline", "skills", "experience_years", "location",
                "availability", "portfolio_url", "bio",
            ],
            "profile": {
                "headline": "Backend developer (Python, FastAPI)",
                "skills": ["Python", "FastAPI", "SQLite", "REST APIs"],
                "experience_years": 3,
                "location": "Thimphu",
                "availability": "Open to full-time roles",
                "open_to_remote": True,
                "portfolio_url": "https://example.com/deepak-portfolio",
                "bio": "Builds small, reliable web services and loves clean APIs.",
                # ---- private: never authorized ----
                "phone": "+975-17-000-001",
                "personal_email": "deepak.private@example.com",
                "salary_expectation": "Nu. 45,000/month",
                "home_address": "12 Fictional Lane, Thimphu",
            },
        },
    },
    {
        "name": "Karma",
        "role": "employer",
        "email": "karma.demo@example.com",
        "password": "mira-karma-demo",
        "twin": {
            "display_name": "Kay",
            "persona": (
                "Hiring lead for the fictional Druk Analytics studio. "
                "Screens candidates carefully and recommends, never hires alone."
            ),
            "tone": "direct, courteous, structured",
            "avatar_seed": "karma-companion-02",
            "authorized_fields": [
                "company", "role_hiring", "requirements", "location",
                "contact_channel",
            ],
            "profile": {
                "company": "Druk Analytics (fictional)",
                "role_hiring": "Backend Developer",
                "requirements": ["Python", "APIs", "2+ years experience"],
                "location": "Thimphu (hybrid)",
                "contact_channel": "via MIRA twin chat",
                # ---- private: never authorized ----
                "phone": "+975-17-000-002",
                "personal_email": "karma.private@example.com",
                "hiring_budget_cap": "Nu. 60,000/month",
                "internal_notes": "Need someone before the winter release.",
            },
        },
    },
    {
        "name": "Pema",
        "role": "professional",
        "email": "pema.demo@example.com",
        "password": "mira-pema-demo",
        "twin": {
            "display_name": "Pem",
            "persona": (
                "UX designer exploring new collaborations and full-time "
                "opportunities in product design."
            ),
            "tone": "creative, upbeat, clear",
            "avatar_seed": "pema-companion-03",
            "authorized_fields": [
                "headline", "skills", "experience_years", "location",
                "availability", "portfolio_url",
            ],
            "profile": {
                "headline": "UX designer, design systems & prototyping",
                "skills": ["Figma", "Prototyping", "User research"],
                "experience_years": 4,
                "location": "Paro",
                "availability": "Open to collaborations",
                "open_to_remote": True,
                "portfolio_url": "https://example.com/pema-portfolio",
                # ---- private: never authorized ----
                "phone": "+975-17-000-003",
                "personal_email": "pema.private@example.com",
                "day_rate_floor": "Nu. 8,000/day",
            },
        },
    },
    {
        "name": "Tashi",
        "role": "independent",
        "email": "tashi.demo@example.com",
        "password": "mira-tashi-demo",
        "twin": {
            "display_name": "Tash",
            "persona": (
                "Independent DevOps specialist taking on short infrastructure "
                "and automation projects."
            ),
            "tone": "pragmatic, calm, precise",
            "avatar_seed": "tashi-companion-04",
            "authorized_fields": [
                "headline", "skills", "experience_years", "location",
                "availability",
            ],
            "profile": {
                "headline": "Independent DevOps specialist",
                "skills": ["Docker", "CI/CD", "Linux", "Monitoring"],
                "experience_years": 5,
                "location": "Remote",
                "availability": "2 project slots open",
                "open_to_remote": True,
                # ---- private: never authorized ----
                "phone": "+975-17-000-004",
                "personal_email": "tashi.private@example.com",
                "client_list": ["Fictional Client A", "Fictional Client B"],
            },
        },
    },
]


def seed_demo_data(path: Path | str | None = None) -> dict[str, int]:
    """Insert demo users + twins if missing. Returns counts created/skipped."""
    from .auth import hash_password
    from .db import get_connection, set_user_password

    # NOTE: callers must init_db() first; seeding never wipes (Postgres
    # test init drops the schema, so an init here would erase prior seeds).
    conn = get_connection(path)
    created = skipped = 0
    try:
        for entry in SEED_USERS:
            existing = get_user_by_email(conn, entry["email"])
            if existing is not None:
                if not existing["password_hash"]:
                    set_user_password(conn, existing["id"], hash_password(entry["password"]))
                if get_twin_by_user(conn, existing["id"]) is None:
                    twin = entry["twin"]
                    create_twin(
                        conn,
                        user_id=existing["id"],
                        display_name=twin["display_name"],
                        persona=twin["persona"],
                        tone=twin["tone"],
                        profile=twin["profile"],
                        authorized_fields=twin["authorized_fields"],
                        avatar_seed=twin["avatar_seed"],
                    )
                skipped += 1
                continue
            user_id = create_user(
                conn,
                name=entry["name"],
                role=entry["role"],
                email=entry["email"],
                password_hash=hash_password(entry["password"]),
            )
            twin = entry["twin"]
            create_twin(
                conn,
                user_id=user_id,
                display_name=twin["display_name"],
                persona=twin["persona"],
                tone=twin["tone"],
                profile=twin["profile"],
                authorized_fields=twin["authorized_fields"],
                avatar_seed=twin["avatar_seed"],
            )
            created += 1
        conn.commit()
    finally:
        conn.close()
    return {"created": created, "skipped": skipped}


OPPORTUNITIES: list[dict] = [
    {
        "owner_email": "karma.demo@example.com",
        "kind": "job",
        "category": "job",
        "title": "Backend Developer",
        "description": (
            "Build and maintain small Python APIs for a fictional analytics "
            "studio. Hybrid role based in Thimphu."
        ),
        "required_skills": ["Python", "REST APIs", "SQLite", "GraphQL"],
        "preferred_experience_years": 2,
        "location": "Thimphu",
        "remote_ok": False,
        "status": "open",
    },
    {
        "owner_email": "karma.demo@example.com",
        "kind": "job",
        "category": "job",
        "title": "API Integration Specialist",
        "description": "Closed listing kept to verify closed items never surface.",
        "required_skills": ["Python"],
        "preferred_experience_years": 1,
        "location": "Thimphu",
        "remote_ok": False,
        "status": "closed",
    },
    {
        "owner_email": "tashi.demo@example.com",
        "kind": "gig",
        "category": "service",
        "title": "CI/CD Setup Gig",
        "description": (
            "Containerize a small service and set up CI/CD. Fully remote, "
            "two-week gig."
        ),
        "required_skills": ["Docker", "CI/CD"],
        "preferred_experience_years": 3,
        "location": "Remote",
        "remote_ok": True,
        "status": "open",
    },
    {
        "owner_email": "pema.demo@example.com",
        "kind": "collaboration",
        "category": "collaboration",
        "title": "Design System Collaboration",
        "description": (
            "Co-design a small component library and prototype flows. "
            "Remote-friendly."
        ),
        "required_skills": ["Figma", "Prototyping"],
        "preferred_experience_years": 2,
        "location": "Paro",
        "remote_ok": True,
        "status": "open",
    },
]


def seed_demo_opportunities(path: Path | str | None = None) -> dict[str, int]:
    """Insert demo opportunities if missing. Returns counts created/skipped."""
    from .db import (
        create_opportunity,
        get_connection,
        get_opportunity_by_owner_title,
    )

    # NOTE: callers must init_db() first; seeding never wipes (Postgres
    # test init drops the schema, so an init here would erase prior seeds).
    conn = get_connection(path)
    created = skipped = 0
    try:
        for entry in OPPORTUNITIES:
            owner = get_user_by_email(conn, entry["owner_email"])
            if owner is None:
                continue
            existing = get_opportunity_by_owner_title(conn, owner["id"], entry["title"])
            if existing is not None:
                skipped += 1
                continue
            create_opportunity(
                conn,
                owner_user_id=owner["id"],
                kind=entry["kind"],
                title=entry["title"],
                description=entry["description"],
                required_skills=entry["required_skills"],
                preferred_experience_years=entry["preferred_experience_years"],
                location=entry["location"],
                remote_ok=entry["remote_ok"],
                status=entry["status"],
                category=entry.get("category", ""),
                attributes=entry.get("attributes"),
            )
            created += 1
        conn.commit()
    finally:
        conn.close()
    return {"created": created, "skipped": skipped}


DRIVER_USERS: list[dict] = [
    {
        "name": "Dorji",
        "role": "independent",
        "email": "dorji.demo@example.com",
        "password": "mira-dorji-demo",
        "twin": {
            "display_name": "Dor",
            "persona": "Friendly city taxi driver twin for the fictional Dorji.",
            "tone": "warm, helpful, brief",
            "avatar_seed": "dorji-companion-05",
            "authorized_fields": ["headline", "rate", "vehicle", "service_area", "availability"],
            "profile": {
                "headline": "City taxi driver (Thimphu & Paro valley)",
                "rate": "Nu. 400 per city trip",
                "vehicle": "Sedan, 3 seats",
                "service_area": ["Thimphu", "GCIT", "Paro"],
                "availability": "Weekdays 7 AM - 9 PM",
                "phone": "+975-17-000-005",
                "personal_email": "dorji.private@example.com",
            },
        },
    },
    {
        "name": "Sonam",
        "role": "independent",
        "email": "sonam.demo@example.com",
        "password": "mira-sonam-demo",
        "twin": {
            "display_name": "Son",
            "persona": "Premium taxi driver twin for the fictional Sonam.",
            "tone": "polite, professional",
            "avatar_seed": "sonam-companion-06",
            "authorized_fields": ["headline", "rate", "vehicle", "service_area"],
            "profile": {
                "headline": "Premium taxi (airport & long trips)",
                "rate": "Nu. 650 per trip",
                "vehicle": "SUV, 4 seats",
                "service_area": ["Paro", "Thimphu", "Airport"],
                "phone": "+975-17-000-006",
                "personal_email": "sonam.private@example.com",
            },
        },
    },
]

DRIVER_OFFERINGS: list[dict] = [
    {
        "owner_email": "dorji.demo@example.com",
        "kind": "gig",
        "category": "transport",
        "title": "City Taxi (Thimphu valley)",
        "description": "Sedan taxi for city trips around Thimphu, GCIT and Paro.",
        "required_skills": [],
        "preferred_experience_years": None,
        "location": "Thimphu",
        "remote_ok": False,
        "status": "open",
        "attributes": {
            "price": 400,
            "currency": "Nu.",
            "service_area": ["Thimphu", "GCIT", "Paro"],
            "availability": ["weekdays", "7am-9pm"],
            "tags": ["taxi", "sedan", "city"],
        },
    },
    {
        "owner_email": "sonam.demo@example.com",
        "kind": "gig",
        "category": "transport",
        "title": "Premium Taxi (long trips)",
        "description": "SUV taxi for airport runs and longer valley trips.",
        "required_skills": [],
        "preferred_experience_years": None,
        "location": "Paro",
        "remote_ok": False,
        "status": "open",
        "attributes": {
            "price": 650,
            "currency": "Nu.",
            "service_area": ["Paro", "Thimphu", "Airport"],
            "availability": ["anytime"],
            "tags": ["taxi", "suv", "airport"],
        },
    },
]


def seed_demo_drivers(path: Path | str | None = None) -> dict[str, int]:
    """Insert fictional driver twins + taxi offerings if missing (idempotent)."""
    from .auth import hash_password
    from .db import (
        create_opportunity,
        get_connection,
        get_opportunity_by_owner_title,
        set_twin_conditions,
    )

    # NOTE: callers must init_db() first; seeding never wipes (Postgres
    # test init drops the schema, so an init here would erase prior seeds).
    conn = get_connection(path)
    created = skipped = 0
    try:
        for entry in DRIVER_USERS:
            existing = get_user_by_email(conn, entry["email"])
            if existing is None:
                user_id = create_user(
                    conn, name=entry["name"], role=entry["role"],
                    email=entry["email"],
                    password_hash=hash_password(entry["password"]))
                twin = entry["twin"]
                twin_id = create_twin(
                    conn, user_id=user_id, display_name=twin["display_name"],
                    persona=twin["persona"], tone=twin["tone"],
                    profile=twin["profile"],
                    authorized_fields=twin["authorized_fields"],
                    avatar_seed=twin["avatar_seed"])
                set_twin_conditions(conn, twin_id, {"auto_answer": True})
                created += 1
            else:
                skipped += 1
        for entry in DRIVER_OFFERINGS:
            owner = get_user_by_email(conn, entry["owner_email"])
            if owner is None:
                continue
            if get_opportunity_by_owner_title(conn, owner["id"], entry["title"]) is not None:
                skipped += 1
                continue
            create_opportunity(
                conn, owner_user_id=owner["id"], kind=entry["kind"],
                title=entry["title"], description=entry["description"],
                required_skills=entry["required_skills"],
                preferred_experience_years=entry["preferred_experience_years"],
                location=entry["location"], remote_ok=entry["remote_ok"],
                status=entry["status"], category=entry["category"],
                attributes=entry["attributes"])
            created += 1
        conn.commit()
    finally:
        conn.close()
    return {"created": created, "skipped": skipped}


def seed_demo_conversation(path: Path | str | None = None) -> dict[str, int]:
    """Create one public demo twin chat (Deepak x Karma). Idempotent."""
    from . import conversations as engine
    from .db import get_connection, get_opportunity_by_owner_title, get_twin_by_user

    # NOTE: callers must init_db() first; seeding never wipes (Postgres
    # test init drops the schema, so an init here would erase prior seeds).
    conn = get_connection(path)
    try:
        existing = conn.execute(
            "SELECT id FROM conversations WHERE is_demo = 1 LIMIT 1").fetchone()
        if existing is not None:
            return {"created": 0, "skipped": 1}
        deepak = get_user_by_email(conn, "deepak.demo@example.com")
        karma = get_user_by_email(conn, "karma.demo@example.com")
        if deepak is None or karma is None:
            return {"created": 0, "skipped": 0}
        twin_d = get_twin_by_user(conn, deepak["id"])
        twin_k = get_twin_by_user(conn, karma["id"])
        opp = get_opportunity_by_owner_title(conn, karma["id"], "Backend Developer")
        if twin_d is None or twin_k is None or opp is None:
            return {"created": 0, "skipped": 0}
        cid = engine.create_conversation(
            conn, twin_a_id=twin_d["id"], twin_b_id=twin_k["id"],
            created_by_user_id=deepak["id"], opportunity_id=opp["id"],
            purpose="recruitment", max_turns=8, is_demo=True)
        for i in range(6):
            engine.step_conversation(
                conn, cid, deepak["id"] if i % 2 == 0 else karma["id"])
        return {"created": 1, "skipped": 0}
    finally:
        conn.close()


DEMO_MEMORIES: list[dict] = [
    # Dee — junior backend developer looking for a full-time role.
    {"email": "deepak.demo@example.com", "status": "approved",
     "category": "skill",
     "content": ("Built REST APIs with Python and FastAPI for three years, "
                 "including auth and clean versioning.")},
    {"email": "deepak.demo@example.com", "status": "approved",
     "category": "experience",
     "content": ("Shipped an internal dashboard backed by SQLite that the "
                 "support team still uses daily.")},
    {"email": "deepak.demo@example.com", "status": "approved",
     "category": "preference",
     "content": "Prefers hybrid roles in Thimphu; open to remote for the right team."},
    {"email": "deepak.demo@example.com", "status": "approved",
     "category": "project",
     "content": "Portfolio project: a small job-board API with tests and docs."},
    {"email": "deepak.demo@example.com", "status": "pending",
     "category": "skill",
     "content": "Currently learning basic Docker to containerize personal projects."},
    # Kay — hiring lead for a fictional studio.
    {"email": "karma.demo@example.com", "status": "approved",
     "category": "preference",
     "content": ("Hiring bar: 2+ years of Python, working REST APIs, and clear "
                 "communication; culture fit matters.")},
    {"email": "karma.demo@example.com", "status": "approved",
     "category": "experience",
     "content": "Druk Analytics is a fictional ten-person studio; backend team of three."},
    {"email": "karma.demo@example.com", "status": "approved",
     "category": "general",
     "content": ("Interview process: twin-chat screening first, then one human "
                 "conversation before any offer.")},
    # Pem — product designer open to collaborations.
    {"email": "pema.demo@example.com", "status": "approved",
     "category": "skill",
     "content": ("Product designer: Figma design systems, prototyping, and user "
                 "research for four years.")},
    {"email": "pema.demo@example.com", "status": "approved",
     "category": "project",
     "content": ("Led a component-library redesign used across three fictional "
                 "client apps.")},
    {"email": "pema.demo@example.com", "status": "approved",
     "category": "preference",
     "content": "Open to collaborations; prefers remote-friendly teams."},
    # Tash — independent DevOps specialist.
    {"email": "tashi.demo@example.com", "status": "approved",
     "category": "skill",
     "content": ("DevOps specialist: Docker, CI/CD pipelines, Linux servers, "
                 "and monitoring for five years.")},
    {"email": "tashi.demo@example.com", "status": "approved",
     "category": "experience",
     "content": ("Set up zero-downtime deploys for fictional clients; "
                 "on-call friendly.")},
    {"email": "tashi.demo@example.com", "status": "approved",
     "category": "preference",
     "content": "Takes short infrastructure gigs; two project slots currently open."},
]


def seed_demo_memories(path: Path | str | None = None) -> dict[str, int]:
    """Insert fictional twin memories if missing. Returns counts created/skipped.

    Approved entries stand in for owner-approved knowledge for these
    fictional demo accounts; one pending entry per demo flow is kept so
    the review queue stays demonstrable.
    """
    from .db import get_connection
    from .memories import create_memory

    # NOTE: callers must init_db() first; seeding never wipes (Postgres
    # test init drops the schema, so an init here would erase prior seeds).
    conn = get_connection(path)
    created = skipped = 0
    try:
        for entry in DEMO_MEMORIES:
            owner = get_user_by_email(conn, entry["email"])
            twin = get_twin_by_user(conn, owner["id"]) if owner else None
            if twin is None:
                continue
            mem_id = create_memory(
                conn, twin_id=twin["id"], content=entry["content"],
                source="demo-seed", status=entry["status"],
                category=entry["category"])
            if mem_id is None:
                skipped += 1
            else:
                created += 1
        conn.commit()
    finally:
        conn.close()
    return {"created": created, "skipped": skipped}


def refuse_in_production() -> None:
    from .config import settings as _settings

    demo_on = getattr(_settings, "demo_mode", False) is True
    if _settings.app_env == "production" and not demo_on:
        raise SystemExit("Refusing to seed demo data in production.")


if __name__ == "__main__":
    refuse_in_production()
    init_db()
    users = seed_demo_data()
    opps = seed_demo_opportunities()
    drivers = seed_demo_drivers()
    mems = seed_demo_memories()
    demo = seed_demo_conversation()
    print(f"seeded demo users: {users['created']} created, {users['skipped']} skipped")
    print(f"seeded opportunities: {opps['created']} created, {opps['skipped']} skipped")
    print(f"seeded drivers: {drivers['created']} created, {drivers['skipped']} skipped")
    print(f"seeded memories: {mems['created']} created, {mems['skipped']} skipped")
    print(f"seeded demo chat: {demo['created']} created, {demo['skipped']} skipped")
