"""
app/ml/subcategories.py
-------------------------
The second level of the industry axis: what KIND of business, inside a
PSIC section.

WHY THIS EXISTS

The model scores an industry section in a barangay. That is the right
question for a city planner and the wrong one for a business owner.
"Food and Beverage is 72% saturated in Tibag" says nothing useful to
someone opening a pandesal bakery, if forty of Tibag's food businesses
are eateries and three are bakeries -- that owner faces three direct
competitors, not forty-three. The revision that asked for this put it
exactly: if the industry is crowded but my kind of shop is rare, am I
really affected by the saturation?

So a plan can now name a sub-category, and the system counts DIRECT
competitors for it. See app/services/subcategory_service.py for how that
count is found (live search, the LGU's permit register, or -- where
neither exists -- an estimate that is labelled as one) and how it adjusts
the score.

WHAT EACH ENTRY CARRIES

  key       stable identifier stored on sme_profile.subcategory.
  label     what the owner sees.
  query     the Google Places text search for this kind of business.
            Kept SME-scale for the same reason as SEARCH_TERM_MAP in
            places_service.py.
  keywords  lower-case words used to recognise this sub-category in a
            permit register's line-of-business column. Longest match
            wins, so "milk tea" is not swallowed by "tea".
  examples  shown under the dropdown so a non-technical owner can see
            which one they are.

Every section also offers "other": a plan filed there is scored at the
industry level, unadjusted, because there is nothing narrower to count.
"""

import re
from functools import lru_cache


OTHER_KEY = "other"

_OTHER = {
    "key": OTHER_KEY,
    "label": "Other / not listed",
    "query": None,
    "keywords": [],
    "examples": "scored at the industry level",
}


def _s(key, label, query, keywords, examples):
    return {"key": key, "label": label, "query": query,
            "keywords": keywords, "examples": examples}


