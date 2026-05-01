import pytest
from wifit3.app import WifiteApp
from textual.widgets import Header, Footer, Tree, RichLog

@pytest.mark.asyncio
async def test_app_layout_and_boot():
    """Verify the app boots and has the required layout components."""
    app = WifiteApp()
    async with app.run_test() as pilot:
        # Check Title
        assert pilot.app.title == "wifit3 - Wireless Auditor", "App title is incorrect"
        
        # Check Header
        header = pilot.app.query_one(Header)
        assert header is not None, "Header widget is missing"
        
        # Check Footer
        footer = pilot.app.query_one(Footer)
        assert footer is not None, "Footer widget is missing"
        
        # Check Sidebar (Tree)
        tree = pilot.app.query_one(Tree)
        assert tree is not None, "Sidebar (Tree) for APs is missing"
        
        # Check Main Terminal View (RichLog)
        rich_log = pilot.app.query_one(RichLog)
        assert rich_log is not None, "Main terminal view (RichLog) is missing"
