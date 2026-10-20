from sqlalchemy import inspect

from app.database import engine


def test_v1_tables_and_columns_exist(client):
    insp = inspect(engine)
    for t in ("checkouts", "payments", "payment_events", "fine_payments", "webhook_events", "sandbox_transfers",
              "idempotency_keys"):
        assert t in insp.get_table_names()
    assert {"slug", "topics", "is_digital", "course"} <= {c["name"] for c in insp.get_columns("titles")}
    assert {"collection_point", "checkout_id"} <= {c["name"] for c in insp.get_columns("loans")}
    assert "metadata" in {c["name"] for c in insp.get_columns("payments")}
