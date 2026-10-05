"""
Tests for Polar payment integration specifics.

Covers:
- Polar-Version date-based API version pinning on every SDK request
"""

from unittest.mock import patch

import httpx


class TestPolarApiVersionPinning:
    def test_polar_version_header_sent_on_requests(self):
        """Every Polar API request must carry the pinned Polar-Version header."""
        captured = {}

        def spy_send(self, request, **kwargs):
            captured.update(dict(request.headers))
            return httpx.Response(500, text="stop", request=request)

        with patch.object(httpx.Client, "send", spy_send):
            from app.services.payment_service import get_polar_client
            client = get_polar_client()
            try:
                client.checkouts.create(request={
                    "products": ["prod_test"],
                    "metadata": {"campaign_id": "1", "user_id": "1"},
                    "success_url": "http://test/campaigns/1",
                })
            except Exception:
                pass  # response parsing/network failure is fine — we only want headers

        from app.config import get_settings
        assert captured.get("polar-version") == get_settings().polar_api_version

    def test_polar_api_version_defaults_to_2026_04(self):
        """Default pin keeps the contract this integration was built against."""
        from app.config import Settings
        assert Settings().polar_api_version == "2026-04"

    def test_orders_list_call_site_uses_pinned_client(self):
        """The campaign-activation fallback must use get_polar_client (pinned)."""
        from app.routers.campaigns import _polar_activate_campaign
        from sqlmodel import Session, SQLModel, create_engine
        from sqlmodel.pool import StaticPool

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(engine)

        with patch("app.services.payment_service.get_polar_client") as mock_getter:
            mock_getter.return_value.orders.list.side_effect = RuntimeError("boom")
            with Session(engine) as session:
                result = _polar_activate_campaign(999, 1, session)

        assert mock_getter.called
        assert result is False  # failure handled gracefully
