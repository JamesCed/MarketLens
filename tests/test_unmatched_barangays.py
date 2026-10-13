"""
tests/test_unmatched_barangays.py
-----------------------------------
The LGU dashboard's "uploaded name(s) not matched to a barangay" warning
is about names an LGU uploaded. Placeholder rows the app writes for
itself are not uploads and never trigger it, and uploaded rows left over
from before the importer refused unknown names are archived at start-up,
where an administrator can still restore them.
"""

import pytest

from app import create_app
from app.extensions import db
from app.models import LguData, SystemSetting, User
from app.services.forecasting_service import PLACEHOLDER_ZONING_NOTE, SYSTEM_USER_EMAIL
from tests.account_fixtures import STRONG_PASSWORD


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email, role, name="User"):
    user = User(name=name, email=email, role=role)
    user.set_password(STRONG_PASSWORD)
    db.session.add(user)
    db.session.commit()
    return user


def _row(barangay, uploaded_by, placeholder=False):
    row = LguData(source="Other" if placeholder else "DTI", barangay=barangay, permit_count=1,
                  zoning_info=PLACEHOLDER_ZONING_NOTE if placeholder else None, uploaded_by=uploaded_by)
    db.session.add(row)
    db.session.commit()
    return row


def _tile(app, email):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": STRONG_PASSWORD})
    page = client.get("/lgu/dashboard").get_data(as_text=True)
    tile = page[page.index("Barangays On File"):]
    return tile[:tile.index("</div>\n    </div>")]


def test_a_placeholder_the_app_wrote_is_not_an_uploaded_name(app):
    officer = _user("lgu@x.test", "LGU")
    system = _user(SYSTEM_USER_EMAIL, "Admin", name="System (placeholder data)")
    _row("Tibag", officer.user_id)
    _row("Somewhere Typed Freely", system.user_id, placeholder=True)
    assert "not matched" not in _tile(app, officer.email)


def test_an_uploaded_unmatched_name_is_shown_by_name(app):
    officer = _user("lgu@x.test", "LGU")
    _row("Not A Barangay", officer.user_id)
    tile = _tile(app, officer.email)
    assert "1 uploaded name(s) not matched to a barangay: Not A Barangay" in tile


def test_start_up_archives_only_uploaded_rows_that_match_no_barangay(app):
    from app.services.startup_migrations import UNMATCHED_BARANGAY_REASON, _archive_unmatched_uploaded_barangays

    officer = _user("lgu@x.test", "LGU")
    system = _user(SYSTEM_USER_EMAIL, "Admin", name="System (placeholder data)")
    official = _row("Tibag", officer.user_id).lgu_id
    alias = _row("Baras", officer.user_id).lgu_id
    unmatched = _row("Not A Barangay", officer.user_id).lgu_id
    placeholder = _row("Typed Freely", system.user_id, placeholder=True).lgu_id

    assert _archive_unmatched_uploaded_barangays() == 1
    assert _archive_unmatched_uploaded_barangays() == 0, "nothing left to archive on the next start"

    visible = {row.lgu_id for row in LguData.query.all()}
    assert visible == {official, alias, placeholder}
    archived = LguData.query.execution_options(include_archived=True).get(unmatched)
    assert archived.is_archived and archived.archive_reason == UNMATCHED_BARANGAY_REASON
    assert "not matched" not in _tile(app, officer.email)

    archived.restore()
    db.session.commit()
    assert unmatched in {row.lgu_id for row in LguData.query.all()}
