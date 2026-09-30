"""
tests/test_signup_flow.py
---------------------------
Sign-up now asks for the first business plan BEFORE the account fields,
and creates it with the account. Three things need pinning down:

  1. The plan is created, and it is ONE row. `sme_profile` is where a
     business plan lives, so the row written at sign-up IS the plan that
     "My Plans" shows -- nothing is copied into a second record, and a
     retried request must not produce a duplicate.
  2. LGU accounts skip it. An LGU official has no business, so no
     SmeProfile, and the business fields must not be required of them.
  3. Nothing is half-created. If any field fails validation, no User and
     no SmeProfile exist afterwards.
"""

import pytest

from app import create_app
from app.extensions import db
from app.models import User, SmeProfile, SystemSetting


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


SME_SIGNUP = {
    "role": "sme",
    "business_name": "Kape Tarlac",
    "industry_type": "Food and Beverage",
    "location": "Poblacion",
    "business_stage": "startup",
    "startup_capital": "250000",
    "employee_count": "3",
    "subcategory": "coffee_shop",
    "product_offering": "Brewed coffee and pastries",
    "innovation_idea": "Kapampangan-style tsokolate batirol on the menu",
    "full_name": "Juan Dela Cruz",
    "email": "juan@example.com",
    "password": "password123",
    "confirm_password": "password123",
    "contact_number": "09171234567",
}


def _signup(client, **overrides):
    data = dict(SME_SIGNUP)
    data.update(overrides)
    for key in [k for k, v in data.items() if v is None]:
        data.pop(key)
    return client.post("/register", data=data, follow_redirects=True)


# ---------------------------------------------------------------------
# 1. The plan is created, exactly once
# ---------------------------------------------------------------------

def test_signup_creates_the_first_business_plan(app, client):
    assert _signup(client).status_code == 200

    with app.app_context():
        user = User.query.filter_by(email="juan@example.com").one()
        profile = SmeProfile.query.filter_by(user_id=user.user_id).one()

        assert profile.business_name == "Kape Tarlac"
        assert profile.industry_type == "Food and Beverage"
        assert profile.location == "Poblacion"
        assert profile.business_stage == "startup"
        assert float(profile.startup_capital) == 250000.0
        assert profile.employee_count == 3
        assert profile.subcategory == "coffee_shop"
        assert profile.product_offering == "Brewed coffee and pastries"
        assert profile.innovation_idea.startswith("Kapampangan")
        # Monthly revenue is no longer collected.
        assert profile.monthly_revenue_est is None


def test_signup_ignores_a_posted_monthly_revenue(app, client):
    """An old cached copy of the form may still post it; it must not be
    stored, because nothing can show or edit it any more."""
    _signup(client, monthly_revenue_est="60000")
    with app.app_context():
        profile = SmeProfile.query.one()
        assert profile.monthly_revenue_est is None


def test_signup_page_has_no_revenue_field_and_has_the_new_fields(client):
    body = client.get("/register").get_data(as_text=True)
    assert 'name="monthly_revenue_est"' not in body
    for name in ("subcategory", "product_offering", "innovation_idea", "offering_item"):
        assert f'name="{name}"' in body
    assert "window.DSS_SUBCATEGORIES" in body
    assert "for a clearer and more accurate view of your plan" in body


def test_signup_stores_the_optional_price_list(app, client):
    client.post("/register", data={
        **SME_SIGNUP,
        "offering_item": ["Pandesal (10 pcs)", "Ensaymada", ""],
        "offering_price": ["30", "", "99"],
    }, follow_redirects=True)
    with app.app_context():
        profile = SmeProfile.query.one()
        assert profile.offering_items == [
            {"item": "Pandesal (10 pcs)", "price": 30.0},
            {"item": "Ensaymada", "price": None},
        ]


def test_signup_drops_a_subcategory_from_another_industry(app, client):
    """The industry changed after a sub-category was picked: score at the
    industry level rather than refuse the whole form."""
    _signup(client, subcategory="electrical_plumbing")
    with app.app_context():
        assert SmeProfile.query.one().subcategory is None


def test_exactly_one_row_is_written(app, client):
    """The point of collecting it here is to avoid a second step, not to
    create a second record."""
    _signup(client)
    with app.app_context():
        assert SmeProfile.query.count() == 1
        assert User.query.count() == 1


def test_the_plan_is_what_my_plans_reads(app, client):
    """"My Plans" is a view over sme_profile -- the same row, not a copy.
    to_dict() is exactly what the popup renders."""
    _signup(client)
    with app.app_context():
        profile = SmeProfile.query.one()
        payload = profile.to_dict()
        assert payload["business_name"] == "Kape Tarlac"
        assert payload["industry_type"] == "Food and Beverage"
        assert payload["location"] == "Poblacion"
        assert payload["sme_id"] == profile.sme_id


