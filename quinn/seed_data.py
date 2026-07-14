"""Curated mock dataset for Quinn's inbound path.

Everything here is fake but deliberately *plausible* for Telnyx's market:
B2B telecom / CPaaS (programmable voice & SMS, SIP trunking, number porting,
IoT SIMs, Verify/2FA, fax, inference). The spread of ICP fit is intentional —
strong fits, mid fits, and clear non-fits — so the qualifier has real
decisions to make rather than a uniformly hot list.

Each record carries both layers:
  * inbound_* fields  -> what the prospect self-reported on the form
  * everything else    -> the external enrichment Quinn would fetch live

Keeping them together in one authored list keeps the "story" of each lead
coherent (a VP at a 2M-msg/mo fintech replacing Twilio hangs together).
seed.py splits them into the two tables at insert time.
"""

from __future__ import annotations

# NOTE: created_at values are spread across early July 2026 (today = 2026-07-13)
# to make the "recent inbound queue" feel real.
RECORDS: list[dict] = [
    {
        "name": "Marcus Reyes",
        "email": "marcus.reyes@nimbuspay.io",
        "company": "NimbusPay",
        "role": "VP of Engineering",
        "request_for": (
            "We send OTP and transaction alerts globally and our Twilio bill is "
            "getting out of hand — around 2M SMS/month. Looking for better US + "
            "LATAM deliverability and a Verify API to offload 2FA. Can we get a "
            "volume quote?"
        ),
        "source": "demo_request",
        "created_at": "2026-07-11T14:22:00Z",
        "seniority": "VP",
        "department": "Engineering",
        "dept_headcount": 64,
        "company_employees": 380,
        "industry": "Fintech / Payments",
        "hq_region": "Austin, US",
        "estimated_revenue_usd": "$50M-$100M",
        "current_provider": "Twilio",
        "monthly_volume": "~2M SMS/mo",
        "funding_stage": "Series C",
        "linkedin_url": "https://linkedin.com/in/marcusreyes-eng",
    },
    {
        "name": "Priya Nadar",
        "email": "priya.nadar@loadhaul.com",
        "company": "LoadHaul Logistics",
        "role": "Head of Infrastructure",
        "request_for": (
            "We manage ~12,000 fleet trackers and our current M2M SIM vendor has "
            "poor coverage in rural corridors. Evaluating global IoT SIMs with a "
            "single data plan and an API to manage activation/suspension."
        ),
        "source": "web_form",
        "created_at": "2026-07-10T09:05:00Z",
        "seniority": "Director",
        "department": "Infrastructure / IoT",
        "dept_headcount": 28,
        "company_employees": 1500,
        "industry": "Logistics / Fleet",
        "hq_region": "Rotterdam, NL",
        "estimated_revenue_usd": "$250M-$500M",
        "current_provider": "1NCE",
        "monthly_volume": "~12K active SIMs",
        "funding_stage": "Private",
        "linkedin_url": "https://linkedin.com/in/priya-nadar",
    },
    {
        "name": "Devin Cole",
        "email": "dcole@beaconhealth.co",
        "company": "Beacon Health",
        "role": "CTO",
        "request_for": (
            "Building a telehealth platform. Need HIPAA-eligible programmable "
            "voice for appointment lines plus a Fax API — clinics still fax "
            "referrals. BAA required before we can move forward."
        ),
        "source": "demo_request",
        "created_at": "2026-07-09T16:40:00Z",
        "seniority": "C-Level",
        "department": "Engineering",
        "dept_headcount": 22,
        "company_employees": 140,
        "industry": "Healthcare / Telehealth",
        "hq_region": "Boston, US",
        "estimated_revenue_usd": "$10M-$25M",
        "current_provider": "None / in-house",
        "monthly_volume": "~40K voice min/mo",
        "funding_stage": "Series A",
        "linkedin_url": "https://linkedin.com/in/devincole",
    },
    {
        "name": "Tomasz Wójcik",
        "email": "tomasz@snapcart.app",
        "company": "SnapCart",
        "role": "Growth Lead",
        "request_for": (
            "E-commerce app. We want to run SMS marketing campaigns and cart "
            "recovery flows to ~90K opted-in users. What are your rates for "
            "high-volume A2P in the US and UK?"
        ),
        "source": "pricing_page",
        "created_at": "2026-07-12T08:33:00Z",
        "seniority": "Manager",
        "department": "Marketing / Growth",
        "dept_headcount": 9,
        "company_employees": 75,
        "industry": "E-commerce",
        "hq_region": "Kraków, PL",
        "estimated_revenue_usd": "$5M-$10M",
        "current_provider": "MessageBird",
        "monthly_volume": "~300K SMS/mo",
        "funding_stage": "Series A",
        "linkedin_url": "https://linkedin.com/in/tomaszwojcik",
    },
    {
        "name": "Aisha Bello",
        "email": "aisha.bello@gmail.com",
        "company": "(early-stage, unnamed)",
        "role": "Founder",
        "request_for": (
            "Just exploring for a side project — a reminder app for gym-goers. "
            "Might need to send a few texts. Is there a free tier?"
        ),
        "source": "live_chat",
        "created_at": "2026-07-12T21:47:00Z",
        "seniority": "Founder",
        "department": "Founder / Solo",
        "dept_headcount": 1,
        "company_employees": 1,
        "industry": "Consumer app (pre-seed)",
        "hq_region": "Lagos, NG",
        "estimated_revenue_usd": "$0-$1M",
        "current_provider": "None / in-house",
        "monthly_volume": "<1K SMS/mo",
        "funding_stage": "Bootstrapped",
        "linkedin_url": None,
    },
    {
        "name": "Jordan Fry",
        "email": "jordan.fry@studybuddy.edu",
        "company": "StudyBuddy",
        "role": "Student",
        "request_for": (
            "Doing a class project and want to build a chatbot that can call my "
            "phone. Do you have an API I can use for free for a school demo?"
        ),
        "source": "live_chat",
        "created_at": "2026-07-13T02:15:00Z",
        "seniority": "IC",
        "department": "N/A",
        "dept_headcount": 0,
        "company_employees": 0,
        "industry": "Education (student)",
        "hq_region": "Columbus, US",
        "estimated_revenue_usd": "$0-$1M",
        "current_provider": "None / in-house",
        "monthly_volume": "negligible",
        "funding_stage": "Bootstrapped",
        "linkedin_url": None,
    },
    {
        "name": "Robert Njoroge",
        "email": "r.njoroge@agristream.co.ke",
        "company": "AgriStream",
        "role": "Operations Lead",
        "request_for": (
            "We push market-price SMS to ~250K smallholder farmers across East "
            "Africa. Current sender-ID delivery is flaky. Need better local "
            "routes and shortcode support in KE/TZ/UG."
        ),
        "source": "web_form",
        "created_at": "2026-07-08T07:25:00Z",
        "seniority": "Manager",
        "department": "Operations",
        "dept_headcount": 18,
        "company_employees": 320,
        "industry": "AgriTech",
        "hq_region": "Nairobi, KE",
        "estimated_revenue_usd": "$10M-$25M",
        "current_provider": "Africa's Talking",
        "monthly_volume": "~1.2M SMS/mo",
        "funding_stage": "Series A",
        "linkedin_url": "https://linkedin.com/in/robertnjoroge",
    },
    {
        "name": "Pavel Novak",
        "email": "pavel@getleadsnow.biz",
        "company": "GetLeadsNow",
        "role": "Marketing Consultant",
        "request_for": (
            "I run cold SMS campaigns for clients and need to blast to purchased "
            "lists — a few hundred thousand numbers. Do you allow bulk sending "
            "without opt-in? Need it live this week."
        ),
        "source": "live_chat",
        "created_at": "2026-07-13T05:02:00Z",
        "seniority": "IC",
        "department": "Marketing",
        "dept_headcount": 2,
        "company_employees": 4,
        "industry": "Lead-gen / Marketing",
        "hq_region": "Prague, CZ",
        "estimated_revenue_usd": "$0-$1M",
        "current_provider": "Unknown",
        "monthly_volume": "unclear (bulk / unsolicited)",
        "funding_stage": "Bootstrapped",
        "linkedin_url": None,
    },
    {
        "name": "Oliver Grant",
        "email": "oliver.grant@northpeak.vc",
        "company": "NorthPeak Ventures",
        "role": "Investor / Partner",
        "request_for": (
            "Not a customer — I'm doing diligence on a portfolio company that "
            "uses CPaaS heavily and wanted to understand your pricing model and "
            "market position. Can someone walk me through it?"
        ),
        "source": "live_chat",
        "created_at": "2026-07-12T16:18:00Z",
        "seniority": "VP",
        "department": "Investment",
        "dept_headcount": 5,
        "company_employees": 30,
        "industry": "Venture Capital",
        "hq_region": "London, UK",
        "estimated_revenue_usd": "N/A",
        "current_provider": "N/A",
        "monthly_volume": "N/A (not a buyer)",
        "funding_stage": "Private",
        "linkedin_url": "https://linkedin.com/in/olivergrant-vc",
    },
    {
        "name": "Fatima Al-Sayed",
        "email": "fatima@souqexpress.ae",
        "company": "SouqExpress",
        "role": "Director of Customer Experience",
        "request_for": (
            "Q4 delivery peak is coming. We need SMS delivery notifications + a "
            "WhatsApp-style two-way channel and a voice IVR for order status. "
            "~1M orders/month across the GCC. Want to go live before November."
        ),
        "source": "demo_request",
        "created_at": "2026-07-11T11:41:00Z",
        "seniority": "Director",
        "department": "Customer Experience",
        "dept_headcount": 44,
        "company_employees": 1100,
        "industry": "E-commerce / Delivery",
        "hq_region": "Dubai, AE",
        "estimated_revenue_usd": "$250M-$500M",
        "current_provider": "Unifonic",
        "monthly_volume": "~1.5M SMS/mo",
        "funding_stage": "Series D",
        "linkedin_url": "https://linkedin.com/in/fatima-alsayed-cx",
    },
]
