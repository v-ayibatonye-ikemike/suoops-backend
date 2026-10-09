"""Expense reports validate calendar dates and share tax-report ISO periods."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api import routes_expense
from app.api.dependencies import get_data_owner_id
from app.api.main import app
from app.api.routes_auth import get_current_user_id
from app.models import models


@pytest.fixture
def expense_client(db_session, monkeypatch):
    owner = models.User(phone="+2348160000093", name="Owner")
    db_session.add(owner)
    db_session.commit()
    monkeypatch.setitem(app.dependency_overrides, get_current_user_id, lambda: owner.id)
    monkeypatch.setitem(app.dependency_overrides, get_data_owner_id, lambda: owner.id)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("endpoint", ["/expenses/summary/by-period", "/expenses/stats/overview"])
@pytest.mark.parametrize(
    "params,status",
    [
        ({"period_type": "day", "year": 2025, "month": 2, "day": 30}, 400),
        ({"period_type": "week", "year": 2021, "week": 53}, 400),
        ({"period_type": "year", "year": 0}, 422),
        ({"period_type": "year", "year": 10000}, 422),
    ],
)
def test_invalid_periods_are_client_errors(expense_client, endpoint, params, status):
    response = expense_client.get(endpoint, params=params)
    assert response.status_code == status, response.text
    assert response.json()["detail"]


@pytest.mark.parametrize("endpoint", ["/expenses/summary/by-period", "/expenses/stats/overview"])
def test_valid_iso_week_53_spans_calendar_years(expense_client, endpoint):
    response = expense_client.get(endpoint, params={"period_type": "week", "year": 2020, "week": 53})
    assert response.status_code == 200, response.text
    assert response.json()["start_date"] == "2020-12-28"
    assert response.json()["end_date"] == "2021-01-03"


def test_default_period_remains_current_month(expense_client):
    response = expense_client.get("/expenses/summary/by-period")
    assert response.status_code == 200, response.text
    assert response.json()["start_date"] == date.today().replace(day=1).isoformat()


def test_current_week_uses_iso_year_at_new_year(expense_client, monkeypatch):
    class NewYearDate(date):
        @classmethod
        def today(cls):
            return cls(2021, 1, 1)

    monkeypatch.setattr(routes_expense, "date", NewYearDate)
    response = expense_client.get("/expenses/summary/by-period", params={"period_type": "week"})
    assert response.status_code == 200, response.text
    assert response.json()["start_date"] == "2020-12-28"
    assert response.json()["end_date"] == "2021-01-03"