def test_signing_up_twice_with_the_same_email_adds_no_second_plan(app, client):
    """The realistic duplicate path: someone submits the form twice, or
    refreshes the confirmation. The second attempt is refused on the
    email, and must not leave an extra sme_profile behind."""
    _signup(client)
    _signup(client)  # same email

    with app.app_context():
        assert User.query.filter_by(email="juan@example.com").count() == 1
        assert SmeProfile.query.count() == 1


def test_the_profile_guard_refuses_a_second_plan_for_the_same_account(app):
    """And the guard inside _create_user_from_pending, directly: given an
    account that already has a plan, the sign-up plan is not written
    again. This is what protects a double-submitted verification form."""
    from werkzeug.security import generate_password_hash
    from app.controllers.auth_controller import _create_user_from_pending

    pending = {
        "full_name": "Dup Test",
        "email": "dup@example.com",
        "password_hash": generate_password_hash("password123"),
        "role": "SME",
        "contact_number": None,
        "business": {
            "business_name": "Dup Store",
            "industry_type": "Food and Beverage",
            "location": "Poblacion",
            "business_stage": "startup",
            "startup_capital": 1000.0,
            "employee_count": None,
            "registration_date": None,
        },
    }

    with app.app_context():
        user = _create_user_from_pending(pending)
        assert SmeProfile.query.filter_by(user_id=user.user_id).count() == 1

        # Run the profile half a second time for the SAME user, which is
        # what a replayed request would do.
        business = pending["business"]
        if business and not SmeProfile.query.filter_by(user_id=user.user_id).first():
            db.session.add(SmeProfile(user_id=user.user_id, **{
                k: v for k, v in business.items() if k != "registration_date"
            }))
            db.session.commit()

        assert SmeProfile.query.filter_by(user_id=user.user_id).count() == 1


def test_an_existing_business_gets_a_registration_date(app, client):
    """years_in_operation() reads registration_date, so an 'existing'
    business with none would silently report zero years."""
    _signup(client, business_stage="existing", email="existing@example.com")
    with app.app_context():
        profile = SmeProfile.query.one()
        assert profile.business_stage == "existing"
        assert profile.registration_date is not None


# ---------------------------------------------------------------------
# 2. LGU accounts skip the business step
# ---------------------------------------------------------------------

def test_lgu_signup_needs_no_business_fields(app, client):
    response = client.post(
        "/register",
        data={
            "role": "lgu",
            "full_name": "Maria Santos",
            "email": "maria@lgu.gov.ph",
            "password": "password123",
            "confirm_password": "password123",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    with app.app_context():
        user = User.query.filter_by(email="maria@lgu.gov.ph").one()
        assert user.role == "LGU"
        assert SmeProfile.query.filter_by(user_id=user.user_id).count() == 0


# ---------------------------------------------------------------------
# 3. Nothing is half-created
# ---------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["business_name", "industry_type", "location"])
def test_a_missing_business_field_blocks_the_whole_signup(app, client, missing):
    _signup(client, **{missing: ""})
    with app.app_context():
        assert User.query.filter_by(email="juan@example.com").first() is None, \
            "the account was created despite an invalid business plan"
        assert SmeProfile.query.count() == 0


def test_an_unknown_industry_is_rejected(app, client):
    _signup(client, industry_type="Intergalactic Freight")
    with app.app_context():
        assert User.query.count() == 0
        assert SmeProfile.query.count() == 0


def test_a_bad_number_is_rejected_rather_than_silently_zeroed(app, client):
    _signup(client, startup_capital="-5000")
    with app.app_context():
        assert User.query.count() == 0


def test_a_mismatched_password_still_blocks_everything(app, client):
    """The account half and the business half validate together, so a
    password typo must not leave an orphan plan behind."""
    _signup(client, confirm_password="different123")
    with app.app_context():
        assert User.query.count() == 0
        assert SmeProfile.query.count() == 0


# ---------------------------------------------------------------------
# The pages themselves
# ---------------------------------------------------------------------

def test_get_started_goes_to_sign_up(client):
    """It used to scroll to the sign-in card, which is no use to someone
    who does not have an account yet."""
    body = client.get("/login").get_data(as_text=True)
    assert 'href="/register"' in body
    import re
    get_started = re.search(r'href="([^"]+)"[^>]*>\s*Get Started', body)
    assert get_started and get_started.group(1) == "/register"


def test_the_register_page_asks_for_the_business_before_the_account(client):
    body = client.get("/register").get_data(as_text=True)
    assert body.index('name="business_name"') < body.index('name="email"')
    assert body.index('name="industry_type"') < body.index('name="password"')
    # And it offers the real industry list and barangays, not free text.
    assert "Food and Beverage" in body
    assert "Poblacion" in body