SUBCATEGORIES = {
    "Food and Beverage": [
        _s("bakery", "Bakery / Pastries", "bakery OR bakeshop OR pastry shop",
           ["bakery", "bakeshop", "bake shop", "pandesal", "pastry", "pastries", "bread", "cake shop"],
           "pandesal, bread, cakes, pastries"),
        _s("coffee_shop", "Coffee Shop / Café", "coffee shop OR cafe",
           ["coffee", "cafe", "café", "espresso", "coffee shop"],
           "brewed coffee, espresso drinks, light snacks"),
        _s("milk_tea", "Milk Tea / Beverage Kiosk", "milk tea shop OR juice bar",
           ["milk tea", "milktea", "boba", "juice bar", "shake", "fruit shake", "smoothie"],
           "milk tea, shakes, fruit juices"),
        _s("fast_food", "Fast Food / Quick Service", "fast food restaurant",
           ["fast food", "burger", "fried chicken", "shawarma", "quick service"],
           "burgers, fried chicken, rice meals to go"),
        _s("restaurant", "Restaurant / Casual Dining", "restaurant",
           ["restaurant", "dining", "grill", "bistro", "samgyup", "buffet"],
           "sit-down meals, grill, family dining"),
        _s("eatery", "Eatery / Carinderia", "eatery OR carinderia",
           ["eatery", "carinderia", "karinderya", "turo-turo", "canteen", "lutong bahay"],
           "home-cooked viands, rice meals"),
        _s("snack_kiosk", "Snacks / Street Food Stall", "snack bar OR food kiosk",
           ["snack", "kiosk", "street food", "fishball", "siomai", "kwek", "food cart"],
           "siomai, fishball, fries, snacks"),
        _s("catering", "Catering / Food Trays", "catering service",
           ["catering", "food tray", "party tray", "events food"],
           "party trays, event catering"),
        _s("beverage_production", "Beverage / Food Production", "food products manufacturer OR beverage manufacturer",
           ["beverage production", "food processing", "bottling", "food products"],
           "packaged foods, bottled drinks"),
        _OTHER,
    ],
    "Accommodation and Food Service Activities": [
        _s("hotel", "Hotel / Inn", "hotel OR inn",
           ["hotel", "inn", "pension", "lodge", "lodging"], "rooms, short stays"),
        _s("resort", "Resort / Private Pool", "resort OR private pool",
           ["resort", "private pool", "villa"], "day tours, pool rentals"),
        _s("transient", "Transient House / Dormitory", "transient house OR dormitory",
           ["transient", "dormitory", "dorm", "boarding house", "bed space"], "dorms, bedspace, transients"),
        _s("restaurant", "Restaurant", "restaurant",
           ["restaurant", "dining", "grill", "buffet"], "sit-down dining"),
        _s("catering", "Catering / Events", "catering service OR events place",
           ["catering", "events", "banquet", "function hall"], "event catering, function halls"),
        _OTHER,
    ],
    "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles": [
        _s("grocery", "Grocery / Mini-mart", "grocery store OR minimart",
           ["grocery", "minimart", "mini mart", "supermarket", "convenience"], "daily goods, groceries"),
        _s("hardware", "Hardware / Construction Supply", "hardware store",
           ["hardware", "construction supply", "lumber", "paint"], "tools, cement, paint"),
        _s("tires_auto_parts", "Tires / Auto Parts", "tire shop OR auto parts store",
           ["tire", "gulong", "vulcanizing", "auto parts", "battery", "car accessories"],
           "tires, vulcanizing, batteries, parts"),
        _s("auto_repair", "Auto / Motorcycle Repair", "auto repair shop OR motorcycle repair shop",
           ["auto repair", "motor repair", "motorcycle repair", "talyer", "car wash", "auto shop"],
           "repair, maintenance, car wash"),
        _s("clothing", "Clothing / Apparel", "clothing store OR boutique",
           ["clothing", "apparel", "boutique", "ukay", "fashion", "shoes"], "clothes, shoes, ukay-ukay"),
        _s("gadgets", "Gadgets / Electronics", "cellphone store OR electronics store",
           ["cellphone", "gadget", "electronics", "computer shop", "phone accessories"],
           "phones, accessories, electronics"),
        _s("agri_supply", "Agri-supply / Feeds", "agricultural supply OR feeds store",
           ["agri", "feeds", "fertilizer", "agrivet", "poultry supply"], "feeds, fertilizer, farm inputs"),
        _s("pharmacy_retail", "Drugstore / Health Products", "drugstore",
           ["drugstore", "pharmacy", "botika"], "medicines, health products"),
        _s("wholesale", "Wholesale / Distribution", "wholesale distributor",
           ["wholesale", "distributor", "dealer", "supplier"], "bulk goods, distribution"),
        _OTHER,
    ],
    "Other Service Activities": [
        _s("salon", "Salon / Barbershop", "hair salon OR barbershop",
           ["salon", "barber", "barbershop", "parlor", "hair"], "haircuts, styling"),
        _s("spa_wellness", "Spa / Massage / Wellness", "spa OR massage",
           ["spa", "massage", "wellness", "nail", "lash"], "massage, nails, lashes"),
        _s("laundry", "Laundry / Dry Cleaning", "laundry shop",
           ["laundry", "laundromat", "dry clean", "wash and fold"], "wash-dry-fold, dry cleaning"),
        _s("repair_services", "Appliance / Gadget Repair", "appliance repair OR cellphone repair",
           ["appliance repair", "cellphone repair", "gadget repair", "electronics repair", "aircon"],
           "phone repair, appliance repair"),
        _s("tailoring", "Tailoring / Alterations", "tailoring shop",
           ["tailor", "alteration", "dressmaker", "sewing"], "alterations, custom clothes"),
        _s("funeral", "Funeral Services", "funeral services",
           ["funeral", "memorial"], "funeral homes, memorial services"),
        _OTHER,
    ],
    "Manufacturing": [
        _s("food_processing", "Food Processing", "food processing plant",
           ["food processing", "meat processing", "longganisa", "tocino", "delicacies", "kakanin"],
           "longganisa, delicacies, processed food"),
        _s("furniture", "Furniture / Woodwork", "furniture maker",
           ["furniture", "woodwork", "carpentry", "cabinet"], "furniture, cabinets"),
        _s("garments", "Garments / Textiles", "garments manufacturer",
           ["garment", "textile", "embroidery", "t-shirt printing"], "garments, uniforms"),
        _s("printing", "Printing / Signage", "printing press OR signage",
           ["printing", "print shop", "tarpaulin", "signage", "sticker"], "tarpaulin, signage, prints"),
        _s("metal_fabrication", "Metal / Welding Fabrication", "welding shop OR metal fabrication",
           ["welding", "fabrication", "steel", "metal works"], "gates, grills, fabrication"),
        _OTHER,
    ],
    "Construction": [
        _s("general_contractor", "General Contractor / Builder", "general contractor OR construction company",
           ["contractor", "construction", "builder"], "houses, buildings"),
        _s("electrical_plumbing", "Electrical / Plumbing", "electrical contractor OR plumbing services",
           ["electrical", "plumbing", "plumber", "electrician"], "wiring, plumbing"),
        _s("renovation", "Renovation / Finishing", "renovation services",
           ["renovation", "tiles", "painting services", "finishing", "roofing"], "renovation, tiling, roofing"),
        _OTHER,
    ],
    "Information and Communication": [
        _s("it_services", "IT Services / Software", "IT services company OR software company",
           ["it services", "software", "web development", "app development"], "software, web, IT support"),
        _s("internet_cafe", "Internet Café / Printing Shop", "internet cafe OR computer shop",
           ["internet cafe", "computer shop", "pisonet", "printing services"], "internet, printing, gaming"),
        _s("telecom", "Telecom / Load Retail", "telecommunications office",
           ["telecom", "load", "sim", "internet provider"], "load, internet plans"),
        _s("media", "Media / Publishing", "publishing house OR radio station",
           ["media", "publishing", "radio", "video production", "photography"], "media, photo & video"),
        _OTHER,
    ],
    "Human Health and Social Work Activities": [
        _s("clinic", "Medical Clinic", "medical clinic",
           ["clinic", "medical", "doctor", "physician"], "consultations, check-ups"),
        _s("dental", "Dental Clinic", "dental clinic",
           ["dental", "dentist", "orthodontic"], "dental care"),
        _s("pharmacy", "Pharmacy", "pharmacy OR drugstore",
           ["pharmacy", "drugstore", "botika"], "medicines"),
        _s("diagnostic", "Diagnostic Laboratory", "diagnostic laboratory",
           ["laboratory", "diagnostic", "x-ray", "ultrasound"], "lab tests, imaging"),
        _s("care_facility", "Care Facility / Therapy", "care facility OR therapy center",
           ["care home", "therapy", "rehabilitation", "physical therapy"], "elderly care, therapy"),
        _OTHER,
    ],
    "Agriculture, Forestry, and Fishing": [
        _s("crop_farming", "Crop Farming", "farm", ["farm", "rice", "vegetable", "crops"], "rice, vegetables"),
        _s("poultry_livestock", "Poultry / Livestock", "poultry farm OR piggery",
           ["poultry", "piggery", "livestock", "egg", "hog"], "eggs, chicken, hogs"),
        _s("fishery", "Fishery / Aquaculture", "fishery OR fish pond", ["fishery", "fish pond", "tilapia", "bangus"],
           "tilapia, bangus"),
        _OTHER,
    ],
    "Mining and Quarrying": [
        _s("sand_gravel", "Sand & Gravel / Aggregates", "sand and gravel supplier",
           ["sand", "gravel", "aggregates"], "sand, gravel"),
        _s("quarry", "Quarry", "quarry", ["quarry"], "quarry operations"),
        _OTHER,
    ],
    "Electricity, Gas, Steam, and Air Conditioning Supply": [
        _s("lpg", "LPG / Gas Distribution", "LPG distributor", ["lpg", "gas", "refill"], "LPG refills"),
        _s("solar", "Solar / Power Solutions", "solar panel installer",
           ["solar", "power", "generator"], "solar installation, generators"),
        _OTHER,
    ],
    "Water Supply; Sewerage, Waste Management, and Remediation Activities": [
        _s("water_refilling", "Water Refilling Station", "water refilling station",
           ["water refilling", "purified water", "mineral water", "water station"], "purified water delivery"),
        _s("waste", "Waste Collection / Junk Shop", "junk shop OR waste management services",
           ["junk shop", "junkshop", "recycling", "waste", "scrap"], "recycling, junk buying"),
        _s("septic", "Septic / Sanitation Services", "septic tank services",
           ["septic", "sanitation", "declogging"], "septic siphoning, declogging"),
        _OTHER,
    ],
    "Transportation and Storage": [
        _s("courier", "Courier / Delivery", "courier service",
           ["courier", "delivery", "padala", "logistics"], "parcel delivery"),
        _s("trucking", "Trucking / Hauling", "trucking company", ["trucking", "hauling", "lipat bahay"],
           "hauling, moving"),
        _s("warehouse", "Warehouse / Storage", "warehouse", ["warehouse", "storage"], "storage space"),
        _s("transport_terminal", "Terminal / Transport Service", "bus terminal OR van rental",
           ["terminal", "van rental", "car rental", "shuttle"], "van and car rental"),
        _OTHER,
    ],
    "Financial and Insurance Activities": [
        _s("lending", "Lending / Microfinance", "lending company",
           ["lending", "microfinance", "loan", "financing"], "personal and business loans"),
        _s("pawnshop", "Pawnshop", "pawnshop", ["pawnshop", "pawn"], "pawning, jewelry"),
        _s("remittance", "Remittance / Bills Payment", "money transfer service",
           ["remittance", "padala", "money transfer", "bills payment"], "remittance, bills payment"),
        _s("insurance", "Insurance Agency", "insurance agency", ["insurance"], "insurance plans"),
        _OTHER,
    ],
    "Real Estate Activities": [
        _s("rental_property", "Apartment / Space Rental", "apartment for rent OR commercial space for rent",
           ["apartment", "rental", "space for rent", "boarding"], "apartments, stalls for rent"),
        _s("realty", "Realty / Brokerage", "real estate agency", ["realty", "real estate", "broker"],
           "buying and selling property"),
        _OTHER,
    ],
    "Professional, Scientific, and Technical Activities": [
        _s("accounting_legal", "Accounting / Legal", "accounting firm OR law office",
           ["accounting", "bookkeeping", "law office", "notary", "tax"], "bookkeeping, notary, tax"),
        _s("engineering_design", "Engineering / Architecture", "engineering firm OR architectural firm",
           ["engineering", "architect", "surveying", "design"], "plans, surveys"),
        _s("veterinary", "Veterinary Clinic", "veterinary clinic", ["veterinary", "vet", "pet clinic"],
           "pet care"),
        _s("marketing_consulting", "Marketing / Consulting", "consulting firm OR marketing agency",
           ["consulting", "marketing", "advertising"], "business services"),
        _OTHER,
    ],
    "Administrative and Support Service Activities": [
        _s("manpower_security", "Manpower / Security Agency", "manpower agency OR security agency",
           ["manpower", "security agency", "staffing"], "staffing, guards"),
        _s("travel", "Travel Agency", "travel agency", ["travel", "ticketing", "tours"], "tickets, tours"),
        _s("equipment_rental", "Equipment / Event Rental", "equipment rental OR party rentals",
           ["rental", "party needs", "event rental", "sound system"], "chairs, tents, sound system"),
        _s("cleaning", "Cleaning / Janitorial", "janitorial services", ["janitorial", "cleaning", "pest control"],
           "cleaning, pest control"),
        _OTHER,
    ],
    "Education": [
        _s("tutorial", "Tutorial / Review Center", "tutorial center OR review center",
           ["tutorial", "review center", "tutoring", "learning center"], "tutoring, reviews"),
        _s("preschool", "Preschool / Daycare", "preschool OR daycare",
           ["preschool", "daycare", "kinder", "nursery"], "early learning"),
        _s("training_center", "Skills / Driving School", "training center OR driving school",
           ["training center", "driving school", "tesda", "vocational"], "skills training, driving lessons"),
        _OTHER,
    ],
    "Arts, Entertainment, and Recreation": [
        _s("gym", "Gym / Fitness", "gym OR fitness center", ["gym", "fitness", "crossfit", "yoga"],
           "memberships, classes"),
        _s("events_venue", "Events Venue", "event venue", ["events place", "venue", "function hall"],
           "parties, weddings"),
        _s("recreation", "Recreation / Gaming", "amusement center OR billiards",
           ["billiards", "videoke", "arcade", "amusement", "court rental"], "billiards, videoke, courts"),
        _OTHER,
    ],
    "Activities of Households as Employers": [
        _s("domestic_agency", "Domestic Helper Agency", "domestic helper agency",
           ["helper", "domestic", "kasambahay", "nanny", "caregiver"], "helpers, nannies"),
        _OTHER,
    ],
}


