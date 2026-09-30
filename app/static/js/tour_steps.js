// app/static/js/tour_steps.js
// -----------------------------------------------------------------
// WHAT the first-time walkthrough says, one list per role. HOW it is
// shown -- the spotlight, the card, moving between pages -- is all in
// tour.js; nothing in this file runs on its own.
//
// Written for someone who has never used a web app like this one. The
// rule for every step: say what the thing is FOR before saying what it
// is, in short sentences, with no words a sari-sari store owner would
// have to look up. "Viability Score" gets explained; "choropleth" never
// appears.
//
// A step is a plain object:
//
//   id          unique, stable name (used in tests and in debugging)
//   page        the path this step lives on ("/home"). Every step names
//               one, so moving to a step on another page is just a
//               navigation -- tour.js resumes there on load.
//   target      CSS selector (or a list of them, tried in order) for the
//               real element to spotlight. Leave it out, or let it miss,
//               and the step is shown as a centred card instead -- a
//               changed page layout makes the tour plainer, never broken.
//   closest     optional: widen the spotlight from the matched element to
//               its nearest ancestor matching this (e.g. the whole card
//               a chart sits in).
//   placement   optional preferred side for the card: right|left|top|bottom.
//   title, what, how    the card text. `what` answers "what is this
//               for?", `how` answers "how do I use it?".
//   list        optional bullet points shown under `what`.
//   labels      optional { what, how } to rename the two headings, for
//               steps that are about the tour rather than a control.
//
//   INTERACTIVE ("try it") steps add:
//   tryIt       the instruction, starting "Try it:".
//   advanceOn   { selector, event, match? } -- the step waits until that
//               event happens on (or inside) `selector`. `match`, when
//               given, must match the actual element clicked -- used on
//               the maps so a click on a barangay counts but a click on
//               the empty map background does not.
//   success     the "Nice! ..." line shown when they've done it.
//
//   prepare     optional { click: selector } -- clicked just before the
//               step shows, to open the tab the step talks about.
//   closeModals optional: close any open dialog when leaving the step.
//   finish      true on the last step; its button reads "Finish".
//
// The Home page anchors ([data-tour="plan-selector"] and friends) are
// added by the Home page itself. Everything else targets ids and
// classes that already exist on those pages.

