import pytest
from wifit3.ui.app import WifiteApp
from textual.widgets import Footer, RichLog, DataTable, Static

@pytest.mark.asyncio
async def test_app_layout_and_boot():
    """Verify the app boots and has the required layout components."""
    app = WifiteApp()
    async with app.run_test() as pilot:
        # Check Title
        assert pilot.app.title == "wifit3 - Wireless Auditor", "App title is incorrect"
        
        # Check Header (Custom Static)
        header = pilot.app.query_one("#header-area")
        assert header is not None, "Header area is missing"
        
        # Check Footer
        footer = pilot.app.query_one(Footer)
        assert footer is not None, "Footer widget is missing"
        
        # Check DataTable
        table = pilot.app.query_one(DataTable)
        assert table is not None, "DataTable for APs is missing"
        
        # Check Main Terminal View (RichLog)
        rich_log = pilot.app.query_one(RichLog)
        assert rich_log is not None, "Main terminal view (RichLog) is missing"
