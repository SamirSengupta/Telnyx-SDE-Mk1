"""Curated Telnyx product-fact base — the grounding source for outreach.

WHAT THIS FILE DOES: holds a hand-curated list of VERIFIED Telnyx facts, each
tagged with a topic, keywords, and the SOURCE URL it was researched from
(telnyx.com product/docs pages, July 2026). `seed_knowledge` loads these into
the `knowledge` table; `knowledge.retrieve_facts` pulls the relevant ones into
the composer/approver prompts so every Telnyx claim in an email is grounded in
a citable fact — not the model's memory.

Provenance matters: these were gathered by web research against Telnyx's own
site, not written from parametric knowledge. If a product detail changes, this
is the single file to update (in production: a sync job against Telnyx docs /
a CMS, refreshed on a schedule).

Topics align with schemas.Topic, plus three cross-cutting buckets always worth
surfacing: `platform` (the one-network story), `compliance`, and `coverage`
(where Telnyx is genuinely strong — the honest footprint, so the composer can't
over-claim geographies, e.g. the East-Africa case).
"""

from __future__ import annotations

# Each fact: topic, product, claim (one grounded sentence), tags (keyword bag
# for retrieval), source_url (provenance).
FACTS: list[dict] = [
    # ---- messaging -------------------------------------------------------- #
    {"topic": "messaging", "product": "SMS/MMS",
     "claim": "Telnyx sends SMS and MMS as a licensed carrier with global PSTN "
              "connectivity and end-to-end control over numbering, messaging, "
              "and telephony on its owned network.",
     "tags": "sms mms messaging text carrier deliverability owned network route",
     "source_url": "https://telnyx.com/products/sms-api"},
    {"topic": "messaging", "product": "10DLC",
     "claim": "For US application-to-person (A2P) long-code traffic Telnyx "
              "supports 10DLC registration; registered 10DLC numbers achieve a "
              "baseline throughput of 1 message per second per number.",
     "tags": "10dlc a2p compliance registration throughput us long code campaign",
     "source_url": "https://telnyx.com/resources/what-is-10dlc"},
    {"topic": "messaging", "product": "Messaging channels",
     "claim": "Telnyx offers 10DLC, short codes, and toll-free SMS — short "
              "codes for large-scale high-throughput campaigns, toll-free for "
              "nationwide presence, 10DLC for a cost/throughput balance.",
     "tags": "short code toll free 10dlc throughput volume campaign scale",
     "source_url": "https://telnyx.com/resources/what-is-10dlc"},

    # ---- voice ------------------------------------------------------------ #
    {"topic": "voice", "product": "Programmable Voice / Call Control",
     "claim": "The Telnyx Voice API can receive real-time commands throughout a "
              "call for interactive control, unlike solutions that require "
              "pre-scripted commands at the start of a call.",
     "tags": "voice call control programmable api ivr real-time commands",
     "source_url": "https://telnyx.com/products/voice-api"},
    {"topic": "voice", "product": "Low-latency edge / Voice AI",
     "claim": "Telnyx uses global edge PoPs with colocated GPUs to deliver "
              "Voice AI end-to-end latency of under 500 milliseconds, processing "
              "data nearest the customer application.",
     "tags": "latency voice ai agent media streaming stt llm tts edge gpu real-time",
     "source_url": "https://telnyx.com/resources/voice-ai-agents-compared-latency"},
    {"topic": "voice", "product": "Private voice network",
     "claim": "Telnyx routes voice over a private Tier-1 global network with "
              "strategically placed PoPs, avoiding the public internet to reduce "
              "jitter and packet loss, with wideband (AMR-WB) HD codecs.",
     "tags": "private network backbone jitter packet loss hd voice quality codec latency",
     "source_url": "https://telnyx.com/our-network"},

    # ---- iot_connectivity ------------------------------------------------- #
    {"topic": "iot_connectivity", "product": "IoT SIM coverage",
     "claim": "Telnyx IoT SIMs provide multi-network coverage on 650+ networks "
              "across 180+ countries using GSMA-compliant eUICC SIMs with "
              "multi-IMSI, remote SIM provisioning, and intelligent steering.",
     "tags": "iot sim m2m coverage global esim euicc multi-imsi networks countries fleet",
     "source_url": "https://telnyx.com/products/iot-sim-card"},
    {"topic": "iot_connectivity", "product": "SIM form factors",
     "claim": "All Telnyx SIMs are eUICC and available as triple-cut removable "
              "cards, embedded MFF2 chips, and downloadable OTA eSIM profiles.",
     "tags": "sim form factor mff2 esim embedded removable ota chip",
     "source_url": "https://telnyx.com/products/esim"},
    {"topic": "iot_connectivity", "product": "IoT SIM pricing",
     "claim": "Telnyx IoT SIMs cost $1 up front ($0.70 for OTA eSIMs) plus $2 "
              "per month per active SIM, with zone-based data starting at "
              "$0.0125 per MB and volume discounts.",
     "tags": "iot sim pricing cost price per mb data plan monthly volume discount",
     "source_url": "https://telnyx.com/pricing/iot-data-plans"},
    {"topic": "iot_connectivity", "product": "Mission Control Portal",
     "claim": "Telnyx gives IoT fleets one unified Mission Control Portal and "
              "APIs for SIM lifecycle control, OTA management, usage insights, "
              "and automation at scale.",
     "tags": "iot fleet management api portal lifecycle activate suspend dashboard one bill",
     "source_url": "https://telnyx.com/products/iot-sim-card"},

    # ---- verify_2fa ------------------------------------------------------- #
    {"topic": "verify_2fa", "product": "Verify API",
     "claim": "The Telnyx Verify API delivers OTP two-factor authentication via "
              "SMS, voice, and flash call, reaching over 190 country codes.",
     "tags": "verify 2fa otp two factor authentication sms voice flash call login",
     "source_url": "https://telnyx.com/products/verify-api"},
    {"topic": "verify_2fa", "product": "Verify abuse controls",
     "claim": "Telnyx Verify supports rate-limiting by user, phone number, IP, "
              "and session, plus CAPTCHA and authenticated-session checks, with "
              "client-based delivery receipts for insight.",
     "tags": "verify fraud rate limit captcha abuse dlr security 2fa otp",
     "source_url": "https://telnyx.com/resources/verify-two-factor-authentication"},
    {"topic": "verify_2fa", "product": "Verify pricing",
     "claim": "Telnyx Verify charges only for each successful verification (when "
              "the user's token matches the OTP), with greater discounts as "
              "volume or committed spend grows.",
     "tags": "verify pricing cost per verification successful volume discount 2fa",
     "source_url": "https://telnyx.com/pricing/verify-api"},
    {"topic": "verify_2fa", "product": "Number Lookup API",
     "claim": "Telnyx offers a separate Number Lookup (carrier lookup) API to "
              "retrieve information about a phone number, including whether it "
              "can receive SMS.",
     "tags": "number lookup carrier scrub invalid mobile validate phone verify",
     "source_url": "https://telnyx.com/products/number-lookup"},

    # ---- porting_trunking ------------------------------------------------- #
    {"topic": "porting_trunking", "product": "Number porting",
     "claim": "Telnyx can port and keep numbers across 50+ countries with "
              "FastPort cutover on demand and up-front CSR validation, and "
              "provisions numbers across 100+ countries.",
     "tags": "porting port numbers did fastport migration carrier cutover countries",
     "source_url": "https://telnyx.com/products/number-porting"},
    {"topic": "porting_trunking", "product": "Elastic SIP Trunking",
     "claim": "Telnyx Elastic SIP Trunking is pay-as-you-go with no minimum "
              "commitments and dynamic channel scaling, on a private Tier-1 "
              "network with low latency and no packet loss.",
     "tags": "sip trunking elastic concurrent channels scaling pbx per minute pay as you go",
     "source_url": "https://telnyx.com/products/sip-trunks"},
    {"topic": "porting_trunking", "product": "Trunking security",
     "claim": "Telnyx SIP trunks connect directly to carrier-grade "
              "infrastructure with no reseller hops, built-in TLS/SRTP "
              "encryption, and full STIR/SHAKEN attestation.",
     "tags": "sip trunk security tls srtp stir shaken encryption reseller carrier",
     "source_url": "https://telnyx.com/products/sip-trunks"},

    # ---- coverage (the honest footprint — prevents geographic over-claiming) #
    {"topic": "coverage", "product": "Carrier licenses & voice reach",
     "claim": "Telnyx holds carrier licenses in 30-40+ countries, with PSTN "
              "termination in 100+ countries and inbound calling reaching 130+ "
              "countries — its licensed-carrier strength is concentrated in "
              "North America and Europe.",
     "tags": "coverage countries carrier license pstn global reach voice region footprint",
     "source_url": "https://telnyx.com/global-coverage"},
    {"topic": "coverage", "product": "Operator Connect",
     "claim": "Telnyx Operator Connect for Microsoft Teams is available in 66 "
              "countries across six continents.",
     "tags": "operator connect microsoft teams countries coverage",
     "source_url": "https://telnyx.com/release-notes/microsoft-teams-operator-connect-countries"},

    # ---- compliance ------------------------------------------------------- #
    {"topic": "compliance", "product": "Certifications",
     "claim": "Telnyx's compliance posture covers SOC 2 Type II, HIPAA-eligible "
              "infrastructure, PCI DSS, ISO 27001, and GDPR with EU-deployed "
              "infrastructure for data locality.",
     "tags": "compliance hipaa soc2 pci iso gdpr data residency locality security baa healthcare",
     "source_url": "https://telnyx.com/resources/architecting-hipaa-telnyx"},
    {"topic": "compliance", "product": "Fax API",
     "claim": "Telnyx's Programmable Fax API sends fax over T.38 FoIP on its "
              "private IP network with HIPAA-compliant encryption; T.38 SIP "
              "faxes have encrypted signaling and media with no data stored on "
              "either end.",
     "tags": "fax api t38 foip hipaa healthcare referral encrypted programmable fax",
     "source_url": "https://telnyx.com/products/fax-api"},

    # ---- platform (the one-network story — always worth surfacing) -------- #
    {"topic": "platform", "product": "Owned global network",
     "claim": "Telnyx is a licensed carrier that owns its global private IP "
              "network over a private MPLS fiber backbone, spanning voice, "
              "messaging, numbers, IoT, and Verify on one API, one portal, one "
              "bill.",
     "tags": "platform one api portal bill owned network mpls backbone carrier consolidate",
     "source_url": "https://support.telnyx.com/en/articles/1130637-what-is-telnyx"},
    {"topic": "platform", "product": "Voice AI infrastructure",
     "claim": "Telnyx runs SIP trunks, PSTN termination, GPU inference, and "
              "speech models on the same private backbone, so AI voice agents "
              "run with no third-party intermediaries.",
     "tags": "voice ai agent inference gpu speech model media streaming platform backbone",
     "source_url": "https://telnyx.com/"},
]