def subcategories_for(industry_type):
    """The sub-categories offered for an industry, "other" last. An
    industry typed outside BUSINESS_TYPES gets only "other"."""
    return SUBCATEGORIES.get(industry_type, [_OTHER])


def get_subcategory(industry_type, key):
    for entry in subcategories_for(industry_type):
        if entry["key"] == key:
            return entry
    return None


def subcategory_label(industry_type, key):
    entry = get_subcategory(industry_type, key)
    return entry["label"] if entry else None


def is_valid_subcategory(industry_type, key):
    return get_subcategory(industry_type, key) is not None


def countable_subcategories(industry_type):
    """Everything except "other" -- the entries narrow enough to count."""
    return [e for e in subcategories_for(industry_type) if e["key"] != OTHER_KEY]


def match_subcategory(industry_type, text):
    """The sub-category a free-text description most likely belongs to,
    or None. Longest keyword wins, so "milk tea" beats "tea" and
    "fast food" beats "food". Returning None is a real answer: a line of
    business that matches nothing is counted nowhere rather than counted
    in the wrong place -- the same rule canonical_industry_for() follows
    one level up."""
    if not text:
        return None
    lowered = str(text).strip().lower()
    best, best_len = None, 0
    for entry in countable_subcategories(industry_type):
        for keyword in entry["keywords"]:
            if len(keyword) > best_len and _keyword_pattern(keyword).search(lowered):
                best, best_len = entry["key"], len(keyword)
    return best


@lru_cache(maxsize=None)
def _keyword_pattern(keyword):
    """Whole words only (plus a plural -s/-es). A bare substring test
    read "inn" in "dinner", "tea" in "steak" and "tire" in "retired" --
    each a permit counted as the wrong kind of business."""
    return re.compile(r"(?<!\w)" + re.escape(keyword) + r"(?:s|es)?(?!\w)")


@lru_cache(maxsize=1)
def as_client_payload():
    """{industry: [{key, label, examples}]} -- what the plan forms need
    to populate the sub-category dropdown when the industry changes. No
    search queries or keywords: the browser has no use for them.
    Cached: the table is static, and every page with a plan form renders
    it. Callers must treat the result as read-only."""
    return {
        industry: [{"key": e["key"], "label": e["label"], "examples": e["examples"]} for e in entries]
        for industry, entries in SUBCATEGORIES.items()
    }
