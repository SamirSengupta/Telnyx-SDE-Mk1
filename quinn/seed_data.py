"""Curated mock dataset for Quinn's inbound path.

Fake but recognizable: the companies are well-known names and the "requests"
are written in plain English (what the business wants, not telecom jargon), so
the demo is easy to follow. Everything Telnyx sells — texts, calls, login
codes, phone numbers, SIM cards — is framed the way a non-technical buyer would
describe it.

The spread of ICP fit is intentional so the qualifier + judge have real
decisions to make and every TIER shows up:
  * Hot  — big brand, clear need, real volume, ready to move.
  * Warm — good fit, but softer/uncertain timing.
  * Mild — small / marginal fit or just exploring (nurture, don't hard-sell).
  * Cold — not a real buyer (student, spammer, investor doing research).

One good-fit lead (Shopify) sits on the do-not-contact suppression list, so the
compliance gate visibly parks it in HELD instead of emailing it.

Each record carries both layers:
  * inbound_* fields  -> what the prospect typed into the form (self-reported)
  * everything else    -> the external enrichment Quinn would fetch live

seed.py splits them into the two tables at insert time.
"""

from __future__ import annotations

# created_at values are spread across early-to-mid July 2026 (today = 2026-07-15)
# so the "recent inbound queue" feels real.
RECORDS: list[dict] = [
    # ------------------------------------------------------------------ HOT
    {
        "name": "Diana Chen",
        "email": "diana.chen@uber.com",
        "company": "Uber",
        "role": "Operations Manager",
        "request_for": (
            "We connect millions of drivers and riders. We want them to be able "
            "to call and text each other inside the app without seeing each "
            "other's real phone numbers. We're on another provider today and "
            "want to lower our costs and improve reliability."
        ),
        "source": "demo_request",
        "created_at": "2026-07-13T14:22:00Z",
        "seniority": "Manager",
        "department": "Rider Operations",
        "dept_headcount": 45,
        "company_employees": 32000,
        "industry": "Ride-hailing / Mobility",
        "hq_region": "San Francisco, US",
        "estimated_revenue_usd": "$10B+",
        "current_provider": "Twilio",
        "monthly_volume": "~8M masked minutes/mo",
        "funding_stage": "Public",
        "linkedin_url": "https://linkedin.com/in/diana-chen-ops",
    },
    {
        "name": "Mark Reyes",
        "email": "mark.reyes@dominos.com",
        "company": "Domino's",
        "role": "Digital Operations Lead",
        "request_for": (
            "We text customers their order confirmation and a 'your pizza is on "
            "the way' update. That's about 3 million texts a month. We want more "
            "reliable delivery of those texts and better pricing than we get now."
        ),
        "source": "demo_request",
        "created_at": "2026-07-12T09:05:00Z",
        "seniority": "Director",
        "department": "Digital / E-commerce",
        "dept_headcount": 30,
        "company_employees": 15000,
        "industry": "Food & Delivery",
        "hq_region": "Ann Arbor, US",
        "estimated_revenue_usd": "$4B+",
        "current_provider": "Sinch",
        "monthly_volume": "~3M SMS/mo",
        "funding_stage": "Public",
        "linkedin_url": "https://linkedin.com/in/mark-reyes-digital",
    },
    {
        "name": "Priya Sharma",
        "email": "priya.sharma@netflix.com",
        "company": "Netflix",
        "role": "Security Engineer",
        "request_for": (
            "When someone signs in on a new device we send them a one-time "
            "verification code by text to confirm it's really them. We need this "
            "to work reliably worldwide at very high volume. Looking to compare "
            "providers."
        ),
        "source": "demo_request",
        "created_at": "2026-07-11T16:40:00Z",
        "seniority": "Senior IC",
        "department": "Security / Identity",
        "dept_headcount": 25,
        "company_employees": 13000,
        "industry": "Streaming / Media",
        "hq_region": "Los Gatos, US",
        "estimated_revenue_usd": "$30B+",
        "current_provider": "Vonage",
        "monthly_volume": "~10M verifications/mo",
        "funding_stage": "Public",
        "linkedin_url": "https://linkedin.com/in/priya-sharma-sec",
    },

    # ------------------------------------------------------------------ WARM
    {
        "name": "Alex Morgan",
        "email": "alex.morgan@onepeloton.com",
        "company": "Peloton",
        "role": "Product Manager",
        "request_for": (
            "We'd like to text members class reminders and a little motivation "
            "before their workout. It's a good-sized audience. We're planning "
            "for next year and comparing a few options — no rush yet."
        ),
        "source": "web_form",
        "created_at": "2026-07-10T12:10:00Z",
        "seniority": "Manager",
        "department": "Product / Lifecycle",
        "dept_headcount": 14,
        "company_employees": 3000,
        "industry": "Fitness / Connected Hardware",
        "hq_region": "New York, US",
        "estimated_revenue_usd": "$2B+",
        "current_provider": "None / in-house",
        "monthly_volume": "~500K SMS/mo (planned)",
        "funding_stage": "Public",
        "linkedin_url": "https://linkedin.com/in/alex-morgan-pm",
    },
    {
        "name": "Sam Patel",
        "email": "sam.patel@shopify.com",
        "company": "Shopify",
        "role": "Partnerships Lead",
        "request_for": (
            "We're exploring letting our merchants text their shoppers back and "
            "forth (two-way messaging) for order questions. Real interest, but "
            "we're still evaluating vendors and don't have a firm timeline."
        ),
        "source": "web_form",
        "created_at": "2026-07-09T11:12:00Z",
        "seniority": "Manager",
        "department": "Partnerships",
        "dept_headcount": 20,
        "company_employees": 8000,
        "industry": "E-commerce Platform",
        "hq_region": "Ottawa, CA",
        "estimated_revenue_usd": "$5B+",
        "current_provider": "Twilio",
        "monthly_volume": "~1M SMS/mo (potential)",
        "funding_stage": "Public",
        "linkedin_url": "https://linkedin.com/in/sam-patel-partnerships",
    },

    # ------------------------------------------------------------------ MILD
    {
        "name": "Laura Simmons",
        "email": "laura@brightsmiledental.com",
        "company": "BrightSmile Dental",
        "role": "Office Manager",
        "request_for": (
            "We have three dental offices and would like to text patients their "
            "appointment reminders. We're not very technical and it's a small "
            "number of messages. Just seeing what's out there — no timeline."
        ),
        "source": "web_form",
        "created_at": "2026-07-12T13:05:00Z",
        "seniority": "Manager",
        "department": "Front Office",
        "dept_headcount": 6,
        "company_employees": 40,
        "industry": "Healthcare / Dental",
        "hq_region": "Phoenix, US",
        "estimated_revenue_usd": "$5M-$10M",
        "current_provider": "None / in-house",
        "monthly_volume": "~5K SMS/mo",
        "funding_stage": "Private",
        "linkedin_url": "https://linkedin.com/in/laura-simmons-ops",
    },
    {
        "name": "Tom Becker",
        "email": "tom@greenroots.org",
        "company": "GreenRoots",
        "role": "Volunteer Coordinator",
        "request_for": (
            "We're a small environmental non-profit. Once in a while we text our "
            "volunteers about clean-up events — maybe a few hundred people. "
            "Wondering what something like that would cost. Just exploring for now."
        ),
        "source": "live_chat",
        "created_at": "2026-07-13T08:33:00Z",
        "seniority": "Coordinator",
        "department": "Community",
        "dept_headcount": 3,
        "company_employees": 25,
        "industry": "Non-profit / Environmental",
        "hq_region": "Portland, US",
        "estimated_revenue_usd": "$0-$1M",
        "current_provider": "None / in-house",
        "monthly_volume": "~2K SMS/mo",
        "funding_stage": "Non-profit",
        "linkedin_url": None,
    },

    # ------------------------------------------------------------------ COLD
    {
        "name": "Jake Miller",
        "email": "jake.miller@student.asu.edu",
        "company": "(school project)",
        "role": "Student",
        "request_for": (
            "I'm building a chatbot for a class project and want a free way to "
            "send a few text messages for my school demo. Is there a free tier "
            "I can use?"
        ),
        "source": "live_chat",
        "created_at": "2026-07-14T02:15:00Z",
        "seniority": "IC",
        "department": "N/A",
        "dept_headcount": 0,
        "company_employees": 0,
        "industry": "Education (student)",
        "hq_region": "Tempe, US",
        "estimated_revenue_usd": "$0-$1M",
        "current_provider": "None / in-house",
        "monthly_volume": "negligible",
        "funding_stage": "Bootstrapped",
        "linkedin_url": None,
    },
    {
        "name": "Victor Kozlov",
        "email": "victor@leadblastpro.biz",
        "company": "LeadBlast Pro",
        "role": "Marketing Consultant",
        "request_for": (
            "I run text-message campaigns for clients and need to blast to lists "
            "I've purchased — a few hundred thousand numbers that haven't opted "
            "in. Do you allow that? Need it live this week."
        ),
        "source": "live_chat",
        "created_at": "2026-07-14T05:02:00Z",
        "seniority": "IC",
        "department": "Marketing",
        "dept_headcount": 2,
        "company_employees": 4,
        "industry": "Lead-gen / Marketing",
        "hq_region": "Miami, US",
        "estimated_revenue_usd": "$0-$1M",
        "current_provider": "Unknown",
        "monthly_volume": "unclear (bulk / unsolicited)",
        "funding_stage": "Bootstrapped",
        "linkedin_url": None,
    },
    {
        "name": "Rachel Adams",
        "email": "rachel.adams@summitpartners.vc",
        "company": "Summit Partners",
        "role": "Investor",
        "request_for": (
            "Not a customer — I'm an investor doing research on a company that "
            "uses your service heavily. I just want to understand your pricing "
            "and how you compare in the market. Can someone walk me through it?"
        ),
        "source": "live_chat",
        "created_at": "2026-07-13T16:18:00Z",
        "seniority": "VP",
        "department": "Investment",
        "dept_headcount": 5,
        "company_employees": 60,
        "industry": "Venture Capital",
        "hq_region": "Boston, US",
        "estimated_revenue_usd": "N/A",
        "current_provider": "N/A",
        "monthly_volume": "N/A (not a buyer)",
        "funding_stage": "Private",
        "linkedin_url": "https://linkedin.com/in/rachel-adams-vc",
    },
]
