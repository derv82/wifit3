#!/usr/bin/env python
# Import the app INSIDE the __main__ guard: the WEP cracker's
# ProcessPoolExecutor spawns workers by re-importing this module, and we don't
# want each worker dragging in Textual + the whole UI just to run RC4 math.
if __name__ == "__main__":
    from wifit3.ui.app import WifiteApp

    app = WifiteApp()
    app.run()