(function (root) {
  "use strict";

  // Friendly names for "Taking you to the ... page".
  const PAGES = {
    "/home": "Home",
    "/saturation-map": "Saturation Map",
    "/trend-reports": "Trend Reports",
    "/recommendations": "Recommendations",
    "/community/": "Community",
    "/community/moderation": "Moderation",
    "/settings": "Settings",
    "/lgu/dashboard": "LGU Dashboard",
    "/lgu/government-data-upload": "Gov't Data Upload",
    "/admin/dashboard": "Admin Dashboard",
    "/admin/users": "Manage Users",
    "/admin/audit-log": "Audit Trail",
    "/admin/datasets": "Datasets",
    "/admin/settings": "System Settings",
  };

  // Shared wording, so the three tours explain the same things the same way.
  const HOW_THE_TOUR_WORKS =
    "Press Next to go forward and Back to go back. A few steps ask you to try something " +
    "yourself — that's the best way to learn. You can leave anytime with Exit tour.";
  const PHONE_MENU_TIP = "On a phone, tap the ☰ button at the top left to open this menu.";
  const REPLAY_TIP = "You can replay this tour anytime from Take the tour in the sidebar.";
  // The Community page's own "Write a post" button (the header one comes
  // first in the page, ahead of the empty-state copy).
  const WRITE_A_POST = '.forum-page a.btn-primary[href*="/community/new"]';
  const MAP_CLICK = {
    // The list of barangays on the right counts too: for someone who
    // finds the map fiddly, picking a name is the same action.
    selector: "#dss-map, #locationList",
    event: "click",
    match: ".leaflet-interactive, .dss-loc-item",
  };

  // =================================================================
  // SME -- business owners
  // =================================================================
  const SME = [
    {
      id: "sme-welcome",
      page: "/home",
      title: "Welcome to MarketLens!",
      labels: { what: "What MarketLens is for", how: "How this tour works" },
      what:
        "MarketLens helps you choose where in Tarlac City to open or grow your business. " +
        "It checks how many similar businesses are already in each barangay, how many people " +
        "live there, and how the market is changing.",
      how: "This tour takes about 5 minutes. " + HOW_THE_TOUR_WORKS,
    },
    {
      id: "sme-menu",
      page: "/home",
      target: "#dssSidebar .dss-nav",
      placement: "right",
      title: "Your main menu",
      what: "This menu is how you move between the pages of MarketLens. It stays in the same place on every page.",
      list: [
        "Home — your starting point: search, scores and your plans",
        "Saturation Map — how crowded each barangay is",
        "Trend Reports — how the market has changed over time",
        "Recommendations — where we suggest you open, and why",
        "Community — ask other business owners",
        "Settings — your account and your saved plans",
      ],
      how: "Click a name to open that page. The highlighted one is the page you're on now. " + PHONE_MENU_TIP,
    },
    {
      id: "sme-plan-selector",
      page: "/home",
      target: '[data-tour="plan-selector"]',
      title: "Switch between your plans",
      what:
        "A business plan is one business idea — for example, a milk tea shop in San Vicente. " +
        "If you have saved more than one, this lets you choose which plan the page is showing.",
      how: "Click it and pick a plan. The scores and forecast on this page change to match the plan you chose.",
    },
    {
      id: "sme-search",
      page: "/home",
      target: '[data-tour="search"]',
      title: "Look up a barangay",
      what: "Want to know about a certain place? This search box finds any of Tarlac City's 76 barangays.",
      how: "Type a barangay name, like Poblacion or San Vicente. Pick it from the list that appears, or press Search.",
    },
    {
      id: "sme-location-picker",
      page: "/home",
      target: '[data-tour="location-picker"]',
      title: "Point to a place on the map",
      what: "Not sure of the barangay's name? You can point to the spot on a map instead.",
      how: "Click this, then click the place where you'd like to open your business. MarketLens works out which barangay it is for you.",
    },
    {
      id: "sme-industry-select",
      page: "/home",
      target: '[data-tour="industry-select"]',
      title: "Choose your type of business",
      what:
        "Tell MarketLens what kind of business you have in mind — food, a shop, a service, and so on. " +
        "The scores you see are worked out for the type you choose here.",
      how: "Click the box to open the list, then click a business type.",
      tryIt: "Try it: choose any business type from the list.",
      advanceOn: { selector: '[data-tour="industry-select"]', event: "change" },
      success: "Nice! MarketLens will now use that type of business.",
    },
    {
      id: "sme-clear",
      page: "/home",
      target: '[data-tour="clear-search"]',
      title: "Start fresh",
      what: "This empties the search box and your choices, so you can look up something new.",
      how: "Click it anytime. It doesn't delete anything you've saved.",
    },
    {
      id: "sme-industry-cards",
      page: "/home",
      target: '[data-tour="industry-cards"]',
      title: "Market Score cards",
      what:
        "Each card is one type of business. Its Market Score (out of 10) shows how good the chance is " +
        "for that business in this barangay. A higher number means a better chance: fewer competitors " +
        "and more possible customers.",
      how:
        "Compare the cards to see which kinds of business have room to grow here. A green arrow means " +
        "a good chance; a red arrow means it will be hard to compete.",
    },
    {
      id: "sme-saved-plans",
      page: "/home",
      target: '[data-tour="saved-plans"]',
      title: "Your saved plans",
      what:
        "Every business idea you save is kept here, so you never have to type it again. MarketLens keeps " +
        "checking your plans and can warn you when a barangay gets more crowded.",
      how: "Click a plan to see its results. To change or delete a plan, go to Settings, then Business Preferences.",
    },
    {
      id: "sme-add-plan",
      page: "/home",
      target: '[data-tour="add-plan"]',
      title: "Add a new business plan",
      what: "This is how you tell MarketLens about a new business idea. It then studies the location and gives you a forecast.",
      how: "Click Add New Plan to open a short form.",
      tryIt: "Try it: click Add New Plan. Don't worry — nothing is saved until you finish the form yourself.",
      advanceOn: { selector: '[data-tour="add-plan"]', event: "click" },
      success: "Nice! That's the new plan form.",
    },
    {
      id: "sme-plan-form",
      page: "/home",
      // Whatever dialog is open -- the Add New Plan form, if the step
      // before was done. If it was skipped there is no open dialog and
      // this shows as a centred card, which still reads correctly.
      target: ".modal.show .modal-content",
      closeModals: true,
      title: "Filling in a plan",
      what:
        "The form asks for your business name, the type of business and what kind it is (for example, " +
        "a bakery rather than just \"food\"), what you will sell, and the barangay — type it, or press " +
        "Pick on map and click the place. You can also say what makes your business different, and add " +
        "a menu or price list. Only the name, type and barangay are required.",
      how:
        "When you're ready for real, fill it in and press the button at the bottom to get your forecast. " +
        "For now, just press Next — we'll close the form and nothing will be saved.",
    },
    {
      id: "sme-mini-map",
      page: "/home",
      target: '[data-tour="mini-map"]',
      title: "The saturation map",
      what: "This map shows Tarlac City divided into barangays. The colours show how crowded each place already is with this kind of business.",
      list: [
        "Red — very crowded, hard to compete",
        "Orange — busy",
        "Yellow — some room left",
        "Green — a good opportunity",
      ],
      how: "Click a barangay to see its details. Scroll, or use the + and − buttons, to zoom in and out.",
      tryIt: "Try it: click any coloured area on the map.",
      advanceOn: { selector: '[data-tour="mini-map"]', event: "click", match: ".leaflet-interactive" },
      success: "Nice! That's how you pick a barangay on any map in MarketLens.",
    },
    {
      id: "sme-forecast-panel",
      page: "/home",
      target: '[data-tour="forecast-panel"]',
      title: "Your forecast",
      what:
        "This is MarketLens's forecast for your plan. The Viability Score (out of 10) shows how likely the " +
        "business is to do well there — higher is better. The Saturation Level shows how crowded the market " +
        "already is: Low means few competitors, Saturated means there are already too many.",
      how:
        "Use it to compare places before you spend money. A high Viability Score with a low Saturation Level " +
        "is a good sign. The chart shows how things may change over the coming months.",
    },
    {
      id: "sme-recommendation-summary",
      page: "/home",
      target: '[data-tour="recommendation-summary"]',
      title: "What should I do?",
      what: "This is our advice in plain words: whether to go ahead, what to watch out for, and why.",
      how:
        "Treat it as a helpful second opinion, not the final word — you know your business best. " +
        "For the full explanation, open the Recommendations page.",
    },
    {
      id: "sme-saturation-map",
      page: "/saturation-map",
      target: "#dss-map",
      title: "The full Saturation Map",
      what:
        "This is the big version of the map, for the whole city. Every barangay is coloured by how crowded " +
        "it is for the business type chosen at the top of the page: red is crowded, green is open.",
      how: "Change the business type with the box at the top, or type a barangay in the search box to jump straight to it.",
      tryIt: "Try it: click any coloured area on the map, or a name in the list on the right.",
      advanceOn: MAP_CLICK,
      success: "Nice! The map now focuses on that barangay. Double-click it, or press Show All, to see the whole city again.",
    },
    {
      id: "sme-barangay-details",
      page: "/saturation-map",
      target: "#detailPanel",
      placement: "left",
      title: "Barangay details",
      what:
        "When you pick a barangay, its numbers appear here: how many people live there, how many businesses " +
        "of each kind are already there, and how crowded it is for your chosen type of business.",
      how: "Pick a few barangays one after another and compare their numbers before you decide.",
    },
    {
      id: "sme-trend-reports",
      page: "/trend-reports",
      target: "#summaryCards",
      placement: "bottom",
      title: "Trend Reports",
      what: "This page shows how Tarlac City's market has moved over time. Are more businesses opening? Are places getting more crowded?",
      how:
        "The four boxes are the headline numbers, compared with the month before. Use Select Period at the " +
        "top to look back at an earlier month. In the charts below, click a business type's name to hide or show its line.",
    },
    {
      id: "sme-recommendations",
      page: "/recommendations",
      target: ['[data-tour="rec-summary"]', ".dss-rec-stat-card"],
      closest: ".row",
      placement: "bottom",
      title: "Recommendations",
      what:
        "Here MarketLens suggests where your business could do well — and explains why. High Opportunity " +
        "places are ready to enter. Moderate ones can work if you offer something different.",
      how:
        "Scroll down to see each of your plans, with Why This Works (the good points) and Considerations " +
        "(the risks). If you picked what kind of business it is, you also see how many DIRECT competitors " +
        "it has, and what the AI thinks of what makes you different.",
    },
    {
      id: "sme-rec-other-locations",
      page: "/recommendations",
      target: '[data-tour="rec-other-locations"]',
      placement: "top",
      title: "Other places to consider",
      what:
        "\"Where else could you open this?\" lists other barangays where the same business could do well, " +
        "each with its reasons and risks.",
      how: "Press Save to My Plans on any card to keep it as a new plan — it then appears in the plan bar on Home.",
    },
    {
      id: "sme-community",
      page: "/community/",
      target: WRITE_A_POST,
      title: "Community",
      what:
        "A place to ask questions and swap tips with other business owners in Tarlac City. Posts are " +
        "grouped by topic in the list on the left.",
      how:
        "Press Write a post to ask a question, or open someone's post to reply and help them. New posts " +
        "are checked first, to keep the space safe and respectful.",
    },
    {
      id: "sme-settings",
      page: "/settings",
      target: "#settingsNav",
      placement: "right",
      title: "Settings",
      what:
        "Settings is where you look after your account: your profile, your saved business plans, " +
        "the notices MarketLens sends you, and how the screen looks.",
      how: "Click a section name to open it.",
      tryIt: "Try it: click Business Preferences.",
      advanceOn: { selector: '#settingsNav [data-section="plans"]', event: "click" },
      success: "Nice! These are your saved business plans.",
    },
    {
      id: "sme-settings-plans",
      page: "/settings",
      target: "#section-plans",
      prepare: { click: '#settingsNav [data-section="plans"]' },
      title: "Edit or delete a plan",
      what:
        "Each saved plan can be changed or deleted here. When you change a plan, MarketLens runs a " +
        "fresh forecast for it straight away.",
      how:
        "Press Edit, change the details, and save. Delete asks you to confirm first, because it can't be " +
        "undone. To choose which warnings you get, open Notifications in the list of sections.",
    },
    {
      id: "sme-finish",
      page: "/settings",
      finish: true,
      title: "You're all set!",
      labels: { what: "Where to find things", how: "Need a reminder?" },
      what: "That's the whole tour — you know your way around now.",
      list: [
        "Home — search a barangay, see Market Scores, and add plans",
        "Saturation Map — see which barangays are crowded",
        "Trend Reports — see how the market is changing",
        "Recommendations — where to open, and why",
        "Community — ask other business owners",
        "Settings — edit your plans and choose your notices",
        "The bell at the top — your messages from MarketLens",
      ],
      how: REPLAY_TIP,
    },
  ];

  // =================================================================
  // LGU -- the city office
  // =================================================================
  const LGU = [
    {
      id: "lgu-welcome",
      page: "/lgu/dashboard",
      title: "Welcome to MarketLens!",
      labels: { what: "What MarketLens is for", how: "How this tour works" },
      what:
        "MarketLens shows which barangays already have too many of the same kind of business, and which " +
        "ones need more. Business owners use it to decide where to open; the city uses it to plan and to " +
        "guide investors.",
      how: "This tour takes about 4 minutes. " + HOW_THE_TOUR_WORKS,
    },
    {
      id: "lgu-menu",
      page: "/lgu/dashboard",
      target: "#dssSidebar .dss-nav",
      placement: "right",
      title: "Your main menu",
      what: "This menu is how you move between the pages. It stays in the same place on every page.",
      list: [
        "LGU Dashboard — the whole city at a glance",
        "Saturation Map — how crowded each barangay is",
        "Trend Reports — how the market has changed",
        "Community — talk with business owners",
        "Gov't Data Upload — add the city's official records",
        "Settings — your account",
      ],
      how: "Click a name to open that page. " + PHONE_MENU_TIP,
    },
    {
      id: "lgu-search",
      page: "/lgu/dashboard",
      target: "#smeSearchForm",
      placement: "bottom",
      title: "Check a barangay",
      what: "Look up any barangay to see how crowded it is for a type of business.",
      how: "Type a barangay name, choose a business type in the box beside it, then press Check.",
    },
    {
      id: "lgu-recommendations",
      page: "/lgu/dashboard",
      target: ["#lguRecIndustry", ".dss-empty-state"],
      closest: ".dss-card",
      placement: "bottom",
      title: "City-wide recommendations",
      what:
        "Once the city's records are uploaded, this lists the barangays with the most room for a type of " +
        "business (Top Opportunity) and the ones that already have too many (Top Saturated).",
      how:
        "Choose a business type to update both lists. If it says \"No record yet\", upload a dataset " +
        "first — we'll show you where in a moment.",
    },
    {
      id: "lgu-barangays",
      page: "/lgu/dashboard",
      target: "#barangayTable",
      closest: ".dss-card",
      placement: "top",
      title: "All barangays",
      what: "Every barangay, with the number of businesses on file, so you can spot crowded and underserved areas.",
      how: "Type in the filter box to find a barangay quickly.",
    },
    {
      id: "lgu-upload",
      page: "/lgu/government-data-upload",
      target: 'form[enctype="multipart/form-data"]',
      placement: "bottom",
      title: "Upload a dataset",
      what:
        "This is where the city adds its official records — like business permits — so every score in " +
        "MarketLens reflects what is really on the ground.",
      how: "Choose the Target Table and the Source, pick your file (Excel or CSV), then press Upload.",
    },
    {
      id: "lgu-dataset-type",
      page: "/lgu/government-data-upload",
      target: "#datasetType",
      placement: "bottom",
      title: "Which kind of data?",
      what:
        "Gov't / Zoning Data is for permits, zoning and closures. Market Data is for competitor and demand " +
        "counts. Each one needs different columns in the file.",
      how: "Pick the one that matches your file. The table of columns below changes to match.",
      tryIt: "Try it: switch the Target Table and watch the column list below change.",
      advanceOn: { selector: "#datasetType", event: "change" },
      success: "Nice! The column list now matches that kind of data.",
    },
    {
      id: "lgu-template",
      page: "/lgu/government-data-upload",
      target: "#templateLink",
      title: "Use the template",
      what:
        "Not sure how to set up your file? The template is a ready-made spreadsheet with the right column " +
        "headings already in place. The table under this button says which columns are required.",
      how:
        "Download it, fill in one row per business, and upload it. If any row has a problem, nothing is " +
        "imported — you're told the row and column to fix, so your existing data is never half-changed.",
    },
    {
      id: "lgu-saturation-map",
      page: "/saturation-map",
      target: "#dss-map",
      title: "Saturation Map",
      what:
        "The whole city, coloured by how crowded each barangay is for the business type chosen at the " +
        "top: red is crowded, green is open.",
      how: "Change the business type with the box at the top. Click a barangay to see its numbers on the right.",
      tryIt: "Try it: click any coloured area on the map, or a name in the list on the right.",
      advanceOn: MAP_CLICK,
      success: "Nice! The map now focuses on that barangay. Double-click it, or press Show All, to see the whole city again.",
    },
    {
      id: "lgu-trend-reports",
      page: "/trend-reports",
      target: "#summaryCards",
      placement: "bottom",
      title: "Trend Reports",
      what: "How the city's market has moved over time: the number of businesses, new start-ups, and how crowded things are getting.",
      how: "Use Select Period to look back at an earlier month. Export / Print Report makes a copy for meetings.",
    },
    {
      id: "lgu-community",
      page: "/community/",
      target: WRITE_A_POST,
      title: "Community",
      what: "Where business owners ask questions and share tips — and where the city can answer them.",
      how: "Open a post to reply, or press Write a post to share an update. New posts are checked first, to keep the space safe.",
    },
    {
      id: "lgu-settings",
      page: "/settings",
      target: "#settingsNav",
      placement: "right",
      title: "Settings",
      what: "Your profile, your password, the notices MarketLens sends you, and how the screen looks.",
      how: "Click a section name to open it.",
    },
    {
      id: "lgu-finish",
      page: "/settings",
      finish: true,
      title: "You're all set!",
      labels: { what: "Where to find things", how: "Need a reminder?" },
      what: "That's the whole tour — you know your way around now.",
      list: [
        "LGU Dashboard — the city at a glance and city-wide recommendations",
        "Gov't Data Upload — add official records (use the template)",
        "Saturation Map — see which barangays are crowded",
        "Trend Reports — see how the market is changing",
        "Community — answer business owners' questions",
        "Settings — your account and notices",
      ],
      how: REPLAY_TIP,
    },
  ];

  // =================================================================
  // Admin -- the people who run this deployment
  // =================================================================
  const ADMIN = [
    {
      id: "admin-welcome",
      page: "/admin/dashboard",
      title: "Welcome to MarketLens!",
      labels: { what: "What MarketLens is for", how: "How this tour works" },
      what:
        "MarketLens helps business owners and the city decide where businesses should open in Tarlac City. " +
        "As an administrator, you keep it running smoothly: the accounts, the data, and the record of who " +
        "changed what.",
      how: "This tour takes about 4 minutes. " + HOW_THE_TOUR_WORKS,
    },
    {
      id: "admin-menu",
      page: "/admin/dashboard",
      target: "#dssSidebar .dss-nav",
      placement: "right",
      title: "Your main menu",
      what:
        "Everything business owners and the city office can see is here, plus an ADMIN section with the " +
        "tools only administrators have.",
      list: [
        "Admin Dashboard — the whole system at a glance",
        "Manage Users — accounts and who can sign in",
        "Audit Trail — who did what, and when",
        "Datasets — the data behind every score",
        "Moderation — community posts waiting for review",
        "System Settings — how the forecasting engine behaves",
      ],
      how: "Click a name to open that page. " + PHONE_MENU_TIP,
    },
    {
      id: "admin-dashboard",
      page: "/admin/dashboard",
      // The first row of counters. Plain Bootstrap classes, so an
      // anchor on the page would be sturdier -- see the report.
      target: ".dss-main .row.g-3",
      placement: "bottom",
      title: "The system at a glance",
      what: "These boxes count the accounts, forecasts and data rows in the system right now.",
      how: "Check them now and then. A sudden jump or drop is a sign to take a closer look in the Audit Trail.",
    },
    {
      id: "admin-users",
      page: "/admin/users",
      target: ".dss-admin-tabs",
      placement: "bottom",
      title: "Manage users",
      what:
        "Every account is listed here. Accounts are never deleted — they are archived instead, so their " +
        "history stays in the records and they can be brought back if needed.",
      how: "Use the Active & suspended and Archived tabs to switch lists. New Account, at the top, creates an account for someone.",
    },
    {
      id: "admin-reason",
      page: "/admin/users",
      target: '#pane-current [data-bs-target="#reasonModal"]',
      closest: "td",
      title: "Always give a reason",
      what:
        "Suspend stops someone signing in until you activate them again. Archive takes an account out of " +
        "use without erasing it.",
      how:
        "Both ask you to type a reason before they go through. The reason is saved in the Audit Trail, so " +
        "anyone checking later knows why it was done.",
    },
    {
      // After the step above on purpose: that one needs the first tab
      // showing, and this one switches away from it.
      id: "admin-archived-users",
      page: "/admin/users",
      target: "#tab-archived",
      placement: "bottom",
      title: "Archived accounts",
      what: "Archived accounts are kept here, not thrown away. Their history stays in the records.",
      how: "Press Restore next to an account, and give a reason, to let that person sign in again.",
      tryIt: "Try it: click the Archived tab.",
      advanceOn: { selector: "#tab-archived", event: "click" },
      success: "Nice! This is the archived list. Switch back with the Active & suspended tab.",
    },
    {
      id: "admin-audit",
      page: "/admin/audit-log",
      target: ".dss-filter-bar",
      placement: "bottom",
      title: "Audit Trail",
      what:
        "A permanent record of every important action: who did it, what they did, when, where (which " +
        "device and page), and why.",
      how:
        "Use these filters to narrow it down by name, role, action or date. Export CSV downloads exactly " +
        "what you've filtered, for reports.",
    },
    {
      id: "admin-datasets",
      page: "/admin/datasets",
      target: ".dss-admin-note",
      placement: "bottom",
      title: "Datasets",
      what:
        "The market and government data behind every score. If a row is wrong or out of date, archive it " +
        "and it stops being used straight away.",
      how: "Press Archive on a row and give a reason. Nothing is lost: archived rows wait in Archived records at the bottom of this page.",
    },
    {
      id: "admin-archived",
      page: "/admin/datasets",
      target: "#archived-records",
      placement: "top",
      title: "Bring data back",
      what: "Everything archived from this page is kept here.",
      how: "Press Restore, and give a reason, to put a row back into use. The scores pick it up on the next page load.",
    },
    {
      id: "admin-moderation",
      page: "/community/moderation",
      target: ".forum-mod-tabs",
      placement: "bottom",
      title: "Moderation",
      what:
        "New community posts and comments that need a look wait here, along with anything members have " +
        "reported — so nothing rude, misleading or unsafe reaches other users.",
      how:
        "Open each tab and read what's waiting, then Approve it or Reject it. Rejecting asks for a reason, " +
        "and the writer is told what you decided and why.",
    },
    {
      id: "admin-system-settings",
      page: "/admin/settings",
      target: ".dss-main form .dss-card",
      placement: "right",
      title: "System Settings",
      what: "These settings control how MarketLens scores the market — for example, how much each factor counts.",
      how:
        "Change them carefully, and only when you're sure: every forecast uses them. Each save is recorded " +
        "in the Audit Trail.",
    },
    {
      id: "admin-finish",
      page: "/admin/settings",
      finish: true,
      title: "You're all set!",
      labels: { what: "Where to find things", how: "Need a reminder?" },
      what: "That's the whole tour — you know your way around now.",
      list: [
        "Admin Dashboard — the system at a glance",
        "Manage Users — suspend, archive and restore accounts (with a reason)",
        "Audit Trail — who did what, when, where and why",
        "Datasets — archive and restore data rows",
        "Moderation — review community posts",
        "System Settings — tune the forecasting engine",
      ],
      how: REPLAY_TIP,
    },
  ];

  // Keyed by User.role exactly as the server sends it.
  const STEPS = { SME: SME, LGU: LGU, Admin: ADMIN };

  root.DSS_TOUR_STEPS = STEPS;
  root.DSS_TOUR_PAGES = PAGES;

  // For tests/tour_logic_test.js (Node). Ignored by the browser.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { steps: STEPS, pages: PAGES };
  }
})(typeof window !== "undefined" ? window : globalThis);
